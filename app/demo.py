"""
CharityAI — single-process showcase deploy (HF Spaces).

This is NOT the full pipeline. The production system is 9 isolated FastAPI
agents talking over HTTP with Postgres + Redis + Ollama, run locally via
`make docker-up`. That architecture cannot fit on a free-tier cloud host.

This module is a deploy target for cloud demos: one process, no external
deps, walks through the same state machine using synthetic data so a
visitor (or interview panel) can see how the pipeline transitions an
email through the 9 gates without standing up the full infra.

Endpoints:
  GET  /                   landing HTML (architecture + how-to-demo)
  GET  /health             liveness
  GET  /architecture       state machine + transitions as JSON
  POST /demo/walkthrough   simulate a full pipeline run with stub agents
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# These imports work without any infra (no DB / Redis / Ollama touched).
from agents.orchestrator.state_machine import VALID_TRANSITIONS
from shared.models import PipelineState

app = FastAPI(
    title="CharityAI — Showcase Demo",
    description=(
        "Read-only showcase deploy. The full production pipeline (9 agents, "
        "Postgres, Redis, Ollama) runs locally via Docker Compose; this is "
        "the cloud-friendly demo entrypoint that walks the state machine."
    ),
    version="0.1.0-demo",
)


# ── Landing ───────────────────────────────────────────────────────────────────


_LANDING_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>CharityAI — Demo</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           max-width: 820px; margin: 2.5rem auto; padding: 0 1.25rem;
           color: #1a1a2e; line-height: 1.55; }
    h1 { color: #4338ca; margin-bottom: 0.25rem; }
    h2 { margin-top: 2rem; color: #1e1b4b; }
    code, pre { background: #f4f4f8; border-radius: 4px; padding: 0.1rem 0.4rem;
                font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 0.92em; }
    pre { padding: 0.85rem 1rem; overflow-x: auto; }
    .pill { display: inline-block; padding: 2px 10px; background: #e0e7ff;
            color: #4338ca; border-radius: 999px; font-size: 0.8em; font-weight: 600; }
    a { color: #4338ca; }
    .note { background: #fef9c3; border-left: 4px solid #ca8a04;
            padding: 0.75rem 1rem; margin: 1.25rem 0; border-radius: 4px; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
    th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e5e7eb; }
    th { background: #f9fafb; }
  </style>
</head>
<body>
  <span class="pill">Demo</span>
  <h1>CharityAI</h1>
  <p>Autonomous email-to-appointment pipeline. Watches an inbox, classifies,
     verifies the org (IRS), checks eligibility, prioritizes, books a slot,
     and routes through a human-in-the-loop gate &mdash; entirely without a
     human triaging the queue.</p>

  <div class="note">
    <strong>This page is the cloud showcase.</strong> The full system (9 isolated
    FastAPI agents + Postgres + Redis + Ollama) runs locally via
    <code>make docker-up</code>. This deploy walks the same state machine using
    synthetic data so the architecture is inspectable end-to-end without infra.
  </div>

  <h2>Try it</h2>
  <pre>curl -X POST $URL/demo/walkthrough \\
  -H "Content-Type: application/json" \\
  -d '{"org_name":"Hope Community Kitchen","ein":"12-3456789","reason":"food bank funding"}'</pre>
  <p>Or hit <a href="/docs">/docs</a> for the interactive Swagger UI.</p>

  <h2>Architecture (9 gates)</h2>
  <table>
    <tr><th>Gate</th><th>Agent</th><th>Decision</th></tr>
    <tr><td>1</td><td>Orchestrator</td><td>state machine &middot; A2A dispatch</td></tr>
    <tr><td>2</td><td>Email Watcher</td><td>Phi3 classify &middot; Mistral extract</td></tr>
    <tr><td>3</td><td>Dedup Guard</td><td>exact + semantic match</td></tr>
    <tr><td>4</td><td>Charity Verifier</td><td>IRS Tax-Exempt API</td></tr>
    <tr><td>5</td><td>Eligibility Agent</td><td>90-day window + urgency</td></tr>
    <tr><td>6</td><td>Prioritizer</td><td>Llama3 score &middot; weekly bumps</td></tr>
    <tr><td>7</td><td>Calendar Agent</td><td>Google Calendar 30-min slot</td></tr>
    <tr><td>8</td><td>PA Notification</td><td>Slack approve/reject &middot; 24h timeout</td></tr>
    <tr><td>9</td><td>Email Composer + RSVP</td><td>send invite &middot; track confirmation</td></tr>
  </table>
  <p>See <a href="/architecture">/architecture</a> for the full state machine
     (legal transitions, terminal states, model assignments).</p>

  <h2>Source</h2>
  <p><a href="https://github.com/PrathikRavichandran/CharityAI">github.com/PrathikRavichandran/CharityAI</a></p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def landing() -> str:
    return _LANDING_HTML


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": "demo",
        "version": "0.1.0-demo",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Architecture introspection ────────────────────────────────────────────────


@app.get("/architecture")
async def architecture() -> dict:
    """Expose the full state machine as JSON."""
    transitions: dict[str, list[str]] = {
        state.value: sorted(t.value for t in targets)
        for state, targets in VALID_TRANSITIONS.items()
    }
    initial_state = PipelineState.EMAIL_RECEIVED.value
    terminal_states = sorted(
        state.value for state, targets in VALID_TRANSITIONS.items() if not targets
    )
    return {
        "initial_state": initial_state,
        "terminal_states": terminal_states,
        "transitions": transitions,
        "agents": [
            {"gate": 1, "name": "orchestrator",     "role": "state machine, A2A dispatch"},
            {"gate": 2, "name": "email_watcher",    "role": "Gmail polling, classify, extract"},
            {"gate": 3, "name": "dedup_guard",      "role": "exact + semantic dedup"},
            {"gate": 4, "name": "charity_verifier", "role": "IRS verification"},
            {"gate": 5, "name": "eligibility",      "role": "90-day window + urgency escalation"},
            {"gate": 6, "name": "prioritizer",      "role": "impact scoring + queue bumps"},
            {"gate": 7, "name": "calendar",         "role": "Google Calendar slot search"},
            {"gate": 8, "name": "pa_notification",  "role": "Slack approve/reject + 24h timeout"},
            {"gate": 9, "name": "email_composer",   "role": "draft + send + RSVP tracking"},
        ],
    }


# ── Synthetic walkthrough ─────────────────────────────────────────────────────


class WalkthroughInput(BaseModel):
    org_name: str = "Sample Charity"
    ein: str = "12-3456789"
    reason: str = "Sample funding request for Q4 programming"


_DEMO_PATH: list[tuple[PipelineState, str, dict[str, Any]]] = [
    (PipelineState.EMAIL_RECEIVED,    "email_watcher",    {"event": "Inbox poll picked up message"}),
    (PipelineState.CLASSIFYING,       "email_watcher",    {"model": "phi3", "task": "email_classify",
                                                           "confidence": 0.93}),
    (PipelineState.DEDUP_CHECK,       "dedup_guard",      {"exact_match": False, "semantic_match": False}),
    (PipelineState.VERIFYING,         "charity_verifier", {"ein_lookup": "IRS Tax-Exempt match",
                                                           "verified": True, "confidence": 0.97}),
    (PipelineState.ELIGIBILITY_CHECK, "eligibility",      {"last_appt_days_ago": None,
                                                           "within_90d_window": False}),
    (PipelineState.SCORING,           "prioritizer",      {"model": "llama3",
                                                           "impact_scale": "high",
                                                           "priority_score": 78}),
    (PipelineState.IN_PRIORITY_QUEUE, "prioritizer",      {"queue_position": 2,
                                                           "queue_total_waiting": 7}),
    (PipelineState.FINDING_SLOT,      "calendar",         {"calendar_id": "primary",
                                                           "search_window_days": 14}),
    (PipelineState.SLOT_HELD,         "calendar",         {"slot_start": "2026-06-02T13:30:00-05:00",
                                                           "duration_minutes": 30,
                                                           "tentative_event_id": "evt_demo_001"}),
    (PipelineState.PA_PENDING,        "pa_notification",  {"slack_channel": "DM(@PA)",
                                                           "blocks_sent": True,
                                                           "timeout_hours": 24}),
    (PipelineState.PA_APPROVED,       "pa_notification",  {"decision": "approve",
                                                           "decided_at": "demo: PA clicked Approve"}),
    (PipelineState.CONFIRMATION_SENT, "email_composer",   {"model": "llama3",
                                                           "task": "confirmation_email",
                                                           "to": "demo@example.org"}),
    (PipelineState.RSVP_PENDING,      "rsvp_monitor",     {"timeout_hours": 24,
                                                           "thread_watched": True}),
    (PipelineState.RSVP_CONFIRMED,    "rsvp_monitor",     {"model": "phi3",
                                                           "task": "rsvp_classify",
                                                           "intent": "confirm"}),
    (PipelineState.BOOKED,            "calendar",         {"event_id_finalized": "evt_demo_001",
                                                           "appointment_locked": True}),
]


@app.post("/demo/walkthrough")
async def demo_walkthrough(req: WalkthroughInput) -> JSONResponse:
    """
    Walk a synthetic pipeline through every state in the happy path.
    No DB, Redis, Ollama, or external API is contacted — pure stub data.
    """
    timeline: list[dict[str, Any]] = []
    prev: PipelineState | None = None

    for to_state, actor, details in _DEMO_PATH:
        legal = (
            prev is None
            or to_state in VALID_TRANSITIONS.get(prev, set())
            or to_state == prev
        )
        timeline.append({
            "from_state": prev.value if prev else None,
            "to_state":   to_state.value,
            "actor":      actor,
            "legal":      legal,
            "details":    details,
        })
        prev = to_state

    return JSONResponse({
        "input": req.model_dump(),
        "final_state": prev.value if prev else None,
        "transitions": len(timeline),
        "timeline": timeline,
        "note": (
            "Synthetic walkthrough. Full pipeline (Gmail polling, real LLMs, "
            "real Postgres state) is local-only via `make docker-up`."
        ),
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run("app.demo:app", host="0.0.0.0", port=port)
