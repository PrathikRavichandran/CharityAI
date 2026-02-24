"""
Calendar Agent — CharityAI (Gate 6 of 9)
Port: 8006

Finds and holds the best available appointment slot using Google Calendar API.

Logic:
  1. Query GCal free/busy for 14–28 days ahead (Mon–Fri, 1–5 PM CT)
  2. Find first 30-minute window that is free
  3. Create a tentative hold event (status=tentative) on GCal
  4. Dispatch slot.found to Orchestrator with proposed slot + GCal hold ID
  5. If no slots available → dispatch no.slots (org stays in queue)

30-minute slots, Mon–Fri, 1:00–5:00 PM CT (configurable via .env)
Look-ahead window: GCAL_SLOT_MIN_DAYS to GCAL_SLOT_MAX_DAYS

Endpoints:
  POST /tasks   — Receive org.scored (or pipeline_id + ein for re-queue)
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
from agents.calendar_agent import slot_finder

log = structlog.get_logger()


class CalendarSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    LOG_LEVEL:        str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = CalendarSettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry("calendar_agent", app)
    log.info("Calendar Agent starting up")
    await get_pool()
    await get_redis()
    yield
    log.info("Calendar Agent shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — Calendar Agent",
    description="Gate 6: GCal free/busy query, slot selection, tentative hold creation.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """Find a slot for an org. Accepts org.scored or any task with pipeline_id + ein."""
    log.info("Calendar task received", task_id=task.task_id, task_type=task.task_type)

    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()
    await slot_finder.find_and_hold(payload)
    return A2AResponse(task_id=task.task_id, status="accepted")


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "calendar_agent"}
