"""
Orchestrator Agent — FastAPI Server
Port: 8000 (default)

This is the pipeline controller. Every other agent posts task results here.
The orchestrator validates transitions, updates state, and dispatches to next agents.

Endpoints:
  POST /tasks         — Receive A2A task results from agents
  GET  /pipeline/{id} — Get current pipeline state
  GET  /queue         — Get current priority queue
  GET  /health        — Health check
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings

from shared.db import get_pool, close_pool
from shared.models import A2ATask, A2AResponse, TaskType, PipelineState
from shared.redis_client import get_redis, close_redis
from shared.a2a_client import A2ADispatchError

from agents.orchestrator.state_machine import StateMachine, InvalidTransitionError
from agents.orchestrator.mcp_tools import (
    db_state_read, db_drop_log, db_queue_upsert, db_write_appointment,
    a2a_dispatch, redis_publish,
)
from agents.orchestrator import handlers
from infra.redis_channels import PubSubChannels

# ── Settings ───────────────────────────────────────────────────────────────────

class OrchestratorSettings(BaseSettings):
    ORCHESTRATOR_PORT:       int  = 8000
    EMAIL_WATCHER_PORT:      int  = 8001
    DEDUP_GUARD_PORT:        int  = 8002
    CHARITY_VERIFIER_PORT:   int  = 8003
    ELIGIBILITY_AGENT_PORT:  int  = 8004
    PRIORITIZER_PORT:        int  = 8005
    CALENDAR_AGENT_PORT:     int  = 8006
    PA_NOTIFICATION_PORT:    int  = 8007
    EMAIL_COMPOSER_PORT:     int  = 8008
    RSVP_MONITOR_PORT:       int  = 8009
    LOG_LEVEL:               str  = "INFO"
    ENV:                     str  = "development"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = OrchestratorSettings()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=settings.LOG_LEVEL)
log = structlog.get_logger()

# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Orchestrator starting up", env=settings.ENV)
    await get_pool()
    await get_redis()
    yield
    log.info("Orchestrator shutting down")
    await close_pool()
    await close_redis()

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="CharityAI — Orchestrator",
    description="Pipeline state machine controller. Receives A2A tasks, transitions state, dispatches to next agents.",
    version="1.0.0",
    lifespan=lifespan,
)

sm = StateMachine()

# ── Agent URL builder ──────────────────────────────────────────────────────────
def agent_url(port: int, path: str = "/tasks") -> str:
    return f"http://localhost:{port}{path}"

AGENT_URLS = {
    "email_watcher":    agent_url(settings.EMAIL_WATCHER_PORT),
    "dedup_guard":      agent_url(settings.DEDUP_GUARD_PORT),
    "charity_verifier": agent_url(settings.CHARITY_VERIFIER_PORT),
    "eligibility":      agent_url(settings.ELIGIBILITY_AGENT_PORT),
    "prioritizer":      agent_url(settings.PRIORITIZER_PORT),
    "calendar":         agent_url(settings.CALENDAR_AGENT_PORT),
    "pa_notification":  agent_url(settings.PA_NOTIFICATION_PORT),
    "email_composer":   agent_url(settings.EMAIL_COMPOSER_PORT),
    "rsvp_monitor":     agent_url(settings.RSVP_MONITOR_PORT),
}

# ── Main A2A Task Receiver ─────────────────────────────────────────────────────

@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """
    Main entry point for all A2A task results from downstream agents.
    Routes to the correct handler based on task_type.
    """
    log.info("A2A task received", task_id=task.task_id, task_type=task.task_type)

    try:
        handler = TASK_HANDLERS.get(task.task_type)
        if not handler:
            log.warning("Unknown task_type", task_type=task.task_type)
            return A2AResponse(
                task_id=task.task_id,
                status="rejected",
                message=f"Unknown task_type: {task.task_type}",
            )

        await handler(task.payload, sm, AGENT_URLS)
        return A2AResponse(task_id=task.task_id, status="accepted")

    except InvalidTransitionError as e:
        log.error("Invalid state transition", from_state=e.from_state, to_state=e.to_state)
        return A2AResponse(task_id=task.task_id, status="error", message=str(e))

    except A2ADispatchError as e:
        log.error("A2A dispatch failed", agent=e.agent_url, pipeline_id=e.pipeline_id)
        pipeline_id = getattr(task.payload, "pipeline_id", None) or task.payload.get("pipeline_id")
        if pipeline_id:
            await sm.transition(
                pipeline_id=pipeline_id,
                to_state=PipelineState.AGENT_UNREACHABLE,
                actor="ORCHESTRATOR",
                details={"failed_agent": e.agent_url},
            )
        return A2AResponse(task_id=task.task_id, status="error", message=str(e))

    except Exception as e:
        log.error("Orchestrator handler error", error=str(e), task_type=task.task_type)
        raise HTTPException(status_code=500, detail=str(e))


# ── Read Endpoints ─────────────────────────────────────────────────────────────

@app.get("/pipeline/{pipeline_id}")
async def get_pipeline(pipeline_id: str) -> dict:
    """Return current state of a pipeline row."""
    row = await db_state_read(pipeline_id)
    if not row:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return row


@app.get("/queue")
async def get_queue() -> list[dict]:
    """Return current priority queue (waiting orgs)."""
    from shared.db import fetch_all
    rows = await fetch_all(
        """
        SELECT pq.*, p.org_name, p.ein, p.contact_email, p.reason
        FROM priority_queue pq
        JOIN pipeline p ON pq.pipeline_id = p.id
        WHERE pq.status = 'waiting'
        ORDER BY pq.priority_score DESC, pq.entered_queue_at ASC
        """
    )
    return [dict(r) for r in rows]


@app.get("/pipeline/{pipeline_id}/audit")
async def get_audit_log(pipeline_id: str) -> list[dict]:
    """Return the full audit trail for a pipeline."""
    from shared.db import fetch_all
    rows = await fetch_all(
        "SELECT * FROM audit_log WHERE pipeline_id = $1 ORDER BY created_at ASC",
        pipeline_id,
    )
    return [dict(r) for r in rows]


@app.get("/health")
async def health() -> dict:
    """Health check — verifies DB and Redis connectivity."""
    from shared.db import fetch_val
    from shared.redis_client import get_redis

    db_ok, redis_ok = False, False
    try:
        await fetch_val("SELECT 1")
        db_ok = True
    except Exception:
        pass
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass

    return {
        "status": "healthy" if (db_ok and redis_ok) else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "agent": "orchestrator",
    }


# ── Task Handler Registry (imported from handlers.py) ─────────────────────────

TASK_HANDLERS: dict[str, Any] = {}

def _register_handlers() -> None:
    TASK_HANDLERS.update({
        TaskType.EMAIL_CLASSIFIED:   handlers.handle_email_classified,
        TaskType.EMAIL_DROPPED:      handlers.handle_email_dropped,
        TaskType.DEDUP_RESULT:       handlers.handle_dedup_result,
        TaskType.ORG_VERIFIED:       handlers.handle_org_verified,
        TaskType.ELIGIBILITY_RESULT: handlers.handle_eligibility_result,
        TaskType.ORG_SCORED:         handlers.handle_org_scored,
        TaskType.SLOT_FOUND:         handlers.handle_slot_found,
        TaskType.NO_SLOTS:           handlers.handle_no_slots,
        TaskType.PA_DECISION:        handlers.handle_pa_decision,
        TaskType.EMAIL_SENT:         handlers.handle_email_sent,
        TaskType.RSVP_OUTCOME:       handlers.handle_rsvp_outcome,
    })

_register_handlers()
