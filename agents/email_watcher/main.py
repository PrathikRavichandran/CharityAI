"""
Email Watcher Agent — CharityAI (Gate 1 of 9)
Port: 8001

Responsibilities:
  1. Poll Gmail inbox every 5 minutes for unread emails
  2. Classify each email as charity vs. not-charity (Phi3)
  3. Extract structured fields from charity emails (Mistral)
  4. Silent-drop non-charity emails
  5. Log-drop emails missing required fields (EIN, org name, reason)
  6. Dispatch valid classified emails to Orchestrator via A2A

Endpoints:
  GET  /health             — Health check
  POST /tasks              — Receive manual trigger or test tasks
  POST /poll               — Manually trigger one poll cycle (dev/testing)
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
from shared.models import A2ATask, A2AResponse
from shared.telemetry import setup_telemetry
from agents.email_watcher import classifier, extractor, poller

log = structlog.get_logger()


class EmailWatcherSettings(BaseSettings):
    GMAIL_POLL_INTERVAL_SECONDS: int = 60
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = EmailWatcherSettings()
logging.basicConfig(level=settings.LOG_LEVEL)

_poll_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    setup_telemetry("email_watcher", app)
    log.info("Email Watcher starting up")
    await get_pool()
    await get_redis()
    # Start background polling loop
    _poll_task = asyncio.create_task(poller.poll_loop(settings.GMAIL_POLL_INTERVAL_SECONDS))
    yield
    log.info("Email Watcher shutting down")
    if _poll_task:
        _poll_task.cancel()
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Email Watcher",
    description="Gate 1: Polls Gmail, classifies, extracts, dispatches to Orchestrator.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "agent": "email_watcher",
        "poll_interval_sec": settings.GMAIL_POLL_INTERVAL_SECONDS,
    }


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """Receive manual trigger tasks (used for testing)."""
    log.info("Task received", task_type=task.task_type)
    return A2AResponse(task_id=task.task_id, status="accepted", message="Noted")


@app.post("/poll")
async def manual_poll(background_tasks: BackgroundTasks) -> dict:
    """Manually trigger one Gmail poll cycle. Useful during dev/testing."""
    background_tasks.add_task(poller.run_once)
    return {"status": "poll triggered", "message": "Check logs for results"}
