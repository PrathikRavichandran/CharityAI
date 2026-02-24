"""
Dedup Guard Agent — CharityAI (Gate 2 of 9)
Port: 8002

Responsibilities:
  1. Check if this exact email (by body hash) already exists in the pipeline
  2. Check if the same org (by EIN) already has an active pipeline entry
     - If yes → merge (update existing pipeline, don't create duplicate)
  3. Pass genuinely new records to Orchestrator for verification

No LLM. Pure DB logic.

Endpoints:
  POST /tasks   — Receive email.classified from Orchestrator
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
from agents.dedup_guard import dedup_logic

log = structlog.get_logger()


class DedupSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    LOG_LEVEL:        str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = DedupSettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry("dedup_guard", app)
    log.info("Dedup Guard starting up")
    await get_pool()
    await get_redis()
    yield
    log.info("Dedup Guard shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Dedup Guard",
    description="Gate 2: Blocks duplicate emails and merges repeat org submissions.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """
    Process an email.classified task from the Orchestrator.
    Runs dedup checks and dispatches result back to Orchestrator.
    """
    log.info("Dedup task received", task_id=task.task_id, task_type=task.task_type)

    if task.task_type != TaskType.EMAIL_CLASSIFIED:
        return A2AResponse(
            task_id=task.task_id,
            status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    await dedup_logic.process(payload)
    return A2AResponse(task_id=task.task_id, status="accepted")


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "dedup_guard"}
