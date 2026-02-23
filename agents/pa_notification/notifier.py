"""
PA Notifier — Slack Block Kit DM sender.

Builds and sends rich Slack messages with interactive Approve/Reject buttons.
Uses the Slack Web API via slack_sdk.WebClient.

Message types:
  1. Approval DM (slot.found)     — Normal flow, has slot time
  2. Escalation DM (urgency)      — 🚨 URGENT header, no slot yet
"""

from __future__ import annotations

import logging
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pydantic_settings import BaseSettings

from shared.db import fetch_one

log = logging.getLogger(__name__)


class NotifierSettings(BaseSettings):
    SLACK_BOT_TOKEN:  str = ""
    PA_SLACK_USER_ID: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = NotifierSettings()


def _client() -> WebClient:
    return WebClient(token=_settings.SLACK_BOT_TOKEN)


async def send_approval_dm(payload: dict, urgent: bool = False) -> None:
    """
    Send a Slack Block Kit DM to the PA for appointment approval.

    Includes:
      - Org summary card
      - Proposed slot time (CT)
      - Verification sources
      - Candid link (if available)
      - [✅ Approve] / [❌ Reject] interactive buttons
    """
    pipeline_id = payload.get("pipeline_id")

    # Fetch full context from DB
    row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
    org_row = None
    if row and row.get("ein"):
        org_row = await fetch_one(
            "SELECT * FROM organizations WHERE ein = $1", row["ein"]
        )

    if not row:
        log.error("Pipeline not found for PA DM: %s", pipeline_id)
        return

    org_name    = row.get("org_name", "Unknown Org")
    ein         = row.get("ein", "N/A")
    reason      = row.get("reason", "No reason provided")
    urgency_sigs = row.get("urgency_signals") or []

    proposed_slot = payload.get("proposed_slot", {})
    display_time  = proposed_slot.get("display_ct", "TBD")
    gcal_hold_id  = proposed_slot.get("gcal_hold_id", "")

    candid_url    = (org_row or {}).get("candid_profile_url")
    confidence    = (org_row or {}).get("verification_confidence", "low")
    irs_ok        = (org_row or {}).get("irs_verified", False)

    confidence_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴", "failed": "⛔"}.get(
        confidence, "🔴"
    )

    header_text = (
        f"🚨 *URGENT Charity Request — PA Action Required*"
        if urgent
        else f"📋 *New Charity Request — PA Approval Required*"
    )

    urgency_line = ""
    if urgency_sigs:
        urgency_line = f"\n⚡ *Urgency Signals:* {', '.join(urgency_sigs)}"

    candid_line = f"\n🔗 <{candid_url}|View Candid Profile>" if candid_url else ""
    irs_line    = "✅ IRS Verified 501(c)(3)" if irs_ok else "⚠️ IRS Not Found"

    blocks = [
        # Header
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🎯 CharityAI — Appointment Approval", "emoji": True},
        },
        {"type": "divider"},
        # Org summary
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{header_text}\n\n"
                    f"*Organization:* {org_name}\n"
                    f"*EIN:* `{ein}`\n"
                    f"*Request:* {reason[:280]}\n"
                    f"*Verification:* {confidence_emoji} {confidence.upper()} — {irs_line}"
                    f"{candid_line}"
                    f"{urgency_line}"
                ),
            },
        },
        {"type": "divider"},
        # Proposed slot
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📅 *Proposed Slot:* {display_time}\n_30-minute meeting, tentative hold created on calendar._",
            },
        },
        {"type": "divider"},
        # Action buttons
        {
            "type": "actions",
            "block_id": f"pa_decision_{pipeline_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "pa_approve",
                    "value": f"{pipeline_id}|{gcal_hold_id}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "pa_reject",
                    "value": f"{pipeline_id}|{gcal_hold_id}",
                },
            ],
        },
        # Footer note
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"⏰ Auto-approves in 24 hours if no response. "
                    f"Pipeline: `{pipeline_id}`"
                ),
            }],
        },
    ]

    try:
        _client().chat_postMessage(
            channel=_settings.PA_SLACK_USER_ID,
            blocks=blocks,
            text=f"Charity approval needed: {org_name}",  # Fallback text
        )
        log.info(
            "Approval DM sent to %s for pipeline %s",
            _settings.PA_SLACK_USER_ID, pipeline_id,
        )
    except SlackApiError as e:
        log.error("Slack DM failed: %s", e.response.get("error", str(e)))


async def send_escalation_dm(payload: dict) -> None:
    """
    Send a 🚨 URGENT Slack DM for an org that is ineligible but has urgency signals.
    No slot yet — PA decides whether to grant an exception.
    """
    pipeline_id   = payload.get("pipeline_id")
    ein           = payload.get("ein", "N/A")
    appointment_count_90d = payload.get("appointment_count_90d", 0)

    row = await fetch_one("SELECT * FROM pipeline WHERE id = $1", pipeline_id)
    org_name     = (row or {}).get("org_name", "Unknown Org")
    reason       = (row or {}).get("reason", "")
    urgency_sigs = (row or {}).get("urgency_signals") or []

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🚨 URGENT — Eligibility Exception Request", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{org_name}* is requesting an urgent meeting but is within the 90-day window.\n\n"
                    f"*EIN:* `{ein}`\n"
                    f"*Appointments in last 90 days:* {appointment_count_90d}\n"
                    f"*Reason:* {reason[:280]}\n"
                    f"⚡ *Urgency:* {', '.join(urgency_sigs)}\n\n"
                    "_This organization has reached out again due to an emergency situation. "
                    "Please decide if an exception is warranted._"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"urgency_decision_{pipeline_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⚡ Grant Exception", "emoji": True},
                    "style": "primary",
                    "action_id": "pa_approve",
                    "value": f"{pipeline_id}|EXCEPTION",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Decline", "emoji": True},
                    "style": "danger",
                    "action_id": "pa_reject",
                    "value": f"{pipeline_id}|EXCEPTION",
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Pipeline: `{pipeline_id}` | EIN: `{ein}`"}],
        },
    ]

    try:
        _client().chat_postMessage(
            channel=_settings.PA_SLACK_USER_ID,
            blocks=blocks,
            text=f"🚨 URGENT exception request: {org_name}",
        )
        log.info("Escalation DM sent for pipeline %s", pipeline_id)
    except SlackApiError as e:
        log.error("Slack escalation DM failed: %s", e.response.get("error", str(e)))



def _build_approval_blocks(
    org_name: str, ein: str, reason: str, display_time: str,
    gcal_hold_id: str, pipeline_id: str,
    candid_url: Optional[str], confidence: str, irs_ok: bool,
    urgency_signals: list, urgent: bool,
) -> list:
    """Build Slack Block Kit payload for an approval DM. Testable without DB."""
    confidence_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴", "failed": "⛔"}.get(confidence, "🔴")
    header_text = (
        "🚨 *URGENT Charity Request — PA Action Required*" if urgent
        else "📋 *New Charity Request — PA Approval Required*"
    )
    urgency_line = f"\n⚡ *Urgency Signals:* {', '.join(urgency_signals)}" if urgency_signals else ""
    candid_line  = f"\n🔗 <{candid_url}|View Candid Profile>" if candid_url else ""
    irs_line     = "✅ IRS Verified 501(c)(3)" if irs_ok else "⚠️ IRS Not Found"

    return [
        {"type": "header", "text": {"type": "plain_text", "text": "🎯 CharityAI — Appointment Approval", "emoji": True}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f"{header_text}\n\n"
                f"*Organization:* {org_name}\n*EIN:* `{ein}`\n"
                f"*Request:* {reason[:280]}\n"
                f"*Verification:* {confidence_emoji} {confidence.upper()} — {irs_line}"
                f"{candid_line}{urgency_line}"
            )},
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"📅 *Proposed Slot:* {display_time}\n_30-minute meeting, tentative hold created on calendar._"}},
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"pa_decision_{pipeline_id}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                 "style": "primary", "action_id": "pa_approve", "value": f"{pipeline_id}|{gcal_hold_id}"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                 "style": "danger", "action_id": "pa_reject", "value": f"{pipeline_id}|{gcal_hold_id}"},
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"⏰ Auto-approves in 24 hours if no response. Pipeline: `{pipeline_id}`"}]},
    ]


def _build_escalation_blocks(
    org_name: str, ein: str, reason: str,
    urgency_signals: list, appointment_count_90d: int, pipeline_id: str,
) -> list:
    """Build Slack Block Kit payload for an urgent escalation DM. Testable without DB."""
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "🚨 URGENT — Eligibility Exception Request", "emoji": True}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f"*{org_name}* is requesting an urgent meeting but is within the 90-day window.\n\n"
                f"*EIN:* `{ein}`\n*Appointments in last 90 days:* {appointment_count_90d}\n"
                f"*Reason:* {reason[:280]}\n"
                f"⚡ *Urgency:* {', '.join(urgency_signals)}\n\n"
                "_This organization has reached out again due to an emergency situation. "
                "Please decide if an exception is warranted._"
            )},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"urgency_decision_{pipeline_id}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "⚡ Grant Exception", "emoji": True},
                 "style": "primary", "action_id": "pa_approve", "value": f"{pipeline_id}|EXCEPTION"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Decline", "emoji": True},
                 "style": "danger", "action_id": "pa_reject", "value": f"{pipeline_id}|EXCEPTION"},
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Pipeline: `{pipeline_id}` | EIN: `{ein}`"}]},
    ]


async def update_dm_with_decision(channel: str, ts: str, decision: str, org_name: str) -> None:
    """Update the original DM to show the decision taken (replaces buttons)."""
    emoji   = "✅" if decision == "approved" else "❌"
    message = f"{emoji} *{org_name}* — Decision recorded: *{decision.upper()}*\n_Buttons removed._"
    try:
        _client().chat_update(
            channel=channel, ts=ts, text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
        )
    except SlackApiError as e:
        log.warning("Could not update DM after decision: %s", e.response.get("error", str(e)))

