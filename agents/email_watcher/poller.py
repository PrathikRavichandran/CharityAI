"""
Gmail polling module for Email Watcher Agent.

Fetches unread emails from Gmail, routes each through classification
and extraction, then dispatches valid results to the Orchestrator.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic_settings import BaseSettings

from agents.email_watcher import classifier, extractor
from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.models import TaskType, EmailDroppedPayload
from shared.db import execute

log = logging.getLogger(__name__)


class GmailSettings(BaseSettings):
    GMAIL_CREDENTIALS_PATH: str = "auth/gmail_oauth.json"
    GMAIL_TOKEN_PATH:        str = "auth/gmail_token.json"
    ORCHESTRATOR_URL:        str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        extra = "ignore"


import time

_settings = GmailSettings()
_START_TIME = int(time.time())


def _get_gmail_service():
    """Build and return an authenticated Gmail API service."""
    token_path = Path(_settings.GMAIL_TOKEN_PATH)
    if not token_path.exists():
        raise RuntimeError(
            f"Gmail token not found at {token_path}. "
            "Run: python scripts/generate_tokens.py"
        )
    creds = Credentials.from_authorized_user_file(
        str(token_path),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _extract_email_body(msg: dict) -> str:
    """Extract plain-text body from a Gmail message dict."""
    payload = msg.get("payload", {})

    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    # Simple body (no parts)
    body_data = payload.get("body", {}).get("data")
    if body_data:
        return _decode(body_data)

    # Multipart — find text/plain part
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return _decode(data)
        # Nested multipart
        for subpart in part.get("parts", []):
            if subpart.get("mimeType") == "text/plain":
                data = subpart.get("body", {}).get("data")
                if data:
                    return _decode(data)
    return ""


def _get_header(msg: dict, name: str) -> str:
    """Extract a specific header value from a Gmail message."""
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


async def process_message(service, msg_meta: dict) -> None:
    """
    Process a single Gmail message through the full Gate 1 pipeline:
    classify → extract → dispatch or drop.
    """
    msg_id    = msg_meta["id"]
    thread_id = msg_meta.get("threadId", msg_id)

    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except HttpError as e:
        log.error("Failed to fetch Gmail message %s: %s", msg_id, e)
        return

    subject    = _get_header(msg, "Subject")
    from_addr  = _get_header(msg, "From")
    body       = _extract_email_body(msg)
    snippet    = body[:500]

    log.info("Processing email: id=%s subject='%s'", msg_id, subject[:60])

    # ── Step 1: Classify (Phi3) ────────────────────────────────────────────────
    classification = await classifier.classify_email(subject, body, from_addr)

    if not classification["is_charity"]:
        log.info("DROP (not charity): %s", msg_id)
        # Silent drop — just mark as read, no DB log needed
        try:
            service.users().messages().modify(
                userId="me", id=msg_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except HttpError:
            pass
        return

    # ── Step 2: Extract structured fields (Mistral) ────────────────────────────
    extracted = await extractor.extract_fields(subject, body, from_addr)

    # Validate required fields
    missing = []
    if not extracted.get("ein"):
        missing.append("ein")
    if not extracted.get("org_name"):
        missing.append("org_name")
    if not extracted.get("reason"):
        missing.append("reason")

    if missing:
        log.info("DROP (missing fields %s): %s", missing, msg_id)
        drop_reason = f"missing_{missing[0]}"  # log first missing field
        await _log_drop(msg_id, drop_reason, subject, extracted.get("org_name"), extracted.get("ein"), snippet)
        try:
            service.users().messages().modify(
                userId="me", id=msg_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except HttpError:
            pass
        return

    # ── Step 3: Dispatch to Orchestrator ──────────────────────────────────────
    payload = {
        "email_id":              msg_id,
        "email_thread_id":       thread_id,
        "org_name":              extracted["org_name"],
        "ein":                   extracted["ein"],
        "reason":                extracted["reason"],
        "urgency_signals":       extracted.get("urgency_signals", []),
        "contact_email":         extracted.get("contact_email") or from_addr,
        "received_at":           datetime.utcnow().isoformat(),
        "classifier_confidence": classification.get("confidence", 0.9),
        "raw_subject":           subject,
    }

    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.EMAIL_CLASSIFIED,
            payload=payload,
        )
        log.info(
            "Dispatched to orchestrator: org='%s' ein=%s",
            extracted["org_name"], extracted["ein"],
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch to orchestrator: %s", e)
        return

    # Mark email as read after successful dispatch
    try:
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as e:
        log.warning("Could not mark email as read: %s", e)


async def _log_drop(
    email_id: str, drop_reason: str,
    raw_subject: str, org_name: Optional[str],
    ein: Optional[str], raw_snippet: str,
) -> None:
    """Write a drop record to the dropped_emails table."""
    try:
        await execute(
            """
            INSERT INTO dropped_emails
                (email_id, drop_reason, raw_subject, org_name_extracted, ein_extracted, raw_snippet)
            VALUES ($1, $2::drop_reason, $3, $4, $5, $6)
            """,
            email_id, drop_reason, raw_subject[:200],
            org_name, ein, raw_snippet,
        )
    except Exception as e:
        log.error("Failed to log drop for %s: %s", email_id, e)


async def run_once() -> None:
    """Fetch and process all unread Gmail messages once."""
    log.info("Gmail poll cycle starting")
    try:
        service = _get_gmail_service()
    except Exception as e:
        log.error("Gmail authentication failed: %s", e)
        return

    try:
        result = service.users().messages().list(
            userId="me",
            labelIds=["UNREAD", "INBOX"],
            q=f"after:{_START_TIME}",
            maxResults=50,
        ).execute()
    except HttpError as e:
        log.error("Gmail list failed: %s", e)
        return

    messages = result.get("messages", [])
    log.info("Found %d unread messages", len(messages))

    for msg_meta in messages:
        await process_message(service, msg_meta)
        await asyncio.sleep(0.5)   # Small delay to avoid rate limiting


async def poll_loop(interval_seconds: int) -> None:
    """Background loop — runs run_once() every interval_seconds."""
    log.info("Gmail poll loop started (interval=%ds)", interval_seconds)
    while True:
        await run_once()
        log.info("Poll cycle done — sleeping %ds", interval_seconds)
        await asyncio.sleep(interval_seconds)
