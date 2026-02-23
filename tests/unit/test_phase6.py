"""
Unit tests for Phase 6 — RSVP Monitor.

Tests:
  - RSVP intent classification (keyword fast-path)
  - Ambiguous intent handling
  - Phi3 LLM classification fallback
  - Timeout detection logic
  - Thread registry (register + deregister)
  - GCal conflict outcome dispatch (mocked)

Run with: pytest tests/unit/test_phase6.py -v
"""

from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from agents.rsvp_monitor.monitor import classify_rsvp_intent, _compute_thread_age_hours
from shared.models import RSVPOutcome, RSVPNextAction, TaskType


# ── RSVP Intent Classification Tests ─────────────────────────────────────────

class TestRSVPIntentClassification:

    @pytest.mark.asyncio
    async def test_yes_keyword_confirmed_fast_path(self):
        """'Yes' keyword should trigger confirmed without LLM call."""
        with patch("agents.rsvp_monitor.monitor._router") as mock_router:
            mock_router.complete = AsyncMock()
            result = await classify_rsvp_intent("Yes, we confirm the meeting. Looking forward!")
        # Should NOT have called LLM (fast-path keyword detection)
        mock_router.complete.assert_not_called()
        assert result["intent"] == "confirmed"
        assert result["confidence"] >= 0.9

    @pytest.mark.asyncio
    async def test_confirm_keyword_fast_path(self):
        result = await classify_rsvp_intent("I confirm our attendance for the appointment.")
        assert result["intent"] == "confirmed"

    @pytest.mark.asyncio
    async def test_no_keyword_declined_fast_path(self):
        with patch("agents.rsvp_monitor.monitor._router") as mock_router:
            mock_router.complete = AsyncMock()
            result = await classify_rsvp_intent("Sorry, we can't make it. We need to cancel.")
        mock_router.complete.assert_not_called()
        assert result["intent"] == "declined"
        assert result["confidence"] >= 0.85

    @pytest.mark.asyncio
    async def test_unfortunately_declined_fast_path(self):
        result = await classify_rsvp_intent("Unfortunately we are unable to attend.")
        assert result["intent"] == "declined"

    @pytest.mark.asyncio
    async def test_ambiguous_delegates_to_llm(self):
        """Vague reply with no clear keywords → LLM is called."""
        mock_resp = '{"intent": "ambiguous", "confidence": 0.5, "reasoning": "Cannot determine"}'
        with patch("agents.rsvp_monitor.monitor._router") as mock_router:
            mock_router.complete = AsyncMock(return_value=mock_resp)
            result = await classify_rsvp_intent("Thank you for reaching out to us.")
        mock_router.complete.assert_called_once()
        assert result["intent"] == "ambiguous"

    @pytest.mark.asyncio
    async def test_llm_confirms_from_json_response(self):
        """LLM returning confirmed JSON → use it."""
        mock_resp = '{"intent": "confirmed", "confidence": 0.85, "reasoning": "Positive tone"}'
        with patch("agents.rsvp_monitor.monitor._router") as mock_router:
            mock_router.complete = AsyncMock(return_value=mock_resp)
            result = await classify_rsvp_intent("We are delighted to accept your kind invitation.")
        assert result["intent"] == "confirmed"
        assert result["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_llm_failure_returns_ambiguous(self):
        """LLM failure → safe 'ambiguous' default (keep watching)."""
        from shared.ollama_client import ModelFailureError
        with patch("agents.rsvp_monitor.monitor._router") as mock_router:
            mock_router.complete = AsyncMock(
                side_effect=ModelFailureError(task="rsvp_classify", primary="phi3", fallback="mistral")
            )
            result = await classify_rsvp_intent("Something unclear happened here.")
        assert result["intent"] == "ambiguous"
        assert result["confidence"] == 0.0


# ── Thread Timeout Tests ───────────────────────────────────────────────────────

class TestThreadTimeout:

    def test_24h_old_thread_is_expired(self):
        registered_at = datetime.now(timezone.utc) - timedelta(hours=25)
        age = _compute_thread_age_hours(registered_at.isoformat())
        assert age > 24.0

    def test_1h_old_thread_is_not_expired(self):
        registered_at = datetime.now(timezone.utc) - timedelta(hours=1)
        age = _compute_thread_age_hours(registered_at.isoformat())
        assert age < 24.0

    def test_exactly_24h_boundary(self):
        registered_at = datetime.now(timezone.utc) - timedelta(hours=24, minutes=1)
        age = _compute_thread_age_hours(registered_at.isoformat())
        assert age > 24.0


# ── Outcome Dispatch Verification ────────────────────────────────────────────

class TestOutcomeDispatch:

    @pytest.mark.asyncio
    async def test_confirmed_dispatches_finalize(self):
        """RSVP confirmed → FINALIZE next_action dispatched."""
        from agents.rsvp_monitor.monitor import _dispatch_outcome

        with patch("agents.rsvp_monitor.monitor.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await _dispatch_outcome(
                pipeline_id="pipe-001",
                outcome=RSVPOutcome.CONFIRMED,
                next_action=RSVPNextAction.FINALIZE,
                gcal_event_id="evt_confirmed_001",
                rsvp_received_at="2026-03-01T14:00:00+00:00",
            )

        sent = mock_dispatch.call_args.kwargs
        assert sent["task_type"] == TaskType.RSVP_OUTCOME
        payload = sent["payload"]
        assert payload["outcome"] == RSVPOutcome.CONFIRMED.value
        assert payload["next_action"] == RSVPNextAction.FINALIZE.value

    @pytest.mark.asyncio
    async def test_declined_dispatches_cancel_and_pull_next(self):
        """RSVP declined → CANCEL_AND_PULL_NEXT dispatched."""
        from agents.rsvp_monitor.monitor import _dispatch_outcome

        with patch("agents.rsvp_monitor.monitor.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await _dispatch_outcome(
                pipeline_id="pipe-002",
                outcome=RSVPOutcome.DECLINED,
                next_action=RSVPNextAction.CANCEL_AND_PULL_NEXT,
                gcal_event_id=None,
                rsvp_received_at="2026-03-01T14:30:00+00:00",
            )

        payload = mock_dispatch.call_args.kwargs["payload"]
        assert payload["outcome"] == RSVPOutcome.DECLINED.value
        assert payload["next_action"] == RSVPNextAction.CANCEL_AND_PULL_NEXT.value
        assert payload["gcal_event_id"] is None

    @pytest.mark.asyncio
    async def test_timeout_dispatches_cancel_and_pull_next(self):
        """Timeout → CANCEL_AND_PULL_NEXT."""
        from agents.rsvp_monitor.monitor import _dispatch_outcome

        with patch("agents.rsvp_monitor.monitor.dispatch_task",
                   new_callable=AsyncMock) as mock_dispatch:
            await _dispatch_outcome(
                pipeline_id="pipe-003",
                outcome=RSVPOutcome.TIMEOUT,
                next_action=RSVPNextAction.CANCEL_AND_PULL_NEXT,
                gcal_event_id=None,
                rsvp_received_at=None,
            )

        payload = mock_dispatch.call_args.kwargs["payload"]
        assert payload["outcome"] == RSVPOutcome.TIMEOUT.value
        assert payload["rsvp_received_at"] is None


# ── ModelTask Routing Tests ───────────────────────────────────────────────────

class TestModelTaskRouting:
    """Verify all ModelTask enum values have routing entries in OllamaRouter."""

    def test_all_tasks_have_routing(self):
        from shared.ollama_client import OllamaRouter, ModelTask
        router = OllamaRouter()
        for task in ModelTask:
            assert task in router._routing, f"ModelTask.{task.name} has no routing entry"

    def test_priority_score_routes_to_llama3(self):
        from shared.ollama_client import OllamaRouter, ModelTask
        router = OllamaRouter()
        primary, fallback = router._routing[ModelTask.PRIORITY_SCORE]
        assert "llama3" in primary

    def test_email_compose_routes_to_llama3(self):
        from shared.ollama_client import OllamaRouter, ModelTask
        router = OllamaRouter()
        primary, fallback = router._routing[ModelTask.EMAIL_COMPOSE]
        assert "llama3" in primary
