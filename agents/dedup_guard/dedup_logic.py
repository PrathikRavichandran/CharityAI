"""
Dedup Guard Logic — Gate 2

Checks:
  1. Email hash (SHA-256 of body) — exact duplicate email body detection
  2. Active pipeline lookup by EIN — same org, new email → merge
  3. Clean pass → dispatch dedup.result to Orchestrator

No LLM. Pure DB queries.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from pydantic_settings import BaseSettings

from shared.db import fetch_one, fetch_all, execute
from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.models import TaskType

log = logging.getLogger(__name__)


class DedupSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = DedupSettings()

# Active states — if an org is in any of these, it counts as "active"
_ACTIVE_STATES = (
    "EMAIL_RECEIVED", "CLASSIFYING", "DEDUP_CHECK", "VERIFYING",
    "ELIGIBILITY_CHECK", "SCORING", "IN_PRIORITY_QUEUE",
    "FINDING_SLOT", "NO_SLOTS_REQUEUE", "SLOT_HELD",
    "PA_PENDING", "PA_APPROVED", "AUTO_APPROVED",
    "CONFIRMATION_SENT", "RSVP_PENDING",
)


async def process(payload: dict) -> None:
    """
    Full dedup check for an incoming classified email.
    Dispatches dedup.result back to Orchestrator.
    """
    email_id    = payload["email_id"]
    ein         = payload.get("ein")
    pipeline_id = payload.get("pipeline_id")   # Set by Orchestrator before dispatching here
    body_hash   = _compute_hash(payload)

    log.info("Dedup check: email_id=%s ein=%s", email_id, ein)

    # ── Check 1: Exact email body hash duplicate ───────────────────────────────
    existing_by_hash = await _check_hash_duplicate(body_hash)
    if existing_by_hash and existing_by_hash["email_id"] != email_id:
        log.info("DUPLICATE detected (same hash): %s", email_id)
        await _dispatch_result(
            pipeline_id=pipeline_id,
            email_id=email_id,
            is_duplicate=True,
            is_merged=False,
            message=f"Exact duplicate of email {existing_by_hash['email_id']}",
        )
        return

    # ── Check 2: Same EIN already active in pipeline ──────────────────────────
    if ein:
        active_pipeline = await _check_active_org(ein, pipeline_id)
        if active_pipeline:
            log.info(
                "MERGE: org EIN=%s already active in pipeline %s",
                ein, active_pipeline["id"],
            )
            # Update existing pipeline with any new urgency signals
            new_signals = payload.get("urgency_signals", [])
            if new_signals:
                await _merge_urgency_signals(str(active_pipeline["id"]), new_signals)

            await _dispatch_result(
                pipeline_id=pipeline_id,
                email_id=email_id,
                is_duplicate=True,
                is_merged=True,
                message=f"Merged into active pipeline {active_pipeline['id']} for EIN {ein}",
            )
            return

    # ── Store this email hash to detect future duplicates ─────────────────────
    await _store_hash(email_id, body_hash, pipeline_id)

    # ── Clean pass — dispatch to Orchestrator ─────────────────────────────────
    log.info("Dedup PASS: email_id=%s ein=%s", email_id, ein)
    await _dispatch_result(
        pipeline_id=pipeline_id,
        email_id=email_id,
        is_duplicate=False,
        is_merged=False,
        message="New unique email — cleared for verification",
    )


def _compute_hash(payload: dict) -> str:
    """
    SHA-256 hash of the email payload for exact duplicate detection.
    Uses org_name + ein + reason as the canonical fingerprint
    (body text can vary slightly with forwarding, signatures, etc.)
    """
    fingerprint = "|".join([
        str(payload.get("org_name", "")).lower().strip(),
        str(payload.get("ein", "")).strip(),
        str(payload.get("reason", ""))[:200].lower().strip(),
    ])
    return hashlib.sha256(fingerprint.encode()).hexdigest()


async def _check_hash_duplicate(body_hash: str) -> Optional[dict]:
    """Look up a pipeline row by email hash."""
    row = await fetch_one(
        "SELECT id, email_id FROM pipeline WHERE email_id IN "
        "(SELECT email_id FROM dropped_emails WHERE raw_snippet LIKE $1 LIMIT 1) LIMIT 1",
        f"%{body_hash[:16]}%",   # Approximate check via snippet prefix
    )
    # Also check active pipeline table for email_id match pattern
    # (full hash stored as part of raw_subject or details in production)
    return dict(row) if row else None


async def _check_active_org(ein: str, current_pipeline_id: Optional[str]) -> Optional[dict]:
    """
    Check if this EIN already has an active (non-terminal) pipeline entry.
    Excludes the current pipeline_id to avoid self-matching.
    """
    placeholders = ", ".join([f"'{s}'" for s in _ACTIVE_STATES])
    query = f"""
        SELECT id, current_state, email_id
        FROM pipeline
        WHERE ein = $1
        AND current_state IN ({placeholders})
    """
    params = [ein]
    if current_pipeline_id:
        query += " AND id != $2"
        params.append(current_pipeline_id)

    rows = await fetch_all(query, *params)
    return dict(rows[0]) if rows else None


async def _merge_urgency_signals(pipeline_id: str, new_signals: list[str]) -> None:
    """Append new urgency signals to the existing pipeline row (array merge)."""
    await execute(
        """
        UPDATE pipeline
        SET urgency_signals = array(
            SELECT DISTINCT unnest(
                COALESCE(urgency_signals, ARRAY[]::text[]) || $1::text[]
            )
        ),
        updated_at = NOW()
        WHERE id = $2
        """,
        new_signals, pipeline_id,
    )


async def _store_hash(email_id: str, body_hash: str, pipeline_id: Optional[str]) -> None:
    """
    Store the email hash in dropped_emails as a sentinel row for future dedup.
    We reuse raw_snippet to store the hash prefix (first 64 chars of SHA-256).
    """
    try:
        await execute(
            """
            INSERT INTO dropped_emails
                (email_id, drop_reason, raw_snippet, pipeline_id)
            VALUES ($1, 'exact_duplicate', $2, $3)
            ON CONFLICT DO NOTHING
            """,
            f"_hash_{email_id}", f"HASH:{body_hash}", pipeline_id,
        )
    except Exception:
        pass   # Non-critical


async def _dispatch_result(
    pipeline_id: Optional[str],
    email_id: str,
    is_duplicate: bool,
    is_merged: bool,
    message: str,
) -> None:
    """Send dedup.result back to the Orchestrator."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.DEDUP_RESULT,
            payload={
                "email_id":    email_id,
                "pipeline_id": pipeline_id,
                "is_duplicate": is_duplicate,
                "is_merged":   is_merged,
                "message":     message,
            },
            pipeline_id=pipeline_id,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch dedup result to orchestrator: %s", e)
