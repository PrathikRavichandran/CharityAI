"""
Email Composer Agent — CharityAI (Gate 8 of 9)
Port: 8008

Drafts and sends appointment confirmation emails to charity contacts.

Steps:
  1. Fetch pipeline + org context from DB
  2. Mistral drafts a warm, professional confirmation email
  3. Gmail API sends the email from the executive's account
  4. Dispatches email.sent to Orchestrator

Email types:
  - confirmation_rsvp: Confirm appointment + request RSVP (main flow)
  - rejection_notice:  Polite decline for PA-rejected orgs

Endpoints:
  POST /tasks   — Receive pa.decision from Orchestrator
  GET  /health  — Health check
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from pydantic_settings import BaseSettings

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from shared.models import A2ATask, A2AResponse, TaskType, PADecision
from shared.telemetry import setup_telemetry
from agents.email_composer import composer

log = structlog.get_logger()


class ComposerSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    LOG_LEVEL:        str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = ComposerSettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry("email_composer", app)
    log.info("Email Composer starting up")
    await get_pool()
    await get_redis()
    yield
    log.info("Email Composer shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Email Composer",
    description="Gate 8: Mistral-drafted Gmail confirmation / rejection emails.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """Receive pa.decision and send the appropriate email."""
    log.info("Email Composer task received", task_id=task.task_id)

    if task.task_type != TaskType.PA_DECISION:
        return A2AResponse(
            task_id=task.task_id,
            status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    decision = payload.get("decision")

    if decision in (PADecision.PA_APPROVED.value, PADecision.AUTO_APPROVED.value):
        await composer.send_confirmation(payload)
    elif decision == PADecision.PA_REJECTED.value:
        await composer.send_rejection(payload)
    else:
        log.warning("Unknown decision: %s", decision)

    return A2AResponse(task_id=task.task_id, status="accepted")


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "email_composer"}
