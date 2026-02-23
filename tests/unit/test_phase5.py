"""
Unit tests for Phase 5 — PA Notification and Email Composer.

Tests:
  - Slack HMAC signature verification (valid / expired / tampered)
  - Block Kit message structure (has header, divider, actions block)
  - Email contact name derivation
  - Mistral email fallback template correctness

Run with: pytest tests/unit/test_phase5.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agents.pa_notification.slack_handler import verify_slack_signature
from agents.email_composer.composer import _derive_contact_name


# ── Slack Signature Verification ─────────────────────────────────────────────

class TestSlackSignatureVerification:
    SIGNING_SECRET = "test_signing_secret_abc123"

    def _make_valid_headers(self, body: bytes, ts: Optional[int] = None) -> dict:
        ts = ts or int(time.time())
        basestring = f"v0:{ts}:{body.decode('utf-8')}"
        sig = "v0=" + hmac.new(
            self.SIGNING_SECRET.encode(),
            basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {"x-slack-request-timestamp": str(ts), "x-slack-signature": sig}

    def test_valid_signature_passes(self):
        body = b"payload=%7B%22action%22%3A%22test%22%7D"
        headers = self._make_valid_headers(body)
        with patch("agents.pa_notification.slack_handler._settings") as m:
            m.SLACK_SIGNING_SECRET = self.SIGNING_SECRET
            result = verify_slack_signature(headers, body)
        assert result is True

    def test_expired_timestamp_fails(self):
        """Requests older than 5 minutes should be rejected (replay attack)."""
        body = b"payload=test"
        old_ts = int(time.time()) - 400   # 400 seconds ago = expired
        headers = self._make_valid_headers(body, ts=old_ts)
        with patch("agents.pa_notification.slack_handler._settings") as m:
            m.SLACK_SIGNING_SECRET = self.SIGNING_SECRET
            result = verify_slack_signature(headers, body)
        assert result is False

    def test_tampered_body_fails(self):
        """Valid signature but body was modified → should reject."""
        original_body = b"payload=original"
        tampered_body = b"payload=tampered"
        headers = self._make_valid_headers(original_body)
        with patch("agents.pa_notification.slack_handler._settings") as m:
            m.SLACK_SIGNING_SECRET = self.SIGNING_SECRET
            result = verify_slack_signature(headers, tampered_body)
        assert result is False

    def test_wrong_secret_fails(self):
        body = b"payload=test"
        headers = self._make_valid_headers(body)
        with patch("agents.pa_notification.slack_handler._settings") as m:
            m.SLACK_SIGNING_SECRET = "completely_wrong_secret"
            result = verify_slack_signature(headers, body)
        assert result is False


# ── Block Kit Message Structure ───────────────────────────────────────────────

class TestBlockKitStructure:

    def test_approval_dm_has_required_blocks(self):
        """Verify notifier builds a Block Kit message with all required sections."""
        from agents.pa_notification.notifier import _build_approval_blocks

        blocks = _build_approval_blocks(
            org_name="Feeding America",
            ein="36-3673599",
            reason="Emergency food security funding",
            display_time="Tuesday, March 5 at 2:00 PM CT",
            gcal_hold_id="gcal_event_001",
            pipeline_id="pipe-001",
            candid_url="https://candid.org/profile/feeding-america",
            confidence="low",
            irs_ok=True,
            urgency_signals=["emergency"],
            urgent=False,
        )

        block_types = [b["type"] for b in blocks]
        assert "header" in block_types
        assert "actions" in block_types  # Must have Approve/Reject buttons
        assert "divider" in block_types

        # Find the actions block and verify it has approve + reject buttons
        action_blocks = [b for b in blocks if b["type"] == "actions"]
        assert len(action_blocks) == 1
        elements = action_blocks[0]["elements"]
        action_ids = [e["action_id"] for e in elements]
        assert "pa_approve" in action_ids
        assert "pa_reject" in action_ids

    def test_urgent_dm_has_urgent_header(self):
        from agents.pa_notification.notifier import _build_escalation_blocks
        blocks = _build_escalation_blocks(
            org_name="Flood Relief Fund",
            ein="12-3456789",
            reason="River flooding emergency",
            urgency_signals=["flood", "emergency"],
            appointment_count_90d=1,
            pipeline_id="pipe-002",
        )
        # Check that the text somewhere contains URGENT / 🚨
        all_text = str(blocks)
        assert "URGENT" in all_text or "🚨" in all_text


# ── Email Contact Name Derivation ─────────────────────────────────────────────

class TestContactNameDerivation:

    def test_grants_email_becomes_grants_team(self):
        assert _derive_contact_name("grants@feedingamerica.org", "Feeding America") == "Grants Team"

    def test_firstname_lastname_returns_firstname(self):
        result = _derive_contact_name("john.doe@redcross.org", "Red Cross")
        assert result == "John"

    def test_firstname_only_local_part(self):
        result = _derive_contact_name("sarah@habitat.org", "Habitat")
        assert result == "Sarah Team"

    def test_fallback_for_unrecognizable_email(self):
        result = _derive_contact_name("h123x@example.com", "Test Org")
        # Should return something sensible — not crash
        assert isinstance(result, str) and len(result) > 0

    def test_info_email_style(self):
        result = _derive_contact_name("info@charity.org", "Test Charity")
        assert "Info" in result or "Test Charity" in result


# ── Email Drafting Fallback ───────────────────────────────────────────────────

class TestEmailFallbacks:

    @pytest.mark.asyncio
    async def test_confirmation_fallback_contains_slot(self):
        """When Mistral fails, template still includes the slot time."""
        from agents.email_composer.composer import _draft_confirmation
        from shared.ollama_client import ModelFailureError

        with patch("agents.email_composer.composer._router") as mock_router:
            mock_router.complete = AsyncMock(side_effect=ModelFailureError(
                task="EMAIL_COMPOSE", primary="mistral", fallback="llama3"
            ))
            body = await _draft_confirmation(
                org_name="Feeding America",
                contact_name="Grants Team",
                slot_display="Tuesday, March 5 at 2:00 PM Central Time",
                reason="Emergency food security",
            )

        assert "Tuesday, March 5 at 2:00 PM Central Time" in body
        assert "Feeding America" in body
        assert "YES" in body   # RSVP instruction

    @pytest.mark.asyncio
    async def test_rejection_fallback_contains_org_name(self):
        """Rejection fallback template must include org name."""
        from agents.email_composer.composer import _draft_rejection
        from shared.ollama_client import ModelFailureError

        with patch("agents.email_composer.composer._router") as mock_router:
            mock_router.complete = AsyncMock(side_effect=ModelFailureError(
                task="EMAIL_COMPOSE", primary="mistral", fallback="llama3"
            ))
            body = await _draft_rejection(
                org_name="Red Cross",
                contact_name="Grants Team",
                rejection_reason="Not aligned with current priorities",
            )

        assert "Red Cross" in body
        assert "CharityAI Scheduling Team" in body
