"""
Unit tests for Phase 4 — Prioritizer Scorer and Calendar Slot Finder.

Run with: pytest tests/unit/test_phase4.py -v
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
import pytz

from agents.prioritizer.scorer import (
    compute_urgency_score,
    compute_queue_bonus,
    URGENCY_SCORES,
)
from agents.calendar_agent.slot_finder import (
    _generate_candidate_slots,
    _is_free,
)

CT = pytz.timezone("America/Chicago")


# ── Prioritizer: Urgency Score Tests ─────────────────────────────────────────

class TestUrgencyScorer:

    def test_emergency_signal_scores_max(self):
        assert compute_urgency_score(["emergency"]) == 25

    def test_disaster_signal_scores_max(self):
        assert compute_urgency_score(["disaster"]) == 25

    def test_urgent_signal_scores_15(self):
        assert compute_urgency_score(["urgent"]) == 15

    def test_deadline_signal_scores_10(self):
        assert compute_urgency_score(["deadline"]) == 10

    def test_no_signals_scores_zero(self):
        assert compute_urgency_score([]) == 0

    def test_multiple_signals_takes_highest(self):
        """With 'emergency' (25) and 'deadline' (10) → score should be 25."""
        assert compute_urgency_score(["deadline", "emergency"]) == 25

    def test_unknown_signal_scores_zero(self):
        assert compute_urgency_score(["free_pizza"]) == 0

    def test_score_capped_at_25(self):
        """Even with multiple max-score signals, cap is 25."""
        signals = ["emergency", "disaster", "crisis"]
        assert compute_urgency_score(signals) == 25

    def test_all_signals_in_table_score_above_zero(self):
        for kw, score in URGENCY_SCORES.items():
            assert score > 0, f"Urgency signal '{kw}' has zero score"


# ── Prioritizer: Queue Bonus Tests ────────────────────────────────────────────

class TestQueueBonus:

    def test_zero_days_in_queue_no_bonus(self):
        assert compute_queue_bonus(0) == 0

    def test_one_week_gives_5_pts(self):
        assert compute_queue_bonus(7) == 5

    def test_two_weeks_gives_10_pts(self):
        assert compute_queue_bonus(14) == 10

    def test_five_weeks_capped_at_25(self):
        """5 weeks = 25 pts, but cap is also 25 → still 25."""
        assert compute_queue_bonus(35) == 25

    def test_ten_weeks_still_capped_at_25(self):
        """10 weeks would be 50, but cap is 25."""
        assert compute_queue_bonus(70) == 25

    def test_six_days_gives_zero(self):
        """6 days < 1 week → no bonus yet."""
        assert compute_queue_bonus(6) == 0


# ── Calendar: Slot Generation Tests ───────────────────────────────────────────

class TestSlotGeneration:

    def _future_monday_ct(self) -> datetime:
        """Return a datetime for next Monday at midnight (CT)."""
        now = datetime.now(CT)
        days_ahead = (0 - now.weekday()) % 7  # Days to next Monday
        if days_ahead == 0:
            days_ahead = 7
        return (now + timedelta(days=days_ahead + 14)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    def test_slots_generated_within_window(self):
        start = self._future_monday_ct()
        end = start + timedelta(days=3)
        slots = _generate_candidate_slots(
            from_dt=start, to_dt=end,
            duration_min=30,
            start_hour=13, end_hour=17,
        )
        assert len(slots) > 0

    def test_no_slots_on_weekends(self):
        start = self._future_monday_ct()
        end = start + timedelta(days=14)
        slots = _generate_candidate_slots(
            from_dt=start, to_dt=end,
            duration_min=30,
            start_hour=13, end_hour=17,
        )
        for slot_start, _ in slots:
            slot_ct = slot_start.astimezone(CT)
            assert slot_ct.weekday() not in (5, 6), (
                f"Slot on weekend: {slot_ct.strftime('%A %Y-%m-%d')}"
            )

    def test_slots_within_business_hours(self):
        start = self._future_monday_ct()
        end = start + timedelta(days=5)
        slots = _generate_candidate_slots(
            from_dt=start, to_dt=end,
            duration_min=30,
            start_hour=13, end_hour=17,
        )
        for slot_start, slot_end in slots:
            s = slot_start.astimezone(CT)
            e = slot_end.astimezone(CT)
            assert s.hour >= 13, f"Slot starts before 1PM CT: {s}"
            assert e.hour <= 17, f"Slot ends after 5PM CT: {e}"

    def test_each_slot_is_30_minutes(self):
        start = self._future_monday_ct()
        end = start + timedelta(days=5)
        slots = _generate_candidate_slots(
            from_dt=start, to_dt=end,
            duration_min=30,
            start_hour=13, end_hour=17,
        )
        for slot_start, slot_end in slots:
            duration = (slot_end - slot_start).total_seconds() / 60
            assert duration == 30, f"Slot duration is {duration} min, expected 30"


# ── Calendar: Free/Busy Overlap Tests ────────────────────────────────────────

class TestFreeBusyOverlap:

    def _utc(self, hour: int, minute: int = 0) -> datetime:
        """Helper to create a datetime at today + 14 days at given UTC hour."""
        base = datetime.now(timezone.utc) + timedelta(days=14)
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_no_busy_periods_returns_free(self):
        assert _is_free(self._utc(14), self._utc(14, 30), []) is True

    def test_exact_overlap_returns_busy(self):
        slot_s, slot_e = self._utc(14), self._utc(14, 30)
        busy_s, busy_e = self._utc(14), self._utc(14, 30)
        assert _is_free(slot_s, slot_e, [(busy_s, busy_e)]) is False

    def test_partial_overlap_at_start_returns_busy(self):
        slot_s, slot_e = self._utc(14), self._utc(14, 30)
        busy_s, busy_e = self._utc(13, 45), self._utc(14, 15)
        assert _is_free(slot_s, slot_e, [(busy_s, busy_e)]) is False

    def test_partial_overlap_at_end_returns_busy(self):
        slot_s, slot_e = self._utc(14), self._utc(14, 30)
        busy_s, busy_e = self._utc(14, 15), self._utc(14, 45)
        assert _is_free(slot_s, slot_e, [(busy_s, busy_e)]) is False

    def test_busy_before_slot_returns_free(self):
        slot_s, slot_e = self._utc(14), self._utc(14, 30)
        busy_s, busy_e = self._utc(13), self._utc(14)   # Ends exactly at slot start
        assert _is_free(slot_s, slot_e, [(busy_s, busy_e)]) is True

    def test_busy_after_slot_returns_free(self):
        slot_s, slot_e = self._utc(14), self._utc(14, 30)
        busy_s, busy_e = self._utc(14, 30), self._utc(15)  # Starts exactly at slot end
        assert _is_free(slot_s, slot_e, [(busy_s, busy_e)]) is True

    def test_multiple_busy_periods_one_overlapping(self):
        slot_s, slot_e = self._utc(15), self._utc(15, 30)
        busy = [
            (self._utc(13), self._utc(14)),
            (self._utc(15, 15), self._utc(16)),   # Overlaps!
            (self._utc(16, 30), self._utc(17)),
        ]
        assert _is_free(slot_s, slot_e, busy) is False
