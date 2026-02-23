"""
Email Composer Logic — Gate 8

Drafts and sends emails via Gmail API:
  - send_confirmation: Warm confirmation + RSVP request (Mistral-drafted)
  - send_rejection:    Polite decline with reason (Mistral-drafted)

Both emails are plain-text to maximize deliverability.
Mistral generates the body — the subject line is deterministic.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic_settings import BaseSettings

from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.db import fetch_one
from shared.models import TaskType
from shared.ollama_client import OllamaRouter, ModelTask, ModelFailureError

log = logging.getLogger(__name__)
_router = OllamaRouter()


class ComposerSettings(BaseSettings):
    GMAIL_TOKEN_PATH:     str = "auth/gmail_token.json"
    GMAIL_SCOPES:         str = "https://www.googleapis.com/auth/gmail.send"
    ORCHESTRATOR_URL:     str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = ComposerSettings()

# ── Gmail Auth ────────────────────────────────────────────────────────────────

def _get_gmail_service():
    token_path = Path(_settings.GMAIL_TOKEN_PATH)
    if not token_path.exists():
        raise RuntimeError(f"Gmail token not found at {token_path}")
    creds = Credentials.from_authorized_user_file(
        str(token_path),
        scopes=[_settings.GMAIL_SCOPES],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _send_email(to: str, subject: str, body: str) -> Optional[str]:
    """Send an email via Gmail API. Returns the Gmail message ID."""
    message = MIMEText(body, "plain")
    message["to"]      = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        service = _get_gmail_service()
        sent    = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return sent.get("id")
    except HttpError as e:
        log.error("Gmail send failed: %s", e)
        return None


# ── Mistral Email Drafters ────────────────────────────────────────────────────

async def _draft_confirmation(
    org_name: str,
    contact_name: str,
    slot_display: str,
    reason: str,
) -> str:
    """Use Mistral to draft a warm appointment confirmation email body."""
    prompt = f"""Draft a warm, professional appointment confirmation email for a charity organization.

Details:
- Organization: {org_name}
- Contact: {contact_name}
- Appointment slot: {slot_display}
- Their request summary: {reason[:200]}

Requirements:
- Warm and encouraging tone
- Confirm the appointment slot clearly
- Ask them to reply YES to confirm or NO to decline (for scheduling purposes)
- Keep it under 200 words
- Do NOT include subject line — body only
- Do NOT use placeholders like [Name] — use the actual values provided
- Sign off as "CharityAI Scheduling Team"

Write body only, no subject line:"""

    try:
        body = await _router.complete(
            task=ModelTask.EMAIL_COMPOSE,
            prompt=prompt,
            temperature=0.5,
            max_tokens=400,
        )
        return body.strip()
    except (ModelFailureError, Exception) as e:
        log.warning("Mistral email drafting failed: %s — using template", e)
        return (
            f"Dear {contact_name},\n\n"
            f"We are pleased to confirm your appointment request on behalf of {org_name}.\n\n"
            f"Your appointment is scheduled for: {slot_display}\n\n"
            f"Please reply YES to confirm or NO to decline at your earliest convenience.\n\n"
            f"Best regards,\nCharityAI Scheduling Team"
        )


async def _draft_rejection(
    org_name: str,
    contact_name: str,
    rejection_reason: str,
) -> str:
    """Use Mistral to draft a polite rejection email body."""
    prompt = f"""Draft a kind, empathetic rejection email for a charity appointment request.

Details:
- Organization: {org_name}
- Contact: {contact_name}
- Reason given by PA (for internal context only, do NOT quote verbatim): {rejection_reason[:150]}

Requirements:
- Empathetic and respectful tone
- Express gratitude for reaching out
- Gently decline without giving the specific internal reason
- Encourage them to reach out again in the future
- Keep it under 150 words
- Sign off as "CharityAI Scheduling Team"

Write body only, no subject line:"""

    try:
        body = await _router.complete(
            task=ModelTask.EMAIL_COMPOSE,
            prompt=prompt,
            temperature=0.5,
            max_tokens=300,
        )
        return body.strip()
    except (ModelFailureError, Exception) as e:
        log.warning("Mistral rejection drafting failed: %s — using template", e)
        return (
            f"Dear {contact_name},\n\n"
            f"Thank you sincerely for reaching out on behalf of {org_name}.\n\n"
            f"After careful consideration, we are unable to schedule a meeting at this time. "
            f"We deeply appreciate the important work you do and encourage you to reach out again.\n\n"
            f"Warm regards,\nCharityAI Scheduling Team"
        )


# ── Main Composer Functions ───────────────────────────────────────────────────

async def send_confirmation(payload: dict) -> None:
    """Draft and send appointment confirmation email, dispatch email.sent."""
    pipeline_id = payload.get("pipeline_id")

    row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
    if not row:
        log.error("Pipeline not found for confirmation: %s", pipeline_id)
        return

    org_name      = row.get("org_name", "Your Organization")
    contact_email = row.get("contact_email", "")
    reason        = row.get("reason", "")
    proposed_slot = row.get("proposed_slot")

    if not contact_email:
        log.error("No contact_email for pipeline %s", pipeline_id)
        return

    # Derive contact name from email (e.g., "grants@feedingamerica.org" → "Grants Team")
    contact_name = _derive_contact_name(contact_email, org_name)

    # Format slot for display
    slot_display = "TBD"
    if proposed_slot:
        try:
            dt = datetime.fromisoformat(str(proposed_slot))
            import pytz
            ct = pytz.timezone("America/Chicago")
            dt_ct = dt.astimezone(ct)
            slot_display = dt_ct.strftime("%A, %B %-d at %-I:%M %p Central Time")
        except Exception:
            slot_display = str(proposed_slot)

    subject = f"Appointment Confirmed — {org_name}"
    body    = await _draft_confirmation(org_name, contact_name, slot_display, reason)

    log.info("Sending confirmation to %s for pipeline %s", contact_email, pipeline_id)
    msg_id = _send_email(contact_email, subject, body)

    if msg_id:
        log.info("Confirmation sent: gmail_msg_id=%s", msg_id)
        await _dispatch_email_sent(pipeline_id, "confirmation_rsvp", contact_email)
    else:
        log.error("Confirmation email failed to send for pipeline %s", pipeline_id)


async def send_rejection(payload: dict) -> None:
    """Draft and send rejection email."""
    pipeline_id      = payload.get("pipeline_id")
    rejection_reason = payload.get("rejection_reason", "Not specified")

    row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
    if not row:
        log.error("Pipeline not found for rejection: %s", pipeline_id)
        return

    org_name      = row.get("org_name", "Your Organization")
    contact_email = row.get("contact_email", "")

    if not contact_email:
        log.error("No contact_email for rejection pipeline %s", pipeline_id)
        return

    contact_name = _derive_contact_name(contact_email, org_name)
    subject      = f"Regarding Your Meeting Request — {org_name}"
    body         = await _draft_rejection(org_name, contact_name, rejection_reason)

    log.info("Sending rejection to %s for pipeline %s", contact_email, pipeline_id)
    msg_id = _send_email(contact_email, subject, body)

    if msg_id:
        log.info("Rejection sent: gmail_msg_id=%s", msg_id)
        await _dispatch_email_sent(pipeline_id, "rejection_notice", contact_email)
    else:
        log.error("Rejection email failed for pipeline %s", pipeline_id)


async def _dispatch_email_sent(
    pipeline_id: Optional[str], template: str, recipient: str
) -> None:
    """Dispatch email.sent to Orchestrator."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.EMAIL_SENT,
            payload={
                "pipeline_id": pipeline_id,
                "template":    template,
                "recipient":   recipient,
                "sent_at":     datetime.utcnow().isoformat(),
            },
            pipeline_id=pipeline_id,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch email.sent: %s", e)


def _derive_contact_name(email: str, org_name: str) -> str:
    """
    Derive a greeting name from email address.
    'grants@feedingamerica.org' → 'Grants Team'
    'john.doe@org.org' → 'John'
    Falls back to org_name if can't parse.
    """
    local = email.split("@")[0] if "@" in email else email
    # Handle 'john.doe' or 'john_doe' style
    parts = re.split(r"[._-]", local)
    if len(parts) >= 2 and all(p.isalpha() for p in parts[:2]):
        return parts[0].capitalize()
    elif len(parts) == 1 and parts[0].isalpha():
        return f"{parts[0].capitalize()} Team"
    return f"{org_name} Team"
