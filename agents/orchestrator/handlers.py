"""
Orchestrator A2A Task Handlers.

One handler per task_type. Each handler:
1. Parses the payload
2. Transitions pipeline state via StateMachine
3. Dispatches to next agent (if needed)

Architecture rule: ZERO business logic here.
Just: parse → transition → dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.models import (
    PipelineState,
    EmailClassifiedPayload, EmailDroppedPayload, DedupResultPayload,
    OrgVerifiedPayload, EligibilityResultPayload, OrgScoredPayload,
    SlotFoundPayload, SlotNotFoundPayload, PADecisionPayload,
    EmailSentPayload, RSVPOutcomePayload, TaskType, PADecision, RSVPNextAction,
)
from agents.orchestrator.mcp_tools import (
    db_drop_log, db_queue_upsert, db_write_appointment, a2a_dispatch,
)

log = logging.getLogger(__name__)


# ── GATE 1 result: Email Watcher → Orchestrator ────────────────────────────────

async def handle_email_classified(payload: dict, sm, agents: dict) -> None:
    p = EmailClassifiedPayload(**payload)
    pipeline_id = await sm.create_pipeline({
        "email_id":       p.email_id,
        "email_thread_id": p.email_thread_id,
        "ein":            p.ein,
        "org_name":       p.org_name,
        "contact_email":  p.contact_email,
        "reason":         p.reason,
        "urgency_signals": p.urgency_signals,
        "received_at":    p.received_at,
    })
    await sm.transition(
        pipeline_id, PipelineState.CLASSIFYING, actor="email_watcher",
        details={"classifier_confidence": p.classifier_confidence},
    )
    # Dispatch to Dedup Guard
    await sm.transition(pipeline_id, PipelineState.DEDUP_CHECK, actor="orchestrator")
    await a2a_dispatch(
        agents["dedup_guard"], TaskType.EMAIL_CLASSIFIED,
        {**payload, "pipeline_id": pipeline_id}, pipeline_id,
    )


async def handle_email_dropped(payload: dict, sm, agents: dict) -> None:
    p = EmailDroppedPayload(**payload)
    await db_drop_log(
        email_id=p.email_id,
        drop_reason=p.drop_reason,
        raw_subject=p.raw_subject,
        org_name=p.org_name,
        ein=p.ein,
    )
    log.info("Email dropped: %s reason=%s", p.email_id, p.drop_reason)


# ── GATE 2 result: Dedup Guard → Orchestrator ──────────────────────────────────

async def handle_dedup_result(payload: dict, sm, agents: dict) -> None:
    p = DedupResultPayload(**payload)
    if p.is_duplicate and not p.is_merged:
        pipeline_id = p.pipeline_id
        if pipeline_id:
            await sm.transition(
                pipeline_id, PipelineState.DROPPED_DUPLICATE,
                actor="dedup_guard",
                details={"message": p.message},
            )
        return
    if p.pipeline_id:
        await sm.transition(
            p.pipeline_id, PipelineState.VERIFYING,
            actor="orchestrator",
        )
        await a2a_dispatch(
            agents["charity_verifier"], TaskType.DEDUP_RESULT,
            payload, p.pipeline_id,
        )


# ── GATE 3 result: Charity Verifier → Orchestrator ────────────────────────────

async def handle_org_verified(payload: dict, sm, agents: dict) -> None:
    p = OrgVerifiedPayload(**payload)
    if not p.verified:
        await sm.transition(
            p.pipeline_id, PipelineState.DROPPED_NOT_VERIFIED,
            actor="charity_verifier",
            details={"sources": p.sources, "confidence": p.confidence},
        )
        return
    await sm.transition(
        p.pipeline_id, PipelineState.ELIGIBILITY_CHECK,
        actor="orchestrator",
        extra_updates={"ein": p.ein},
    )
    await a2a_dispatch(
        agents["eligibility"], TaskType.ORG_VERIFIED,
        payload, p.pipeline_id,
    )


# ── GATE 4 result: Eligibility Agent → Orchestrator ───────────────────────────

async def handle_eligibility_result(payload: dict, sm, agents: dict) -> None:
    p = EligibilityResultPayload(**payload)
    if not p.is_eligible:
        if p.escalate_to_pa:
            await sm.transition(
                p.pipeline_id, PipelineState.URGENT_ESCALATED_TO_PA,
                actor="eligibility_agent",
                details={"appointment_count_90d": p.appointment_count_90d},
            )
            await a2a_dispatch(
                agents["pa_notification"], TaskType.ELIGIBILITY_RESULT,
                payload, p.pipeline_id,
            )
        else:
            await sm.transition(
                p.pipeline_id, PipelineState.DROPPED_WITHIN_90_DAYS,
                actor="eligibility_agent",
                details={"appointment_count_90d": p.appointment_count_90d},
            )
        return
    await sm.transition(
        p.pipeline_id, PipelineState.SCORING, actor="orchestrator",
    )
    await a2a_dispatch(
        agents["prioritizer"], TaskType.ELIGIBILITY_RESULT,
        payload, p.pipeline_id,
    )


# ── GATE 5 result: Prioritizer → Orchestrator ──────────────────────────────────

async def handle_org_scored(payload: dict, sm, agents: dict) -> None:
    p = OrgScoredPayload(**payload)
    await db_queue_upsert(p.ein, p.pipeline_id, p.priority_score)
    await sm.transition(
        p.pipeline_id, PipelineState.IN_PRIORITY_QUEUE,
        actor="prioritizer",
        extra_updates={"priority_score": p.priority_score},
        details=p.score_breakdown.model_dump(),
    )
    # Try to find a slot immediately (Calendar Agent)
    await sm.transition(p.pipeline_id, PipelineState.FINDING_SLOT, actor="orchestrator")
    await a2a_dispatch(agents["calendar"], TaskType.ORG_SCORED, payload, p.pipeline_id)


# ── GATE 6 result: Calendar Agent → Orchestrator ──────────────────────────────

async def handle_slot_found(payload: dict, sm, agents: dict) -> None:
    p = SlotFoundPayload(**payload)
    slot = p.proposed_slot
    await sm.transition(
        p.pipeline_id, PipelineState.SLOT_HELD,
        actor="calendar_agent",
        extra_updates={
            "proposed_slot":      slot.start.isoformat(),
            "gcal_hold_event_id": slot.gcal_hold_id,
        },
    )
    await sm.transition(p.pipeline_id, PipelineState.PA_PENDING, actor="orchestrator")
    await a2a_dispatch(agents["pa_notification"], TaskType.SLOT_FOUND, payload, p.pipeline_id)


async def handle_no_slots(payload: dict, sm, agents: dict) -> None:
    p = SlotNotFoundPayload(**payload)
    await sm.transition(
        p.pipeline_id, PipelineState.NO_SLOTS_REQUEUE, actor="calendar_agent",
    )
    log.info("No slots found for pipeline %s — org stays in queue", p.pipeline_id)


# ── GATE 7 result: PA Notification → Orchestrator ─────────────────────────────

async def handle_pa_decision(payload: dict, sm, agents: dict) -> None:
    p = PADecisionPayload(**payload)
    if p.decision == PADecision.REJECTED:
        await sm.transition(
            p.pipeline_id, PipelineState.PA_REJECTED,
            actor=p.decided_by.value,
            details={"rejection_reason": p.rejection_reason, "pa_notes": p.pa_notes},
        )
        return

    target_state = (
        PipelineState.AUTO_APPROVED
        if p.decision == PADecision.AUTO_APPROVED
        else PipelineState.PA_APPROVED
    )
    await sm.transition(
        p.pipeline_id, target_state,
        actor=p.decided_by.value,
        extra_updates={"pa_response": p.decision.value, "pa_notes": p.pa_notes},
    )
    await sm.transition(
        p.pipeline_id, PipelineState.CONFIRMATION_SENT, actor="orchestrator",
    )
    await a2a_dispatch(agents["email_composer"], TaskType.PA_DECISION, payload, p.pipeline_id)


# ── GATE 8 result: Email Composer → Orchestrator ──────────────────────────────

async def handle_email_sent(payload: dict, sm, agents: dict) -> None:
    p = EmailSentPayload(**payload)
    if p.template == "confirmation_rsvp":
        await sm.transition(
            p.pipeline_id, PipelineState.RSVP_PENDING,
            actor="email_composer",
            extra_updates={"rsvp_sent_at": p.sent_at.isoformat()},
        )
        await a2a_dispatch(agents["rsvp_monitor"], TaskType.EMAIL_SENT, payload, p.pipeline_id)


# ── GATE 9 result: RSVP Monitor → Orchestrator ────────────────────────────────

async def handle_rsvp_outcome(payload: dict, sm, agents: dict) -> None:
    p = RSVPOutcomePayload(**payload)
    if p.next_action == RSVPNextAction.FINALIZE:
        await sm.transition(
            p.pipeline_id, PipelineState.RSVP_CONFIRMED,
            actor="rsvp_monitor",
            extra_updates={
                "gcal_event_id": p.gcal_event_id,
                "rsvp_received_at": p.rsvp_received_at.isoformat() if p.rsvp_received_at else None,
            },
        )
        await sm.transition(p.pipeline_id, PipelineState.BOOKED, actor="orchestrator")
        # Write to appointment_history (source of truth for 90-day rule)
        row = await sm.get_pipeline(p.pipeline_id)
        if row:
            await db_write_appointment(
                ein=row["ein"],
                scheduled_at=str(row["proposed_slot"]),
                gcal_event_id=p.gcal_event_id,
            )
        log.info("🎉 BOOKED: pipeline_id=%s gcal=%s", p.pipeline_id, p.gcal_event_id)

    else:  # cancel_and_pull_next
        await sm.transition(
            p.pipeline_id, PipelineState.RSVP_TIMEOUT_CANCELLED,
            actor="rsvp_monitor",
            details={"outcome": p.outcome.value},
        )
        # Pull next org from queue
        next_entry = await sm.get_top_queue()
        if next_entry:
            await sm.mark_queue_invited(str(next_entry["id"]))
            next_pipeline_id = str(next_entry["pipeline_id"])
            await sm.transition(
                next_pipeline_id, PipelineState.NEXT_IN_QUEUE_INVITED,
                actor="orchestrator",
                details={"invited_due_to": p.pipeline_id},
            )
            await sm.transition(
                next_pipeline_id, PipelineState.FINDING_SLOT, actor="orchestrator",
            )
            await a2a_dispatch(
                agents["calendar"], TaskType.ORG_SCORED,
                {"pipeline_id": next_pipeline_id, "ein": next_entry["ein"]},
                next_pipeline_id,
            )
            log.info("Next org invited: pipeline_id=%s", next_pipeline_id)
        else:
            log.info("No next org in queue after RSVP cancellation")
