"""
Unit tests for the Orchestrator State Machine.

Tests valid/invalid transitions, timer management, and transition table completeness.

Run with: pytest tests/unit/test_orchestrator.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from shared.models import PipelineState
from agents.orchestrator.state_machine import (
    StateMachine,
    InvalidTransitionError,
    VALID_TRANSITIONS,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_record(state: str, pipeline_id: str = "pipe-001") -> MagicMock:
    """Return a mock asyncpg record for the given state."""
    data = {
        "id": pipeline_id,
        "email_id": "gmail_001",
        "email_thread_id": "thread_001",
        "ein": "36-3673599",
        "org_name": "Feeding America",
        "current_state": state,
        "priority_score": None,
        "proposed_slot": None,
    }
    record = MagicMock()
    # Support both dict(row) and row["key"] access
    record.__iter__ = MagicMock(return_value=iter(data.items()))
    record.keys = MagicMock(return_value=data.keys())
    record.__getitem__ = MagicMock(side_effect=data.__getitem__)
    record.get = MagicMock(side_effect=data.get)
    return record


def patch_all(from_state: str):
    """Context manager that patches all external deps for StateMachine.transition()."""
    record = make_record(from_state)
    updated = make_record(from_state)  # After transition

    return (
        patch("agents.orchestrator.state_machine.fetch_one", new_callable=AsyncMock, return_value=record),
        patch("agents.orchestrator.state_machine.transition_state", new_callable=AsyncMock, return_value=updated),
        patch("agents.orchestrator.state_machine.publish", new_callable=AsyncMock),
        patch("agents.orchestrator.state_machine.set_timer", new_callable=AsyncMock),
        patch("agents.orchestrator.state_machine.cancel_timer", new_callable=AsyncMock),
    )


# ── Valid Transitions ─────────────────────────────────────────────────────────

class TestValidTransitions:

    @pytest.mark.asyncio
    async def test_email_received_to_classifying(self):
        p1, p2, p3, p4, p5 = patch_all("EMAIL_RECEIVED")
        with p1, p2 as mock_ts, p3, p4, p5:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.CLASSIFYING, actor="email_watcher")
            mock_ts.assert_called_once()

    @pytest.mark.asyncio
    async def test_classifying_to_dedup_check(self):
        p1, p2, p3, p4, p5 = patch_all("CLASSIFYING")
        with p1, p2 as mock_ts, p3, p4, p5:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.DEDUP_CHECK, actor="email_watcher")
            mock_ts.assert_called_once()

    @pytest.mark.asyncio
    async def test_classifying_to_dropped_not_charity(self):
        p1, p2, p3, p4, p5 = patch_all("CLASSIFYING")
        with p1, p2 as mock_ts, p3, p4, p5:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.DROPPED_NOT_CHARITY, actor="email_watcher")
            mock_ts.assert_called_once()

    @pytest.mark.asyncio
    async def test_pa_pending_to_auto_approved(self):
        p1, p2, p3, p4, p5 = patch_all("PA_PENDING")
        with p1, p2, p3, p4, p5 as mock_cancel:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.AUTO_APPROVED, actor="SYSTEM_TIMEOUT")
            # PA timeout timer must be cancelled
            mock_cancel.assert_called_once_with("pa_timeout:pipe-001")

    @pytest.mark.asyncio
    async def test_pa_pending_to_pa_rejected(self):
        p1, p2, p3, p4, p5 = patch_all("PA_PENDING")
        with p1, p2 as mock_ts, p3, p4, p5:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.PA_REJECTED, actor="PA")
            mock_ts.assert_called_once()

    @pytest.mark.asyncio
    async def test_rsvp_pending_sets_timer(self):
        p1, p2, p3, p4, p5 = patch_all("CONFIRMATION_SENT")
        with p1, p2, p3, p4 as mock_set, p5:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.RSVP_PENDING, actor="orchestrator")
            mock_set.assert_called_once_with("rsvp_timeout:pipe-001", 86400, value="pending")

    @pytest.mark.asyncio
    async def test_pa_pending_sets_timer(self):
        p1, p2, p3, p4, p5 = patch_all("SLOT_HELD")
        with p1, p2, p3, p4 as mock_set, p5:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.PA_PENDING, actor="orchestrator")
            mock_set.assert_called_once_with("pa_timeout:pipe-001", 86400, value="pending")

    @pytest.mark.asyncio
    async def test_rsvp_confirmed_cancels_timer(self):
        p1, p2, p3, p4, p5 = patch_all("RSVP_PENDING")
        with p1, p2, p3, p4, p5 as mock_cancel:
            sm = StateMachine()
            await sm.transition("pipe-001", PipelineState.RSVP_CONFIRMED, actor="rsvp_monitor")
            mock_cancel.assert_called_once_with("rsvp_timeout:pipe-001")


# ── Invalid Transitions Must Raise ───────────────────────────────────────────

class TestInvalidTransitions:

    @pytest.mark.asyncio
    async def test_email_received_to_booked_illegal(self):
        p1, p2, p3, p4, p5 = patch_all("EMAIL_RECEIVED")
        with p1, p2, p3, p4, p5:
            sm = StateMachine()
            with pytest.raises(InvalidTransitionError) as exc_info:
                await sm.transition("pipe-001", PipelineState.BOOKED, actor="test")
            assert exc_info.value.from_state == "EMAIL_RECEIVED"
            assert exc_info.value.to_state == "BOOKED"

    @pytest.mark.asyncio
    async def test_classifying_to_booked_illegal(self):
        p1, p2, p3, p4, p5 = patch_all("CLASSIFYING")
        with p1, p2, p3, p4, p5:
            sm = StateMachine()
            with pytest.raises(InvalidTransitionError):
                await sm.transition("pipe-001", PipelineState.BOOKED, actor="test")

    @pytest.mark.asyncio
    async def test_booked_to_anything_illegal(self):
        """BOOKED is terminal — nothing can follow."""
        for new_state in [PipelineState.CLASSIFYING, PipelineState.PA_PENDING, PipelineState.SCORING]:
            p1, p2, p3, p4, p5 = patch_all("BOOKED")
            with p1, p2, p3, p4, p5:
                sm = StateMachine()
                with pytest.raises(InvalidTransitionError):
                    await sm.transition("pipe-001", new_state, actor="test")

    @pytest.mark.asyncio
    async def test_pipeline_not_found_raises_value_error(self):
        with patch("agents.orchestrator.state_machine.fetch_one",
                   new_callable=AsyncMock, return_value=None):
            sm = StateMachine()
            with pytest.raises(ValueError, match="Pipeline not found"):
                await sm.transition("nonexistent", PipelineState.CLASSIFYING, actor="test")


# ── Terminal States ──────────────────────────────────────────────────────────

class TestTerminalStates:

    TERMINAL = [
        PipelineState.BOOKED,
        PipelineState.DROPPED_NOT_CHARITY,
        PipelineState.DROPPED_MISSING_INFO,
        PipelineState.DROPPED_DUPLICATE,
        PipelineState.DROPPED_NOT_VERIFIED,
        PipelineState.DROPPED_WITHIN_90_DAYS,
        PipelineState.AGENT_UNREACHABLE,
    ]

    def test_terminal_states_have_no_outbound_transitions(self):
        for state in self.TERMINAL:
            valid_next = VALID_TRANSITIONS.get(state, set())
            assert len(valid_next) == 0, (
                f"{state.value} should be terminal but has outbound transitions: {valid_next}"
            )


# ── Transition Table Completeness ────────────────────────────────────────────

class TestTransitionTableCompleteness:

    def test_all_pipeline_states_are_in_transition_table(self):
        """Every PipelineState must have an entry in VALID_TRANSITIONS."""
        for state in PipelineState:
            assert state in VALID_TRANSITIONS, (
                f"PipelineState.{state.value} is missing from VALID_TRANSITIONS — "
                f"add it with an empty set() if terminal."
            )

    def test_valid_transitions_only_reference_existing_states(self):
        """Every target state in transition table must be a valid PipelineState."""
        all_states = set(PipelineState)
        for from_state, to_states in VALID_TRANSITIONS.items():
            for to_state in to_states:
                assert to_state in all_states, (
                    f"Transition {from_state.value} → {to_state} refers to unknown state."
                )
