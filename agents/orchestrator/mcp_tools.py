"""
Orchestrator MCP Tools — DB + Redis + A2A operations.

These functions are the only way the Orchestrator touches external systems.
All business logic lives in the agents, not here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from shared.db import fetch_one, fetch_all, execute, fetch_val
from shared.redis_client import publish, set_timer, cancel_timer, get_redis
from shared.a2a_client import dispatch_task, health_check
from infra.redis_channels import TimerKeys

logger = logging.getLogger(__name__)


# ── DB State Tools ─────────────────────────────────────────────────────────────

async def db_state_read(pipeline_id: str) -> Optional[dict]:
    """Read the current pipeline state row."""
    row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
    return dict(row) if row else None


async def db_state_write(pipeline_id: str, updates: dict[str, Any]) -> None:
    """
    Write arbitrary column updates to a pipeline row.
    For state transitions, use StateMachine.transition() instead.
    """
    set_parts = []
    params: list[Any] = []
    for col, val in updates.items():
        params.append(val)
        set_parts.append(f"{col} = ${len(params)}")
    params.append(pipeline_id)
    query = f"UPDATE pipeline SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = ${len(params)}"
    await execute(query, *params)


async def db_drop_log(
    email_id: str,
    drop_reason: str,
    raw_subject: Optional[str] = None,
    org_name: Optional[str] = None,
    ein: Optional[str] = None,
    raw_snippet: Optional[str] = None,
    pipeline_id: Optional[str] = None,
) -> None:
    """Insert a row into the dropped_emails table."""
    await execute(
        """
        INSERT INTO dropped_emails
            (email_id, drop_reason, raw_subject, org_name_extracted,
             ein_extracted, raw_snippet, pipeline_id)
        VALUES ($1, $2::drop_reason, $3, $4, $5, $6, $7)
        """,
        email_id, drop_reason, raw_subject, org_name,
        ein, raw_snippet[:500] if raw_snippet else None,
        pipeline_id,
    )
    logger.info("Drop logged: email_id=%s reason=%s", email_id, drop_reason)


async def db_pipeline_lookup(ein: str) -> list[dict]:
    """Find all active (non-terminal) pipeline rows for an EIN."""
    rows = await fetch_all(
        """
        SELECT * FROM pipeline
        WHERE ein = $1
        AND current_state NOT IN (
            'BOOKED', 'DROPPED_NOT_CHARITY', 'DROPPED_MISSING_INFO',
            'DROPPED_DUPLICATE', 'DROPPED_NOT_VERIFIED', 'DROPPED_WITHIN_90_DAYS',
            'PA_REJECTED'
        )
        """,
        ein,
    )
    return [dict(r) for r in rows]


async def db_appointment_history_90d(ein: str) -> int:
    """Count appointments for this EIN in the last 90 days."""
    count = await fetch_val(
        """
        SELECT COUNT(*) FROM appointment_history
        WHERE ein = $1 AND scheduled_at > NOW() - INTERVAL '90 days'
        """,
        ein,
    )
    return int(count or 0)


async def db_write_appointment(
    ein: str,
    scheduled_at: str,
    gcal_event_id: Optional[str] = None,
) -> str:
    """Record a confirmed appointment. Returns the new appointment UUID."""
    row = await fetch_one(
        """
        INSERT INTO appointment_history
            (ein, scheduled_at, meeting_status, gcal_event_id)
        VALUES ($1, $2::timestamptz, 'booked', $3)
        RETURNING id
        """,
        ein, scheduled_at, gcal_event_id,
    )
    return str(row["id"])


async def db_queue_upsert(
    ein: str,
    pipeline_id: str,
    priority_score: float,
) -> str:
    """
    Upsert the priority queue entry for this org.
    If already in queue (waiting), update score.
    Returns queue entry UUID.
    """
    existing = await fetch_one(
        "SELECT id FROM priority_queue WHERE pipeline_id = $1 AND status = 'waiting'",
        pipeline_id,
    )
    if existing:
        await execute(
            "UPDATE priority_queue SET priority_score = $1, updated_at = NOW() WHERE id = $2",
            priority_score, str(existing["id"]),
        )
        return str(existing["id"])
    else:
        row = await fetch_one(
            """
            INSERT INTO priority_queue (ein, pipeline_id, priority_score)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            ein, pipeline_id, priority_score,
        )
        return str(row["id"])


async def db_urgency_flag_write(pipeline_id: str, urgency_signals: list[str]) -> None:
    """Write urgency signals detected by the Eligibility Agent."""
    await execute(
        "UPDATE pipeline SET urgency_signals = $1::text[], updated_at = NOW() WHERE id = $2",
        urgency_signals, pipeline_id,
    )


# ── Redis Tools ────────────────────────────────────────────────────────────────

async def redis_publish(channel: str, payload: dict) -> None:
    """Publish an event to a Redis pub/sub channel."""
    await publish(channel, payload)


async def redis_timer_set(key: str, ttl_seconds: int) -> None:
    """Set a TTL timer key in Redis."""
    await set_timer(key, ttl_seconds)


async def redis_timer_cancel(key: str) -> None:
    """Cancel a TTL timer key."""
    await cancel_timer(key)


# ── A2A Dispatch ───────────────────────────────────────────────────────────────

async def a2a_dispatch(
    target_url: str,
    task_type: str,
    payload: dict,
    pipeline_id: Optional[str] = None,
) -> dict:
    """Dispatch an A2A task to a downstream agent."""
    response = await dispatch_task(
        target_url=target_url,
        task_type=task_type,
        payload=payload,
        pipeline_id=pipeline_id,
    )
    return response.model_dump()


async def a2a_health(agent_url: str) -> bool:
    """Ping an agent's health endpoint."""
    return await health_check(agent_url)
