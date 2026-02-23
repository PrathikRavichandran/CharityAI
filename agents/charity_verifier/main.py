"""
Charity Verifier Agent — CharityAI (Gate 3 of 9)
Port: 8003

CURRENT MODE: Manual Verification Mode (Candid API pending)
  - IRS Tax-Exempt Search API as primary source
  - Web search (DuckDuckGo) as secondary fallback
  - ALL orgs flagged LOW confidence → PA must manually verify
  - Candid tools are stubs — zero pipeline changes when API key arrives

When Candid API key is in .env and CANDID_MODE=live, it becomes
the primary source automatically — no code changes needed.

Endpoints:
  POST /tasks   — Receive dedup.result from Orchestrator
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
from agents.charity_verifier import verifier

log = structlog.get_logger()


class VerifierSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    CANDID_MODE:      str = "stub"
    LOG_LEVEL:        str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = VerifierSettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mode = "MANUAL (IRS + web)" if settings.CANDID_MODE == "stub" else "LIVE (Candid + IRS)"
    log.info("Charity Verifier starting", mode=mode)
    await get_pool()
    await get_redis()
    yield
    log.info("Charity Verifier shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Charity Verifier",
    description="Gate 3: IRS + web search verification (manual mode). Candid-ready stub.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """Process a dedup.result task — verify the org and dispatch org.verified."""
    log.info("Verifier task received", task_id=task.task_id, task_type=task.task_type)

    if task.task_type != TaskType.DEDUP_RESULT:
        return A2AResponse(
            task_id=task.task_id,
            status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    await verifier.verify(payload)
    return A2AResponse(task_id=task.task_id, status="accepted")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "agent": "charity_verifier",
        "mode": settings.CANDID_MODE,
    }
