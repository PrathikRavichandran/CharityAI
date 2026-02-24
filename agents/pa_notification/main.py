"""
PA Notification Agent — CharityAI (Gate 7 of 9)
Port: 8007

Sends a rich Slack Block Kit DM to the PA with:
  - Org name, EIN, reason, verification confidence
  - Proposed appointment slot (CT display)
  - Candid profile link (if available)
  - IRS verification status
  - [✅ Approve] and [❌ Reject] interactive buttons

Also handles:
  - Auto-approve: If PA does not respond in 24 hours, Orchestrator fires
    pa_timeout → auto-approves with decision=AUTO_APPROVED
  - Slack interactive callback: /slack/actions endpoint for button clicks
  - Urgency escalation: same flow, but message is flagged URGENT 🚨

Endpoints:
  POST /tasks          — Receive slot.found or eligibility.result (urgent escalation)
  POST /slack/actions  — Slack interactive button callback
  GET  /health         — Health check
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from pydantic_settings import BaseSettings

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from shared.models import A2ATask, A2AResponse, TaskType
from shared.telemetry import setup_telemetry
from agents.pa_notification import notifier, slack_handler

log = structlog.get_logger()


class PANotifSettings(BaseSettings):
    ORCHESTRATOR_URL: str = "http://localhost:8000"
    SLACK_BOT_TOKEN:  str = ""
    SLACK_SIGNING_SECRET: str = ""
    PA_SLACK_USER_ID: str = ""
    LOG_LEVEL:        str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = PANotifSettings()
logging.basicConfig(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry("pa_notification", app)
    log.info("PA Notification Agent starting up", pa_user=settings.PA_SLACK_USER_ID)
    await get_pool()
    await get_redis()
    yield
    log.info("PA Notification Agent shutting down")
    await close_pool()
    await close_redis()


app = FastAPI(
    title="CharityAI — PA Notification Agent",
    description="Gate 7: Slack Block Kit DM to PA with Approve/Reject buttons.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/tasks", response_model=A2AResponse)
async def receive_task(task: A2ATask) -> A2AResponse:
    """
    Receive slot.found (normal approval flow) or
    eligibility.result with escalate_to_pa=True (urgent escalation).
    """
    log.info("PA Notif task received", task_id=task.task_id, task_type=task.task_type)
    payload = task.payload if isinstance(task.payload, dict) else task.payload.model_dump()

    if task.task_type == TaskType.SLOT_FOUND:
        await notifier.send_approval_dm(payload, urgent=False)

    elif task.task_type == TaskType.ELIGIBILITY_RESULT:
        # Urgent escalation — no slot yet, PA decides if exception warranted
        await notifier.send_escalation_dm(payload)

    else:
        return A2AResponse(
            task_id=task.task_id, status="rejected",
            message=f"Unexpected task_type: {task.task_type}",
        )

    return A2AResponse(task_id=task.task_id, status="accepted")


@app.post("/slack/actions")
async def slack_action_callback(request: Request) -> Response:
    """
    Slack interactive button callback endpoint.
    Verifies request signature, parses action, dispatches pa.decision to Orchestrator.
    """
    body_bytes = await request.body()
    body_str   = body_bytes.decode("utf-8")

    # Verify Slack signature
    if not slack_handler.verify_slack_signature(request.headers, body_bytes):
        log.warning("Slack signature verification failed")
        return Response(status_code=403, content="Invalid signature")

    # Parse URL-encoded payload
    from urllib.parse import parse_qs, unquote_plus
    params = parse_qs(body_str)
    raw_payload = params.get("payload", ["{}"])[0]
    payload = json.loads(raw_payload)

    await slack_handler.handle_action(payload)
    return Response(status_code=200, content="")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "agent":  "pa_notification",
        "pa_user": settings.PA_SLACK_USER_ID,
    }
