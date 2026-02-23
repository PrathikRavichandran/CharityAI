"""
Unit tests for Phase 2 — Email Classifier, Extractor, and Dedup Guard.

Run with: pytest tests/unit/test_phase2.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agents.email_watcher.classifier import classify_email, _parse_json
from agents.email_watcher.extractor import extract_fields, _normalize_ein
from agents.dedup_guard.dedup_logic import _compute_hash


# ── Classifier Tests ──────────────────────────────────────────────────────────

class TestClassifier:

    @pytest.mark.asyncio
    async def test_charity_email_classified_correctly(self):
        mock_response = '{"is_charity": true, "confidence": 0.95, "reasoning": "Nonprofit food bank requesting executive meeting"}'
        with patch("agents.email_watcher.classifier._router") as mock_router:
            mock_router.complete = AsyncMock(return_value=mock_response)
            result = await classify_email(
                subject="Meeting Request — Feeding America",
                body="We are a 501(c)(3) food bank serving 500k families. EIN: 36-3673599. We would like to schedule a meeting to discuss partnership opportunities.",
                from_addr="grants@feedingamerica.org",
            )
        assert result["is_charity"] is True
        assert result["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_sales_email_rejected(self):
        mock_response = '{"is_charity": false, "confidence": 0.98, "reasoning": "Commercial SaaS product sales pitch"}'
        with patch("agents.email_watcher.classifier._router") as mock_router:
            mock_router.complete = AsyncMock(return_value=mock_response)
            result = await classify_email(
                subject="Boost Your Sales with AI — Free Trial",
                body="Hi, check out our CRM tool. 14-day free trial, no credit card required.",
                from_addr="sales@crm-tool.com",
            )
        assert result["is_charity"] is False

    def test_parse_json_handles_markdown_fences(self):
        raw = '```json\n{"is_charity": true, "confidence": 0.9, "reasoning": "test"}\n```'
        result = _parse_json(raw)
        assert result["is_charity"] is True
        assert result["confidence"] == 0.9

    def test_parse_json_handles_plain_json(self):
        raw = '{"is_charity": false, "confidence": 0.8, "reasoning": "not charity"}'
        result = _parse_json(raw)
        assert result["is_charity"] is False

    def test_parse_json_handles_malformed_returns_safe_default(self):
        result = _parse_json("this is not json at all {broken")
        assert result.get("is_charity") is False  # Safe default


# ── Extractor Tests ────────────────────────────────────────────────────────────

class TestExtractor:

    @pytest.mark.asyncio
    async def test_full_extraction(self):
        mock_response = '''{
            "org_name": "Feeding America",
            "ein": "36-3673599",
            "reason": "Emergency food security funding for 500k families in Ohio flood zone",
            "contact_email": "grants@feedingamerica.org",
            "estimated_people_impacted": 500000
        }'''
        with patch("agents.email_watcher.extractor._router") as mock_router:
            mock_router.complete = AsyncMock(return_value=mock_response)
            result = await extract_fields(
                subject="Emergency — Feeding America flood relief",
                body="We are Feeding America EIN 36-3673599. Urgent emergency funding needed due to flooding. 500,000 families at risk.",
                from_addr="grants@feedingamerica.org",
            )
        assert result["org_name"] == "Feeding America"
        assert result["ein"] == "36-3673599"
        assert result["reason"] is not None
        assert "emergency" in result["urgency_signals"]
        assert result["estimated_people_impacted"] == 500000

    def test_normalize_ein_9_digits(self):
        assert _normalize_ein("363673599") == "36-3673599"

    def test_normalize_ein_already_formatted(self):
        assert _normalize_ein("36-3673599") == "36-3673599"

    def test_normalize_ein_with_spaces(self):
        assert _normalize_ein("36 3673599") == "36-3673599"

    def test_urgency_keywords_detected(self):
        """Urgency signals are detected from body text, not LLM."""
        from agents.email_watcher.extractor import URGENCY_KEYWORDS
        body = "This is an emergency situation. Critical deadline approaching."
        body_lower = body.lower()
        detected = [kw for kw in URGENCY_KEYWORDS if kw in body_lower]
        assert "emergency" in detected
        assert "critical" in detected
        assert "deadline" in detected

    @pytest.mark.asyncio
    async def test_contact_email_fallback_from_from_header(self):
        mock_response = '{"org_name": "Test Org", "ein": "12-3456789", "reason": "Meeting", "contact_email": null}'
        with patch("agents.email_watcher.extractor._router") as mock_router:
            mock_router.complete = AsyncMock(return_value=mock_response)
            result = await extract_fields(
                subject="Test",
                body="Test email body",
                from_addr="Test Person <test@example.org>",
            )
        # Should fall back to parsing from_addr
        assert result["contact_email"] == "test@example.org"


# ── Dedup Guard Tests ─────────────────────────────────────────────────────────

class TestDedupGuard:

    def test_hash_is_deterministic(self):
        payload = {
            "org_name": "Feeding America",
            "ein": "36-3673599",
            "reason": "Emergency food funding",
        }
        h1 = _compute_hash(payload)
        h2 = _compute_hash(payload)
        assert h1 == h2
        assert len(h1) == 64   # SHA-256 hex length

    def test_hash_differs_for_different_orgs(self):
        p1 = {"org_name": "Feeding America", "ein": "36-3673599", "reason": "Food funding"}
        p2 = {"org_name": "Red Cross", "ein": "53-0196605", "reason": "Disaster relief"}
        assert _compute_hash(p1) != _compute_hash(p2)

    def test_hash_case_insensitive(self):
        """Hash is lowercase-normalized so case differences don't create false misses."""
        p1 = {"org_name": "FEEDING AMERICA", "ein": "36-3673599", "reason": "FOOD FUNDING"}
        p2 = {"org_name": "feeding america", "ein": "36-3673599", "reason": "food funding"}
        assert _compute_hash(p1) == _compute_hash(p2)

    @pytest.mark.asyncio
    async def test_duplicate_email_dispatches_drop(self):
        """When hash duplicate exists, dispatch is_duplicate=True to orchestrator."""
        from agents.dedup_guard.dedup_logic import process

        test_payload = {
            "email_id": "gmail_002",
            "pipeline_id": "pipe-002",
            "org_name": "Feeding America",
            "ein": "36-3673599",
            "reason": "Food security",
            "urgency_signals": [],
        }

        with patch("agents.dedup_guard.dedup_logic._check_hash_duplicate",
                   new_callable=AsyncMock,
                   return_value={"id": "old-pipe", "email_id": "gmail_001"}), \
             patch("agents.dedup_guard.dedup_logic.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await process(test_payload)

        call_kwargs = mock_dispatch.call_args
        payload_sent = call_kwargs.kwargs.get("payload", call_kwargs.args[2] if len(call_kwargs.args) > 2 else {})
        assert payload_sent.get("is_duplicate") is True
        assert payload_sent.get("is_merged") is False

    @pytest.mark.asyncio
    async def test_new_email_dispatches_clean_pass(self):
        """When no duplicates found, dispatch is_duplicate=False."""
        from agents.dedup_guard.dedup_logic import process

        test_payload = {
            "email_id": "gmail_new",
            "pipeline_id": "pipe-new",
            "org_name": "Red Cross",
            "ein": "53-0196605",
            "reason": "Disaster relief",
            "urgency_signals": [],
        }

        with patch("agents.dedup_guard.dedup_logic._check_hash_duplicate",
                   new_callable=AsyncMock, return_value=None), \
             patch("agents.dedup_guard.dedup_logic._check_active_org",
                   new_callable=AsyncMock, return_value=None), \
             patch("agents.dedup_guard.dedup_logic._store_hash",
                   new_callable=AsyncMock), \
             patch("agents.dedup_guard.dedup_logic.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await process(test_payload)

        call_kwargs = mock_dispatch.call_args
        payload_sent = call_kwargs.kwargs.get("payload", call_kwargs.args[2] if len(call_kwargs.args) > 2 else {})
        assert payload_sent.get("is_duplicate") is False
        assert payload_sent.get("is_merged") is False
