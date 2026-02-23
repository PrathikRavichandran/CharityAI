"""
Unit tests for Phase 3 — Charity Verifier and Eligibility Agent.

Run with: pytest tests/unit/test_phase3.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agents.charity_verifier.verifier import (
    candid_lookup_ein,
    irs_exempt_search,
    synthesize_confidence,
)
from agents.eligibility_agent.eligibility_logic import (
    _count_recent_appointments,
    ESCALATION_SIGNALS,
    check,
)
from shared.models import VerificationConfidence


# ── Charity Verifier Tests ────────────────────────────────────────────────────

class TestCandidStubs:
    """Candid stubs return None in stub mode (CANDID_MODE=stub)."""

    @pytest.mark.asyncio
    async def test_candid_lookup_returns_none_in_stub_mode(self):
        with patch("agents.charity_verifier.verifier._settings") as mock_settings:
            mock_settings.CANDID_MODE = "stub"
            mock_settings.CANDID_API_KEY = ""
            result = await candid_lookup_ein("36-3673599")
        assert result is None

    @pytest.mark.asyncio
    async def test_irs_search_makes_http_call(self):
        """IRS API is called in both stub and live modes."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hits": {"hits": [{
                "_source": {
                    "EIN": "363673599",
                    "NAME": "FEEDING AMERICA",
                    "SUBSECTION_CODE": "3",
                    "CITY": "CHICAGO",
                    "STATE": "IL",
                }
            }]}
        }
        with patch("agents.charity_verifier.verifier.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            result = await irs_exempt_search("36-3673599", "Feeding America")

        assert result is not None
        assert result["irs_verified"] is True
        assert "FEEDING AMERICA" in result["irs_name"]


class TestManualMode:

    @pytest.mark.asyncio
    async def test_manual_mode_forces_low_confidence(self):
        """In manual mode, confidence is always LOW regardless of sources."""
        result = await synthesize_confidence(
            ein="36-3673599",
            org_name="Feeding America",
            irs_data={"irs_name": "FEEDING AMERICA", "irs_verified": True, "irs_status": "3"},
            candid_data=None,
            web_data=None,
            manual_mode=True,   # ← manual mode ON
        )
        assert result["verified"] is True
        assert result["confidence"] == VerificationConfidence.LOW
        assert "irs" in result["sources"]

    @pytest.mark.asyncio
    async def test_no_sources_returns_failed(self):
        """No IRS + no Candid + no web → not verified."""
        result = await synthesize_confidence(
            ein="99-0000001",
            org_name="Fake Org",
            irs_data=None,
            candid_data=None,
            web_data=None,
            manual_mode=True,
        )
        assert result["verified"] is False
        assert result["confidence"] == VerificationConfidence.FAILED

    @pytest.mark.asyncio
    async def test_all_sources_still_low_in_manual(self):
        """Even with Candid + IRS + web, manual mode returns LOW."""
        result = await synthesize_confidence(
            ein="36-3673599",
            org_name="Feeding America",
            irs_data={"irs_name": "Feeding America", "irs_verified": True, "irs_status": "3"},
            candid_data={"candid_id": "abc123", "candid_profile_url": "https://candid.org/abc"},
            web_data={"web_snippet": "Feeding America food bank", "web_verified": True},
            manual_mode=True,
        )
        assert result["confidence"] == VerificationConfidence.LOW
        assert set(result["sources"]) == {"irs", "candid", "web"}


# ── Eligibility Agent Tests ───────────────────────────────────────────────────

class TestEligibilityLogic:

    @pytest.mark.asyncio
    async def test_eligible_when_no_recent_appointments(self):
        with patch("agents.eligibility_agent.eligibility_logic.fetch_val",
                   new_callable=AsyncMock, return_value=0):
            count = await _count_recent_appointments("36-3673599", 90)
        assert count == 0

    @pytest.mark.asyncio
    async def test_ineligible_when_recent_appointment_exists(self):
        with patch("agents.eligibility_agent.eligibility_logic.fetch_val",
                   new_callable=AsyncMock, return_value=1):
            count = await _count_recent_appointments("36-3673599", 90)
        assert count == 1

    def test_escalation_signals_are_defined(self):
        """Emergency keywords must cover the most critical scenarios."""
        must_include = {"emergency", "urgent", "crisis", "flood", "disaster"}
        assert must_include.issubset(ESCALATION_SIGNALS)

    @pytest.mark.asyncio
    async def test_ineligible_with_urgency_escalates_to_pa(self):
        """Ineligible + urgency → escalate_to_pa=True."""
        payload = {
            "pipeline_id": "pipe-001",
            "ein": "36-3673599",
        }
        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(side_effect={"urgency_signals": ["emergency", "flood"]}.__getitem__)

        with patch("agents.eligibility_agent.eligibility_logic.fetch_val",
                   new_callable=AsyncMock, return_value=1), \
             patch("agents.eligibility_agent.eligibility_logic.fetch_one",
                   new_callable=AsyncMock, return_value=mock_row), \
             patch("agents.eligibility_agent.eligibility_logic.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await check(payload)

        sent = mock_dispatch.call_args.kwargs.get("payload", {})
        assert sent.get("is_eligible") is False
        assert sent.get("escalate_to_pa") is True

    @pytest.mark.asyncio
    async def test_ineligible_without_urgency_drops(self):
        """Ineligible + no urgency → escalate_to_pa=False."""
        payload = {"pipeline_id": "pipe-002", "ein": "53-0196605"}

        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(side_effect={"urgency_signals": []}.__getitem__)

        with patch("agents.eligibility_agent.eligibility_logic.fetch_val",
                   new_callable=AsyncMock, return_value=2), \
             patch("agents.eligibility_agent.eligibility_logic.fetch_one",
                   new_callable=AsyncMock, return_value=mock_row), \
             patch("agents.eligibility_agent.eligibility_logic.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await check(payload)

        sent = mock_dispatch.call_args.kwargs.get("payload", {})
        assert sent.get("is_eligible") is False
        assert sent.get("escalate_to_pa") is False

    @pytest.mark.asyncio
    async def test_eligible_org_passes(self):
        """COUNT=0 org → is_eligible=True, no escalation."""
        payload = {"pipeline_id": "pipe-003", "ein": "13-1837418"}

        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(side_effect={"urgency_signals": []}.__getitem__)

        with patch("agents.eligibility_agent.eligibility_logic.fetch_val",
                   new_callable=AsyncMock, return_value=0), \
             patch("agents.eligibility_agent.eligibility_logic.fetch_one",
                   new_callable=AsyncMock, return_value=mock_row), \
             patch("agents.eligibility_agent.eligibility_logic.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await check(payload)

        sent = mock_dispatch.call_args.kwargs.get("payload", {})
        assert sent.get("is_eligible") is True
        assert sent.get("escalate_to_pa") is False
