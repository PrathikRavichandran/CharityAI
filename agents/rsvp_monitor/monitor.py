"""
RSVP Monitor Logic — Gate 9 (Final)

Thread Registry: stored in Redis as a hash
  Key: rsvp:threads
  Field: pipeline_id
  Value: JSON{email_thread_id, email_id, registered_at, gcal_hold_id}

Poll loop:
  1. Read all registered threads from Redis
  2. For each thread: check Gmail for replies
  3. Phi3 classifies reply intent → confirmed / declined / ambiguous
  4. On confirmed: GCal confirm → dispatch rsvp.outcome(FINALIZE)
  5. On declined: GCal delete → dispatch rsvp.outcome(CANCEL_AND_PULL_NEXT)
  6. On timeout (>24h): same as declined
  7. Ambiguous or no reply: keep watching
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic_settings import BaseSettings

from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.db import fetch_one
from shared.models import TaskType, RSVPOutcome, RSVPNextAction
from shared.ollama_client import OllamaRouter, ModelTask, ModelFailureError
from shared.redis_client import get_redis

log = logging.getLogger(__name__)
_router = OllamaRouter()

REDIS_THREADS_KEY = "rsvp:threads"


class MonitorSettings(BaseSettings):
    GMAIL_TOKEN_PATH:      str = "auth/gmail_token.json"
    GCAL_TOKEN_PATH:       str = "auth/gcalendar_token.json"
    GCAL_CALENDAR_ID:      str = "primary"
    ORCHESTRATOR_URL:      str = "http://localhost:8000"
    RSVP_TIMEOUT_HOURS:    int = 24

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = MonitorSettings()


# ── Service Builders ──────────────────────────────────────────────────────────

def _get_gmail_service():
    token_path = Path(_settings.GMAIL_TOKEN_PATH)
    creds = Credentials.from_authorized_user_file(
        str(token_path),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_gcal_service():
    token_path = Path(_settings.GCAL_TOKEN_PATH)
    creds = Credentials.from_authorized_user_file(
        str(token_path),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


# ── Thread Registration ───────────────────────────────────────────────────────

async def register_thread(payload: dict) -> None:
    """
    Store a thread in Redis for RSVP monitoring.
    Called when email.sent is received from Orchestrator.
    """
    pipeline_id     = payload.get("pipeline_id")
    pipeline_row    = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)

    if not pipeline_row:
        log.error("Pipeline not found for RSVP registration: %s", pipeline_id)
        return

    thread_data = {
        "pipeline_id":      pipeline_id,
        "email_thread_id":  pipeline_row.get("email_thread_id"),
        "gcal_hold_id":     pipeline_row.get("gcal_hold_event_id", ""),
        "contact_email":    pipeline_row.get("contact_email", ""),
        "org_name":         pipeline_row.get("org_name", ""),
        "registered_at":    datetime.now(timezone.utc).isoformat(),
    }

    r = await get_redis()
    await r.hset(REDIS_THREADS_KEY, pipeline_id, json.dumps(thread_data))
    log.info(
        "RSVP thread registered: pipeline=%s thread=%s",
        pipeline_id, thread_data.get("email_thread_id"),
    )


async def _get_all_threads() -> dict[str, dict]:
    """Fetch all registered RSVP threads from Redis."""
    r = await get_redis()
    raw = await r.hgetall(REDIS_THREADS_KEY)
    if not raw:
        return {}
    return {k: json.loads(v) for k, v in raw.items()}


async def _deregister_thread(pipeline_id: str) -> None:
    """Remove a thread from the watch list after resolution."""
    r = await get_redis()
    await r.hdel(REDIS_THREADS_KEY, pipeline_id)
    log.info("Thread deregistered: %s", pipeline_id)


# ── Gmail Reply Fetcher ───────────────────────────────────────────────────────

def _get_reply_body(service, thread_id: str, sent_msg_id: str) -> Optional[str]:
    """
    Fetch the most recent reply in a Gmail thread that is NOT from 'me'.
    Returns the plain-text body or None if no reply found.
    """
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except HttpError as e:
        log.error("Gmail thread fetch failed %s: %s", thread_id, e)
        return None

    messages = thread.get("messages", [])
    # Skip the first message we sent — find replies (from others)
    for msg in reversed(messages):
        msg_id = msg.get("id")
        if msg_id == sent_msg_id:
            continue
        # Check label — skip our own sent messages
        labels = msg.get("labelIds", [])
        if "SENT" in labels:
            continue
        return _extract_body(msg)
    return None


def _extract_body(msg: dict) -> str:
    """Extract plain-text body from a Gmail message."""
    payload = msg.get("payload", {})

    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    body_data = payload.get("body", {}).get("data")
    if body_data:
        return _decode(body_data)

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return _decode(data)
    return ""


# ── Phi3 RSVP Intent Classifier ──────────────────────────────────────────────

RSVP_SYSTEM = """You classify email replies as RSVP responses.
Respond ONLY with valid JSON. No other text."""

RSVP_PROMPT = """Classify this email reply as an RSVP response:

Email body:
{body}

Categories:
- "confirmed": The sender is accepting / confirming the appointment
  (YES, I will attend, Looking forward, Confirmed, Sounds good, etc.)
- "declined": The sender is declining / cancelling
  (NO, Can't make it, Need to cancel, Unfortunately, regret, etc.)
- "ambiguous": Cannot determine intent (vague, off-topic, auto-reply)

Respond with JSON only:
{{"intent": "confirmed|declined|ambiguous", "confidence": 0.0-1.0, "reasoning": "short explanation"}}"""


async def classify_rsvp_intent(reply_body: str) -> dict:
    """
    Phi3 classifies the RSVP reply intent.
    Returns {"intent": str, "confidence": float, "reasoning": str}
    """
    # Fast deterministic pre-check (high-confidence keywords)
    body_lower = reply_body.lower()
    yes_words = ["yes", "confirm", "i'll be there", "looking forward", "see you", "sounds good", "i'll attend", "accepted"]
    no_words  = ["no", "cancel", "can't", "cannot", "decline", "won't", "unfortunately", "regret", "not able"]

    for w in yes_words:
        if w in body_lower:
            return {"intent": "confirmed", "confidence": 0.95, "reasoning": f"Contains '{w}'"}
    for w in no_words:
        if w in body_lower:
            return {"intent": "declined",  "confidence": 0.90, "reasoning": f"Contains '{w}'"}

    # Delegate to Phi3 for ambiguous/nuanced cases
    try:
        raw = await _router.complete(
            task=ModelTask.EMAIL_CLASSIFY,
            prompt=RSVP_PROMPT.format(body=reply_body[:800]),
            system=RSVP_SYSTEM,
            temperature=0.1,
            max_tokens=150,
        )
        clean = re.sub(r"```(?:json)?", "", raw).strip()
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "intent":     data.get("intent", "ambiguous"),
                "confidence": float(data.get("confidence", 0.5)),
                "reasoning":  data.get("reasoning", ""),
            }
    except (ModelFailureError, Exception) as e:
        log.warning("RSVP classification failed: %s — treating as ambiguous", e)

    return {"intent": "ambiguous", "confidence": 0.0, "reasoning": "Classification failed"}


# ── GCal Hold Management ──────────────────────────────────────────────────────

def _confirm_gcal_event(gcal_hold_id: str, org_name: str) -> Optional[str]:
    """Convert a tentative hold to a confirmed event. Returns new event ID."""
    try:
        service = _get_gcal_service()
        event = service.events().get(
            calendarId=_settings.GCAL_CALENDAR_ID,
            eventId=gcal_hold_id,
        ).execute()

        event["status"]      = "confirmed"
        event["summary"]     = f"Charity Meeting — {org_name}"
        event["description"] = event.get("description", "").replace("[HOLD] ", "")
        event["colorId"]     = "2"    # Sage green — confirmed
        event["transparency"] = "opaque"

        updated = service.events().update(
            calendarId=_settings.GCAL_CALENDAR_ID,
            eventId=gcal_hold_id,
            body=event,
        ).execute()
        return updated.get("id")
    except HttpError as e:
        log.error("GCal confirm failed for %s: %s", gcal_hold_id, e)
        return None


def _delete_gcal_hold(gcal_hold_id: str) -> bool:
    """Delete the tentative hold event. Returns True on success."""
    if not gcal_hold_id or gcal_hold_id == "EXCEPTION":
        return True
    try:
        _get_gcal_service().events().delete(
            calendarId=_settings.GCAL_CALENDAR_ID,
            eventId=gcal_hold_id,
        ).execute()
        return True
    except HttpError as e:
        if e.resp.status == 410:    # Already deleted
            return True
        log.error("GCal delete hold failed for %s: %s", gcal_hold_id, e)
        return False


# ── Outcome Dispatcher ────────────────────────────────────────────────────────

async def _dispatch_outcome(
    pipeline_id: str,
    outcome: RSVPOutcome,
    next_action: RSVPNextAction,
    gcal_event_id: Optional[str],
    rsvp_received_at: Optional[str],
) -> None:
    """Send rsvp.outcome to the Orchestrator."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.RSVP_OUTCOME,
            payload={
                "pipeline_id":      pipeline_id,
                "outcome":          outcome.value,
                "next_action":      next_action.value,
                "gcal_event_id":    gcal_event_id,
                "rsvp_received_at": rsvp_received_at,
            },
            pipeline_id=pipeline_id,
        )
        log.info(
            "rsvp.outcome dispatched: pipeline=%s outcome=%s next_action=%s",
            pipeline_id, outcome.value, next_action.value,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch rsvp.outcome: %s", e)


# ── Core Processing ───────────────────────────────────────────────────────────

def _compute_thread_age_hours(registered_at_iso: str) -> float:
    """Return how many hours ago a thread was registered."""
    registered_at = datetime.fromisoformat(registered_at_iso)
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - registered_at).total_seconds() / 3600


async def process_thread(thread_data: dict) -> None:
    """
    Process a single monitored RSVP thread:
    check for reply → classify → finalize or cancel.
    """
    pipeline_id  = thread_data["pipeline_id"]
    thread_id    = thread_data.get("email_thread_id")
    gcal_hold_id = thread_data.get("gcal_hold_id", "")
    org_name     = thread_data.get("org_name", "")
    registered_at_str = thread_data.get("registered_at", "")

    # ── Timeout check ──────────────────────────────────────────────────────────
    if registered_at_str:
        age_hours = _compute_thread_age_hours(registered_at_str)
        if age_hours > _settings.RSVP_TIMEOUT_HOURS:
            log.info("RSVP timeout for pipeline %s (%.1fh)", pipeline_id, age_hours)
            _delete_gcal_hold(gcal_hold_id)
            await _dispatch_outcome(
                pipeline_id=pipeline_id,
                outcome=RSVPOutcome.TIMEOUT,
                next_action=RSVPNextAction.CANCEL_AND_PULL_NEXT,
                gcal_event_id=None,
                rsvp_received_at=None,
            )
            await _deregister_thread(pipeline_id)
            return

    # ── Fetch pipeline row for original email_id ──────────────────────────────
    pipeline_row = await fetch_one("SELECT email_id FROM pipeline WHERE id = $1", pipeline_id)
    original_email_id = (pipeline_row or {}).get("email_id", "")

    # ── Check Gmail for reply ─────────────────────────────────────────────────
    try:
        gmail = _get_gmail_service()
    except Exception as e:
        log.error("Gmail auth failed for RSVP check: %s", e)
        return

    if not thread_id:
        log.warning("No thread_id for pipeline %s — skipping", pipeline_id)
        return

    reply_body = _get_reply_body(gmail, thread_id, original_email_id)
    if not reply_body:
        log.debug("No reply yet for pipeline %s", pipeline_id)
        return

    log.info("Reply found for pipeline %s — classifying RSVP intent", pipeline_id)

    # ── Classify intent ───────────────────────────────────────────────────────
    classification = await classify_rsvp_intent(reply_body)
    intent     = classification["intent"]
    confidence = classification["confidence"]

    log.info(
        "RSVP intent=%s confidence=%.2f pipeline=%s",
        intent, confidence, pipeline_id,
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    if intent == "confirmed" and confidence >= 0.7:
        # ── CONFIRMED: finalize GCal event ───────────────────────────────────
        confirmed_event_id = _confirm_gcal_event(gcal_hold_id, org_name)
        await _dispatch_outcome(
            pipeline_id=pipeline_id,
            outcome=RSVPOutcome.CONFIRMED,
            next_action=RSVPNextAction.FINALIZE,
            gcal_event_id=confirmed_event_id or gcal_hold_id,
            rsvp_received_at=now_iso,
        )
        await _deregister_thread(pipeline_id)

    elif intent == "declined" and confidence >= 0.7:
        # ── DECLINED: delete GCal hold, pull next from queue ─────────────────
        _delete_gcal_hold(gcal_hold_id)
        await _dispatch_outcome(
            pipeline_id=pipeline_id,
            outcome=RSVPOutcome.DECLINED,
            next_action=RSVPNextAction.CANCEL_AND_PULL_NEXT,
            gcal_event_id=None,
            rsvp_received_at=now_iso,
        )
        await _deregister_thread(pipeline_id)

    else:
        # ── AMBIGUOUS or low confidence: keep watching ────────────────────────
        log.info(
            "RSVP reply ambiguous (intent=%s conf=%.2f) — continuing to watch pipeline %s",
            intent, confidence, pipeline_id,
        )


# ── Poll Loop ─────────────────────────────────────────────────────────────────

async def run_once() -> None:
    """Process all registered RSVP threads once."""
    threads = await _get_all_threads()
    if not threads:
        log.debug("No RSVP threads registered — nothing to do")
        return

    log.info("RSVP poll: checking %d thread(s)", len(threads))
    for pipeline_id, thread_data in threads.items():
        try:
            await process_thread(thread_data)
        except Exception as e:
            log.error("RSVP processing failed for pipeline %s: %s", pipeline_id, e)
        await asyncio.sleep(0.3)   # Small delay between threads


async def poll_loop(interval_seconds: int) -> None:
    """Background loop — runs run_once() every interval_seconds."""
    log.info("RSVP poll loop started (interval=%ds)", interval_seconds)
    while True:
        await run_once()
        await asyncio.sleep(interval_seconds)
