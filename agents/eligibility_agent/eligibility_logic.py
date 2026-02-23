"""
Eligibility Logic — Gate 4

Rule: An organization may only hold 1 appointment per 90-day rolling window.
      The window is measured from the date of their most recent scheduled appointment.

SQL:
    SELECT COUNT(*) FROM appointment_history
    WHERE ein = $1
    AND scheduled_at > NOW() - INTERVAL '90 days'

    COUNT > 0  AND no urgency → DROP + LOG
    COUNT > 0  AND urgency    → ESCALATE TO PA (system never decides exceptions)
    COUNT = 0                 → PASS to Prioritizer

No LLM — deterministic DB logic only.
Urgency signals already extracted and stored in the pipeline row by Email Watcher.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic_settings import BaseSettings

from shared.db import fetch_val, fetch_one
from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.models import TaskType

log = logging.getLogger(__name__)


class EligibilitySettings(BaseSettings):
    ORCHESTRATOR_URL:        str = "http://localhost:8000"
    ELIGIBILITY_WINDOW_DAYS: int = 90

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = EligibilitySettings()

# Urgency signals that can trigger PA escalation on ineligible repeat orgs
ESCALATION_SIGNALS = {
    "urgent", "emergency", "crisis", "critical", "immediate",
    "disaster", "flood", "hurricane", "fire", "life-threatening",
    "deadline", "at risk", "facing closure",
}


async def check(payload: dict) -> None:
    """
    Run the 90-day eligibility check for an org.
    Dispatches eligibility.result back to Orchestrator.
    """
    pipeline_id = payload.get("pipeline_id")
    ein         = payload.get("ein", "")

    if not ein:
        log.error("Eligibility check: missing EIN in payload for pipeline %s", pipeline_id)
        return

    log.info("Eligibility check: ein=%s pipeline_id=%s", ein, pipeline_id)

    # ── SQL: count recent appointments ────────────────────────────────────────
    count = await _count_recent_appointments(ein, _settings.ELIGIBILITY_WINDOW_DAYS)
    is_eligible = (count == 0)

    # ── Fetch urgency signals from pipeline row ────────────────────────────────
    urgency_signals = await _get_urgency_signals(pipeline_id)
    has_urgency = bool(urgency_signals & ESCALATION_SIGNALS)

    log.info(
        "Eligibility result: ein=%s count_90d=%d eligible=%s urgency=%s",
        ein, count, is_eligible, has_urgency,
    )

    # ── Determine escalation path ─────────────────────────────────────────────
    # Ineligible + urgency → PA decides. Ineligible + no urgency → drop.
    escalate = not is_eligible and has_urgency

    await _dispatch_result(
        pipeline_id=pipeline_id,
        ein=ein,
        is_eligible=is_eligible,
        appointment_count_90d=count,
        has_urgency_signals=has_urgency,
        escalate_to_pa=escalate,
    )


async def _count_recent_appointments(ein: str, window_days: int) -> int:
    """
    Core eligibility SQL query.
    Returns number of appointments for this EIN in the last N days.
    """
    count = await fetch_val(
        f"""
        SELECT COUNT(*) FROM appointment_history
        WHERE ein = $1
        AND scheduled_at > NOW() - INTERVAL '{window_days} days'
        """,
        ein,
    )
    return int(count or 0)


async def _get_urgency_signals(pipeline_id: Optional[str]) -> set[str]:
    """
    Fetch urgency signals array from the pipeline row.
    Returns empty set if pipeline not found or no signals.
    """
    if not pipeline_id:
        return set()
    row = await fetch_one(
        "SELECT urgency_signals FROM pipeline WHERE id = $1", pipeline_id
    )
    if not row:
        return set()
    signals = row["urgency_signals"] or []
    return {s.lower().strip() for s in signals}


async def _dispatch_result(
    pipeline_id: Optional[str],
    ein: str,
    is_eligible: bool,
    appointment_count_90d: int,
    has_urgency_signals: bool,
    escalate_to_pa: bool,
) -> None:
    """Send eligibility.result to the Orchestrator."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.ELIGIBILITY_RESULT,
            payload={
                "pipeline_id":           pipeline_id,
                "ein":                   ein,
                "is_eligible":           is_eligible,
                "appointment_count_90d": appointment_count_90d,
                "has_urgency_signals":   has_urgency_signals,
                "escalate_to_pa":        escalate_to_pa,
            },
            pipeline_id=pipeline_id,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch eligibility.result: %s", e)
