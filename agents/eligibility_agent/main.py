"""
Eligibility Agent — CharityAI (Gate 4 of 9)
Port: 8004

Enforces the 90-day rolling window rule:
  - One appointment per organization (by EIN) per 90 days
  - Eligibility determined by a single SQL COUNT query
  - Urgent repeat claims → always escalated to PA (never auto-dropped)
  - No LLM — deterministic DB logic only

SQL Rule:
  SELECT COUNT(*) FROM appointment_history
  WHERE ein = $1 AND scheduled_at > NOW() - INTERVAL '90 days'
  COUNT > 0 → ineligible

Endpoints:
  POST /tasks   — Receive org.verified from Orchestrator
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
from shared.models import A2ATask, A2AResponse, TaskType
from shared.telemetry import setup_telemetry
from agents.eligibility_agent import eligibility_logic

log = structlog.get_logger()


class EligibilitySettings(BaseSettings):
    ORCHESTRATOR_URL:        str = "http://localhost:8000"
    ELIGIBILITY_WINDOW_DAYS: int = 90
    LOG_LEVEL:               str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = EligibilitySettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry("eligibility_agent", app)
    log.info("Eligibility Agent starting", window_days=settings.ELIGIBILITY_WINDOW_DAYS)
    await get_pool()
    await get_redis()
    yield
    log.info("Eligibility Agent shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Eligibility Agent",
    description="Gate 4: 90-day rolling window rule. No LLM — deterministic SQL.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """Process an org.verified task and check 90-day eligibility."""
    log.info("Eligibility task received", task_id=task.task_id)

    if task.task_type != TaskType.ORG_VERIFIED:
        return A2AResponse(
            task_id=task.task_id,
            status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    await eligibility_logic.check(payload)
    return A2AResponse(task_id=task.task_id, status="accepted")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "agent":  "eligibility_agent",
        "rule":   "1 appointment per EIN per 90-day rolling window",
    }
