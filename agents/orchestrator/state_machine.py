"""
Orchestrator State Machine — CharityAI Pipeline

Owns all state transitions for every email in the pipeline.
Validates legal transitions, persists to DB, fires Redis pub/sub events,
and dispatches A2A tasks to downstream agents.

Architecture Rule: The orchestrator NEVER performs business logic.
It only transitions state and delegates.
"""

from __future__ import annotations

import logging
from typing import Optional

from shared.db import transition_state, fetch_one, fetch_all, execute
from shared.redis_client import publish, set_timer, cancel_timer
from shared.models import PipelineState
from infra.redis_channels import PubSubChannels, TimerKeys

logger = logging.getLogger(__name__)

# ── Legal state transitions ────────────────────────────────────────────────────
# Maps (from_state) → set of valid (to_states)

VALID_TRANSITIONS: dict[PipelineState, set[PipelineState]] = {
    PipelineState.EMAIL_RECEIVED: {
        PipelineState.CLASSIFYING,
    },
    PipelineState.CLASSIFYING: {
        PipelineState.DEDUP_CHECK,
        PipelineState.DROPPED_NOT_CHARITY,
        PipelineState.DROPPED_MISSING_INFO,
        PipelineState.MODEL_FAILURE,
    },
    PipelineState.DEDUP_CHECK: {
        PipelineState.VERIFYING,
        PipelineState.DROPPED_DUPLICATE,
    },
    PipelineState.VERIFYING: {
        PipelineState.ELIGIBILITY_CHECK,
        PipelineState.DROPPED_NOT_VERIFIED,
        PipelineState.MODEL_FAILURE,
    },
    PipelineState.ELIGIBILITY_CHECK: {
        PipelineState.SCORING,
        PipelineState.DROPPED_WITHIN_90_DAYS,
        PipelineState.URGENT_ESCALATED_TO_PA,
    },
    PipelineState.URGENT_ESCALATED_TO_PA: {
        PipelineState.SCORING,           # PA allows exception
        PipelineState.DROPPED_WITHIN_90_DAYS,  # PA denies exception
    },
    PipelineState.SCORING: {
        PipelineState.IN_PRIORITY_QUEUE,
        PipelineState.MODEL_FAILURE,
    },
    PipelineState.IN_PRIORITY_QUEUE: {
        PipelineState.FINDING_SLOT,
    },
    PipelineState.FINDING_SLOT: {
        PipelineState.SLOT_HELD,
        PipelineState.NO_SLOTS_REQUEUE,
    },
    PipelineState.NO_SLOTS_REQUEUE: {
        PipelineState.FINDING_SLOT,     # Retry next poll cycle
    },
    PipelineState.SLOT_HELD: {
        PipelineState.PA_PENDING,
    },
    PipelineState.PA_PENDING: {
        PipelineState.PA_APPROVED,
        PipelineState.AUTO_APPROVED,
        PipelineState.PA_REJECTED,
        PipelineState.URGENT_ESCALATED_TO_PA,
    },
    PipelineState.PA_APPROVED: {
        PipelineState.CONFIRMATION_SENT,
    },
    PipelineState.AUTO_APPROVED: {
        PipelineState.CONFIRMATION_SENT,
    },
    PipelineState.PA_REJECTED: {
        # Terminal drop — no further transitions
    },
    PipelineState.CONFIRMATION_SENT: {
        PipelineState.RSVP_PENDING,
    },
    PipelineState.RSVP_PENDING: {
        PipelineState.RSVP_CONFIRMED,
        PipelineState.RSVP_TIMEOUT_CANCELLED,
    },
    PipelineState.RSVP_CONFIRMED: {
        PipelineState.BOOKED,
    },
    PipelineState.RSVP_TIMEOUT_CANCELLED: {
        PipelineState.NEXT_IN_QUEUE_INVITED,
    },
    PipelineState.NEXT_IN_QUEUE_INVITED: {
        PipelineState.FINDING_SLOT,     # Next org restarts at Calendar Agent
    },
    PipelineState.MODEL_FAILURE: {
        PipelineState.CLASSIFYING,      # Manual retry via PA action
        PipelineState.VERIFYING,
        PipelineState.SCORING,
    },
    # Terminal states — no outbound transitions
    PipelineState.BOOKED: set(),
    PipelineState.DROPPED_NOT_CHARITY: set(),
    PipelineState.DROPPED_MISSING_INFO: set(),
    PipelineState.DROPPED_DUPLICATE: set(),
    PipelineState.DROPPED_NOT_VERIFIED: set(),
    PipelineState.DROPPED_WITHIN_90_DAYS: set(),
    PipelineState.AGENT_UNREACHABLE: set(),
}

# Map state → Redis channel to publish on entry
STATE_TO_CHANNEL: dict[PipelineState, Optional[str]] = {
    PipelineState.DEDUP_CHECK:              PubSubChannels.EMAIL_CLASSIFIED,
    PipelineState.ELIGIBILITY_CHECK:        PubSubChannels.VERIFIED,
    PipelineState.SCORING:                  PubSubChannels.ELIGIBLE,
    PipelineState.IN_PRIORITY_QUEUE:        PubSubChannels.SCORED,
    PipelineState.SLOT_HELD:                PubSubChannels.SLOT_HELD,
    PipelineState.PA_APPROVED:              PubSubChannels.PA_APPROVED,
    PipelineState.AUTO_APPROVED:            PubSubChannels.AUTO_APPROVED,
    PipelineState.PA_REJECTED:              PubSubChannels.PA_REJECTED,
    PipelineState.RSVP_CONFIRMED:           PubSubChannels.RSVP_CONFIRMED,
    PipelineState.RSVP_TIMEOUT_CANCELLED:   PubSubChannels.RSVP_TIMEOUT,
    PipelineState.MODEL_FAILURE:            PubSubChannels.MODEL_FAILURE,
    PipelineState.AGENT_UNREACHABLE:        PubSubChannels.AGENT_UNREACHABLE,
}


class InvalidTransitionError(Exception):
    def __init__(self, from_state: str, to_state: str):
        super().__init__(f"Illegal transition: {from_state} → {to_state}")
        self.from_state = from_state
        self.to_state = to_state


class StateMachine:
    """
    Orchestrator state machine.
    All state changes must go through this class.
    """

    async def get_pipeline(self, pipeline_id: str) -> Optional[dict]:
        """Fetch a pipeline row by ID."""
        row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
        return dict(row) if row else None

    async def get_pipeline_by_email(self, email_id: str) -> Optional[dict]:
        """Fetch a pipeline row by Gmail message ID."""
        row = await fetch_one("SELECT * FROM pipeline WHERE email_id = $1", email_id)
        return dict(row) if row else None

    async def transition(
        self,
        pipeline_id: str,
        to_state: PipelineState,
        actor: str,
        extra_updates: Optional[dict] = None,
        details: Optional[dict] = None,
        event_payload: Optional[dict] = None,
    ) -> dict:
        """
        Validate and execute a state transition.

        Args:
            pipeline_id:   UUID of the pipeline row.
            to_state:      Target PipelineState.
            actor:         Agent/actor name for audit log.
            extra_updates: Additional DB column updates on the pipeline row.
            details:       Audit log JSONB details.
            event_payload: Payload to publish to Redis channel on entry.

        Returns:
            Updated pipeline row dict.

        Raises:
            InvalidTransitionError: If the transition is not legal.
            ValueError: If pipeline not found.
        """
        row = await self.get_pipeline(pipeline_id)
        if not row:
            raise ValueError(f"Pipeline not found: {pipeline_id}")

        current = PipelineState(row["current_state"])
        valid_next = VALID_TRANSITIONS.get(current, set())

        if to_state not in valid_next:
            raise InvalidTransitionError(current.value, to_state.value)

        # Execute DB transition + audit log
        updated = await transition_state(
            pipeline_id=pipeline_id,
            new_state=to_state.value,
            actor=actor,
            extra_updates=extra_updates,
            details=details,
        )

        if not updated:
            raise ValueError(f"DB transition failed for pipeline {pipeline_id}")

        updated_dict = dict(updated)
        logger.info(
            "State transition [%s → %s] pipeline_id=%s actor=%s",
            current.value, to_state.value, pipeline_id, actor,
        )

        # Publish Redis event
        channel = STATE_TO_CHANNEL.get(to_state)
        if channel:
            await publish(channel, {
                "pipeline_id": pipeline_id,
                "from_state":  current.value,
                "to_state":    to_state.value,
                "actor":       actor,
                **(event_payload or {}),
            })

        # Set TTL timers where needed
        await self._handle_timers(pipeline_id, to_state)

        return updated_dict

    async def _handle_timers(self, pipeline_id: str, state: PipelineState) -> None:
        """Set or cancel Redis TTL timers based on the new state."""
        if state == PipelineState.PA_PENDING:
            await set_timer(
                TimerKeys.pa_timeout(pipeline_id),
                TimerKeys.PA_TIMEOUT_TTL,
                value="pending",
            )
        elif state in {PipelineState.PA_APPROVED, PipelineState.AUTO_APPROVED, PipelineState.PA_REJECTED}:
            await cancel_timer(TimerKeys.pa_timeout(pipeline_id))

        if state == PipelineState.RSVP_PENDING:
            await set_timer(
                TimerKeys.rsvp_timeout(pipeline_id),
                TimerKeys.RSVP_TIMEOUT_TTL,
                value="pending",
            )
        elif state in {PipelineState.RSVP_CONFIRMED, PipelineState.RSVP_TIMEOUT_CANCELLED}:
            await cancel_timer(TimerKeys.rsvp_timeout(pipeline_id))

    # ── Convenience transition methods ────────────────────────────────────────

    async def create_pipeline(self, payload: dict) -> str:
        """Insert a new pipeline row and return its UUID."""
        row = await fetch_one(
            """
            INSERT INTO pipeline
                (email_id, email_thread_id, ein, org_name, contact_email,
                 reason, urgency_signals, current_state, received_at)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7::text[], 'EMAIL_RECEIVED', $8)
            RETURNING id
            """,
            payload["email_id"],
            payload.get("email_thread_id"),
            payload.get("ein"),
            payload.get("org_name"),
            payload.get("contact_email"),
            payload.get("reason"),
            payload.get("urgency_signals", []),
            payload.get("received_at"),
        )
        pipeline_id = str(row["id"])
        logger.info("Pipeline created: %s for email %s", pipeline_id, payload["email_id"])
        return pipeline_id

    async def get_top_queue(self) -> Optional[dict]:
        """Fetch the highest-priority org currently 'waiting' in the queue."""
        row = await fetch_one(
            """
            SELECT pq.*, p.email_id, p.ein, p.org_name, p.contact_email
            FROM priority_queue pq
            JOIN pipeline p ON pq.pipeline_id = p.id
            WHERE pq.status = 'waiting'
            ORDER BY pq.priority_score DESC, pq.entered_queue_at ASC
            LIMIT 1
            """
        )
        return dict(row) if row else None

    async def mark_queue_invited(self, queue_id: str) -> None:
        """Mark a queue entry as invited (being processed by Calendar Agent)."""
        await execute(
            "UPDATE priority_queue SET status = 'invited', updated_at = NOW() WHERE id = $1",
            queue_id,
        )

    async def is_eligible(self, ein: str) -> tuple[bool, int]:
        """
        Check 90-day eligibility rule.
        Returns (is_eligible, count_of_appointments_in_last_90_days).
        """
        from shared.db import fetch_val
        count = await fetch_val(
            """
            SELECT COUNT(*) FROM appointment_history
            WHERE ein = $1
            AND scheduled_at > NOW() - INTERVAL '90 days'
            """,
            ein,
        )
        count = int(count or 0)
        return (count == 0), count
