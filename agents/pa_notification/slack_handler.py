"""
Slack Interactive Action Handler — PA Notification Agent

Handles button clicks from the PA's Slack DM:
  - pa_approve → dispatches pa.decision(decision=PA_APPROVED) to Orchestrator
  - pa_reject  → opens Slack modal to collect rejection reason, then dispatches

Also verifies Slack request signatures (HMAC-SHA256).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Optional

from pydantic_settings import BaseSettings
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.models import TaskType, PADecision, PADecisionActor

log = logging.getLogger(__name__)


class SlackHandlerSettings(BaseSettings):
    SLACK_BOT_TOKEN:    str = ""
    SLACK_SIGNING_SECRET: str = ""
    ORCHESTRATOR_URL:   str = "http://localhost:8000"
    PA_SLACK_USER_ID:   str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = SlackHandlerSettings()


def verify_slack_signature(headers: dict, body: bytes) -> bool:
    """
    Verify Slack request authenticity using HMAC-SHA256 signature.
    Rejects requests older than 5 minutes.
    """
    ts = headers.get("x-slack-request-timestamp", "0")
    sig = headers.get("x-slack-signature", "")

    # Replay attack protection
    if abs(time.time() - int(ts)) > 300:
        log.warning("Slack request timestamp too old: %s", ts)
        return False

    basestring = f"v0:{ts}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        _settings.SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, sig)


async def handle_action(payload: dict) -> None:
    """
    Route Slack action to the correct handler.
    Payload is the Slack interactive payload (parsed from JSON).
    """
    actions = payload.get("actions", [])
    if not actions:
        return

    action    = actions[0]
    action_id = action.get("action_id")
    value     = action.get("value", "")
    user_id   = payload.get("user", {}).get("id", "unknown")
    channel   = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts", "")

    # Parse value: "pipeline_id|gcal_hold_id"
    parts       = value.split("|", 1)
    pipeline_id = parts[0] if parts else ""
    gcal_hold_id = parts[1] if len(parts) > 1 else ""

    log.info(
        "Slack action: action_id=%s pipeline=%s user=%s",
        action_id, pipeline_id, user_id,
    )

    if action_id == "pa_approve":
        await _handle_approve(pipeline_id, gcal_hold_id, user_id, channel, message_ts)
    elif action_id == "pa_reject":
        await _handle_reject(pipeline_id, gcal_hold_id, user_id, channel, message_ts, payload)
    else:
        log.warning("Unknown Slack action_id: %s", action_id)


async def _handle_approve(
    pipeline_id: str,
    gcal_hold_id: str,
    user_id: str,
    channel: str,
    message_ts: str,
) -> None:
    """PA clicked Approve → dispatch pa.decision(PA_APPROVED) to Orchestrator."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.PA_DECISION,
            payload={
                "pipeline_id":      pipeline_id,
                "decision":         PADecision.PA_APPROVED.value,
                "decided_by":       PADecisionActor.PA.value,
                "pa_notes":         f"Approved via Slack by {user_id}",
                "rejection_reason": None,
            },
            pipeline_id=pipeline_id,
        )
        log.info("PA APPROVED: pipeline=%s by %s", pipeline_id, user_id)

        # Update the DM to remove buttons
        _update_message(channel, message_ts, "✅ *APPROVED* — Confirmation email will be sent.")

    except A2ADispatchError as e:
        log.error("Failed to dispatch PA approval: %s", e)
        _update_message(channel, message_ts, "⚠️ Error recording approval — please contact support.")


async def _handle_reject(
    pipeline_id: str,
    gcal_hold_id: str,
    user_id: str,
    channel: str,
    message_ts: str,
    original_payload: dict,
) -> None:
    """
    PA clicked Reject → open a Slack modal to collect rejection reason,
    then dispatch pa.decision(PA_REJECTED).

    For simplicity in non-interactive contexts, dispatches immediately
    with a default rejection reason if modal trigger_id is unavailable.
    """
    trigger_id = original_payload.get("trigger_id")

    if trigger_id:
        # Open a Slack modal to collect reason
        _open_rejection_modal(trigger_id, pipeline_id, gcal_hold_id)
    else:
        # Fallback: dispatch rejection without modal (e.g., API test)
        await _dispatch_rejection(
            pipeline_id=pipeline_id,
            user_id=user_id,
            rejection_reason="Rejected via Slack (no reason provided)",
        )
        _update_message(channel, message_ts, "❌ *REJECTED* — Org removed from consideration.")


async def _dispatch_rejection(
    pipeline_id: str,
    user_id: str,
    rejection_reason: str,
) -> None:
    """Send pa.decision(PA_REJECTED) to Orchestrator."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.PA_DECISION,
            payload={
                "pipeline_id":      pipeline_id,
                "decision":         PADecision.PA_REJECTED.value,
                "decided_by":       PADecisionActor.PA.value,
                "pa_notes":         f"Rejected via Slack by {user_id}",
                "rejection_reason": rejection_reason,
            },
            pipeline_id=pipeline_id,
        )
        log.info("PA REJECTED: pipeline=%s by %s — reason: %s", pipeline_id, user_id, rejection_reason)
    except A2ADispatchError as e:
        log.error("Failed to dispatch PA rejection: %s", e)


def _open_rejection_modal(trigger_id: str, pipeline_id: str, gcal_hold_id: str) -> None:
    """Open a Slack modal dialog to collect rejection reason from the PA."""
    try:
        WebClient(token=_settings.SLACK_BOT_TOKEN).views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": f"reject_modal_{pipeline_id}",
                "title": {"type": "plain_text", "text": "Rejection Reason"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "private_metadata": f"{pipeline_id}|{gcal_hold_id}",
                "blocks": [{
                    "type": "input",
                    "block_id": "reason_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "rejection_reason",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Why are you rejecting this request?",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Rejection Reason"},
                }],
            },
        )
    except SlackApiError as e:
        log.error("Failed to open rejection modal: %s", e.response.get("error", str(e)))


def _update_message(channel: str, ts: str, message: str) -> None:
    """Update the original Slack DM to show result and remove buttons."""
    if not channel or not ts:
        return
    try:
        WebClient(token=_settings.SLACK_BOT_TOKEN).chat_update(
            channel=channel,
            ts=ts,
            text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
        )
    except SlackApiError as e:
        log.warning("Could not update Slack message: %s", e.response.get("error", str(e)))
