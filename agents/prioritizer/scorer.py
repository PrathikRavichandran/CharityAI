"""
Prioritizer Scorer — Gate 5

Multi-factor Scoring Rubric (0–100 pts total):

  Component              | Max Pts | Method
  ──────────────────────────────────────────────────────
  Urgency signals        |    25   | Deterministic keyword table
  Impact magnitude       |    25   | Llama3 reasoning
  Org reputation/cause   |    25   | Llama3 reasoning
  Time-in-queue bonus    |    25   | Deterministic: +5/week, cap 25

Weekly bump (runs on existing queue entries):
  Every 7 days in queue adds +5 pts to avoid starvation.
  Hard cap at 25 pts from bumping alone.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from pydantic_settings import BaseSettings

from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.db import fetch_one, execute
from shared.models import TaskType
from shared.ollama_client import OllamaRouter, ModelTask, ModelFailureError

log = logging.getLogger(__name__)
_router = OllamaRouter()


class ScorerSettings(BaseSettings):
    ORCHESTRATOR_URL:         str = "http://localhost:8000"
    QUEUE_BUMP_SCORE:         int = 5
    QUEUE_BUMP_CAP:           int = 25
    QUEUE_BUMP_INTERVAL_DAYS: int = 7

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = ScorerSettings()

# ── Urgency signal → pts mapping ─────────────────────────────────────────────
URGENCY_SCORES: dict[str, int] = {
    "emergency":      25,
    "disaster":       25,
    "life-threatening": 25,
    "crisis":         20,
    "critical":       20,
    "flood":          20,
    "hurricane":      20,
    "fire":           20,
    "urgent":         15,
    "immediate":      15,
    "at risk":        12,
    "facing closure": 12,
    "deadline":       10,
    "expiring":       10,
}

# High-priority cause areas (Feeding America, Red Cross, hospice, etc.)
HIGH_CAUSE_KEYWORDS = [
    "food security", "food bank", "hunger", "disaster relief",
    "housing", "homeless", "hospice", "cancer", "mental health",
    "human trafficking", "domestic violence", "refugee",
]


def compute_urgency_score(urgency_signals: list[str]) -> int:
    """
    Deterministic urgency component (0–25 pts).
    Takes the HIGHEST individual signal score found.
    """
    if not urgency_signals:
        return 0
    max_score = max(
        (URGENCY_SCORES.get(s.lower().strip(), 0) for s in urgency_signals),
        default=0,
    )
    return min(max_score, 25)


async def compute_impact_score(
    org_name: str, reason: str, urgency_signals: list[str]
) -> int:
    """
    Llama3 component: impact magnitude (0–25 pts).
    Returns 10 on model failure (conservative default).
    """
    prompt = f"""Rate the IMPACT MAGNITUDE of this charity request on a scale of 0-25 points.

Organization: {org_name}
Request reason: {reason}
Urgency signals: {', '.join(urgency_signals) if urgency_signals else 'None'}

Scoring guide:
  0-5:   Minimal impact, routine request
  6-12:  Moderate impact, serves hundreds of people
  13-19: High impact, serves thousands or critical need
  20-25: Extreme impact, life-threatening crisis affecting many

Respond with JSON only: {{"score": <integer 0-25>, "reasoning": "<one sentence>"}}"""

    try:
        raw = await _router.complete(
            task=ModelTask.PRIORITY_SCORE,
            prompt=prompt,
            temperature=0.2,
            max_tokens=150,
        )
        data = _extract_json(raw)
        score = int(data.get("score", 10))
        return max(0, min(score, 25))
    except (ModelFailureError, Exception) as e:
        log.warning("Impact scoring LLM failed: %s — defaulting to 10", e)
        return 10


async def compute_reputation_score(
    org_name: str,
    cause_category: Optional[str],
    reason: str,
    candid_impact_summary: Optional[str],
) -> int:
    """
    Llama3 component: org reputation + cause area (0–25 pts).
    Returns 10 on model failure.
    """
    # Quick deterministic boost for high-priority cause areas
    cause_text = (cause_category or "") + " " + reason.lower()
    if any(kw in cause_text for kw in HIGH_CAUSE_KEYWORDS):
        return 18   # Skip LLM for clear high-priority causes

    prompt = f"""Rate the REPUTATIONAL STRENGTH and cause area of this charity on a scale of 0-25 points.

Organization: {org_name}
Cause category: {cause_category or 'Unknown'}
Mission/reason: {reason[:300]}
Candid summary: {(candid_impact_summary or 'Not available')[:200]}

Scoring guide:
  0-8:   Low credibility or non-essential cause
  9-15:  Established org, moderate cause priority
  16-20: Well-known org or high-priority cause
  21-25: Nationally recognized org + critical cause

Respond with JSON only: {{"score": <integer 0-25>, "reasoning": "<one sentence>"}}"""

    try:
        raw = await _router.complete(
            task=ModelTask.PRIORITY_SCORE,
            prompt=prompt,
            temperature=0.2,
            max_tokens=150,
        )
        data = _extract_json(raw)
        score = int(data.get("score", 10))
        return max(0, min(score, 25))
    except (ModelFailureError, Exception) as e:
        log.warning("Reputation scoring LLM failed: %s — defaulting to 10", e)
        return 10


def compute_queue_bonus(days_in_queue: int) -> int:
    """
    Weekly bump: +5 pts per 7-day period in queue, capped at 25.
    Prevents starvation for low-scoring orgs.
    """
    if days_in_queue <= 0:
        return 0
    weeks = days_in_queue // _settings.QUEUE_BUMP_INTERVAL_DAYS
    bonus = weeks * _settings.QUEUE_BUMP_SCORE
    return min(bonus, _settings.QUEUE_BUMP_CAP)


# ── Main Entry Point ──────────────────────────────────────────────────────────

async def score_and_queue(payload: dict) -> None:
    """
    Score an eligible org and dispatch org.scored to the Orchestrator.
    Also upserts the org into the priority queue.
    """
    pipeline_id = payload.get("pipeline_id")
    ein         = payload.get("ein", "")

    # Fetch full pipeline row for scoring context
    row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
    if not row:
        log.error("Pipeline row not found: %s", pipeline_id)
        return

    org_name    = row["org_name"] or ""
    reason      = row["reason"] or ""
    urgency_signals = row.get("urgency_signals") or []

    # Fetch org data for reputation scoring
    org_row = await fetch_one("SELECT * FROM organizations WHERE ein = $1", ein)
    cause_category        = (org_row or {}).get("cause_category")
    candid_impact_summary = (org_row or {}).get("candid_impact_summary")

    log.info("Scoring org: '%s' ein=%s", org_name, ein)

    # ── Four scoring components ───────────────────────────────────────────────
    urgency_pts    = compute_urgency_score(urgency_signals)
    impact_pts     = await compute_impact_score(org_name, reason, urgency_signals)
    reputation_pts = await compute_reputation_score(org_name, cause_category, reason, candid_impact_summary)
    queue_bonus    = 0   # New entry — 0 days in queue yet

    total = urgency_pts + impact_pts + reputation_pts + queue_bonus
    total = max(0, min(total, 100))   # Clamp to 0–100

    score_breakdown = {
        "urgency":    urgency_pts,
        "impact":     impact_pts,
        "reputation": reputation_pts,
        "queue_bonus": queue_bonus,
        "total":      total,
    }

    log.info(
        "Score: ein=%s total=%d (urgency=%d impact=%d reputation=%d queue=%d)",
        ein, total, urgency_pts, impact_pts, reputation_pts, queue_bonus,
    )

    # ── Dispatch org.scored to Orchestrator ───────────────────────────────────
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.ORG_SCORED,
            payload={
                "pipeline_id":    pipeline_id,
                "ein":            ein,
                "priority_score": total,
                "score_breakdown": score_breakdown,
            },
            pipeline_id=pipeline_id,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch org.scored: %s", e)


def _extract_json(raw: str) -> dict:
    """Parse JSON from potentially fenced model response."""
    clean = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}
