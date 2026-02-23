"""
Prioritizer Agent — CharityAI (Gate 5 of 9)
Port: 8005

Scores every eligible org on a 0–100 scale using:
  - Urgency signals (deterministic, 0–25 pts)
  - Impact magnitude  (Llama3 LLM reasoning, 0–25 pts)
  - Org reputation / cause area (Llama3, 0–25 pts)
  - Time-in-queue bonus (weekly bump, deterministic, 0–25 pts)

Score is stored in priority_queue. Orgs compete for the single
available appointment slot. Highest score = first offered.

Weekly bump: every 7 days in queue, +5 pts (max +25 cap),
ensuring no org waits forever.

Endpoints:
  POST /tasks   — Receive eligibility.result from Orchestrator
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
from agents.prioritizer import scorer

log = structlog.get_logger()


class PrioritizerSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    LOG_LEVEL:        str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = PrioritizerSettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Prioritizer starting up")
    await get_pool()
    await get_redis()
    yield
    log.info("Prioritizer shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Prioritizer",
    description="Gate 5: Multi-factor scoring (0–100) with Llama3. Feeds the priority queue.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """Score an eligible org and add it to the priority queue."""
    log.info("Prioritizer task received", task_id=task.task_id)

    if task.task_type != TaskType.ELIGIBILITY_RESULT:
        return A2AResponse(
            task_id=task.task_id,
            status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    await scorer.score_and_queue(payload)
    return A2AResponse(task_id=task.task_id, status="accepted")


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "prioritizer"}
