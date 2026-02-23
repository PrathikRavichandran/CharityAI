"""
RSVP Monitor Agent — CharityAI (Gate 9 of 9, FINAL)
Port: 8009

The last gate in the pipeline. Watches Gmail for the charity's reply
to the confirmation email and finalizes or cancels the appointment.

Logic:
  1. Poll Gmail for replies on the confirmation thread (every 5 min)
  2. Phi3 classifies the reply intent: confirmed / declined / ambiguous
  3. Confirmed → GCal: convert tentative hold to confirmed event → rsvp.outcome(FINALIZE)
  4. Declined   → GCal: delete hold event → rsvp.outcome(CANCEL_AND_PULL_NEXT)
  5. Ambiguous  → log + wait (another check cycle)
  6. Timeout (24hr) → escalate to PA, delete hold → rsvp.outcome(CANCEL_AND_PULL_NEXT)

Endpoints:
  POST /tasks    — Receive email.sent from Orchestrator (registers thread to watch)
  GET  /health   — Health check
  POST /poll     — Manual poll trigger (dev/testing)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, BackgroundTasks
from pydantic_settings import BaseSettings

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from shared.models import A2ATask, A2AResponse, TaskType
from agents.rsvp_monitor import monitor

log = structlog.get_logger()


class RSVPSettings(BaseSettings):
    GMAIL_POLL_INTERVAL_SECONDS: int = 60
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = RSVPSettings()
logging.basicConfig(level=settings.LOG_LEVEL)

_poll_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    log.info("RSVP Monitor starting up")
    await get_pool()
    await get_redis()
    _poll_task = asyncio.create_task(
        monitor.poll_loop(settings.GMAIL_POLL_INTERVAL_SECONDS)
    )
    yield
    log.info("RSVP Monitor shutting down")
    if _poll_task:
        _poll_task.cancel()
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — RSVP Monitor",
    description="Gate 9 (Final): Watches Gmail for RSVP replies, confirms or cancels GCal hold.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """
    Register a pipeline's email thread for RSVP monitoring.
    Called by Orchestrator after email.sent.
    """
    log.info("RSVP task received", task_id=task.task_id, task_type=task.task_type)

    if task.task_type != TaskType.EMAIL_SENT:
        return A2AResponse(
            task_id=task.task_id, status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    await monitor.register_thread(payload)
    return A2AResponse(task_id=task.task_id, status="accepted")


@app.post("/poll")
async def manual_poll(background_tasks: BackgroundTasks) -> dict:
    """Manually trigger one RSVP poll cycle."""
    background_tasks.add_task(monitor.run_once)
    return {"status": "RSVP poll triggered"}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "rsvp_monitor"}
