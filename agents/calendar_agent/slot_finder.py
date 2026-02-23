"""
GCal Slot Finder — Gate 6

1. Authenticates with Google Calendar API using saved token
2. Queries free/busy for the executive's calendar (14–28 days ahead)
3. Splits available time into 30-minute candidate slots (Mon-Fri, 1-5 PM CT)
4. Picks the EARLIEST available slot
5. Creates a tentative hold event (status=tentative)
6. Dispatches slot.found or no.slots to Orchestrator

Timezone handling: all internal math in UTC, displayed in CT for the PA/exec.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from typing import Optional
import pytz

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic_settings import BaseSettings

from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.models import TaskType

log = logging.getLogger(__name__)

CT = pytz.timezone("America/Chicago")


class CalendarSettings(BaseSettings):
    GCAL_TOKEN_PATH:       str = "auth/gcalendar_token.json"
    GCAL_CALENDAR_ID:      str = "primary"
    GCAL_SLOT_MIN_DAYS:    int = 14
    GCAL_SLOT_MAX_DAYS:    int = 28
    GCAL_APPT_DURATION_MIN: int = 30
    GCAL_SLOT_START_HOUR:  int = 13    # 1 PM CT
    GCAL_SLOT_END_HOUR:    int = 17    # 5 PM CT
    ORCHESTRATOR_URL:      str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = CalendarSettings()


def _get_gcal_service():
    """Build authenticated GCal service from saved token."""
    token_path = Path(_settings.GCAL_TOKEN_PATH)
    if not token_path.exists():
        raise RuntimeError(
            f"GCal token not found at {token_path}. "
            "Run: python scripts/generate_tokens.py"
        )
    creds = Credentials.from_authorized_user_file(
        str(token_path),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def _generate_candidate_slots(
    from_dt: datetime,
    to_dt: datetime,
    duration_min: int,
    start_hour: int,
    end_hour: int,
) -> list[tuple[datetime, datetime]]:
    """
    Generate all possible 30-minute slots within business hours
    (Mon–Fri, start_hour–end_hour CT) between from_dt and to_dt.
    Returns list of (slot_start_utc, slot_end_utc) tuples.
    """
    slots = []
    current = from_dt.astimezone(CT).replace(
        hour=start_hour, minute=0, second=0, microsecond=0,
    )
    # If today's start_hour has already passed, move to next day
    if current < datetime.now(CT):
        current += timedelta(days=1)
        current = current.replace(hour=start_hour, minute=0, second=0, microsecond=0)

    delta = timedelta(minutes=duration_min)
    while current < to_dt.astimezone(CT):
        # Skip weekends
        if current.weekday() in (5, 6):
            current += timedelta(days=1)
            current = current.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            continue
        # Skip before start or after end of business window
        if current.hour < start_hour:
            current = current.replace(hour=start_hour, minute=0)
        if current.hour >= end_hour:
            current += timedelta(days=1)
            current = current.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            continue

        slot_end = current + delta
        if slot_end.astimezone(CT).hour <= end_hour:
            slots.append((
                current.astimezone(timezone.utc),
                slot_end.astimezone(timezone.utc),
            ))
        current += delta

    return slots


async def find_and_hold(payload: dict) -> None:
    """
    Main slot-finding logic. Queries GCal free/busy, finds first open 30-min
    slot, creates a tentative hold, dispatches result to Orchestrator.
    """
    pipeline_id = payload.get("pipeline_id")
    ein         = payload.get("ein", "")
    org_name    = payload.get("org_name", "")

    # If org_name not in payload, fetch from DB
    if not org_name and pipeline_id:
        from shared.db import fetch_one
        row = await fetch_one("SELECT org_name FROM pipeline WHERE id = $1", pipeline_id)
        if row:
            org_name = row["org_name"] or ""

    log.info("Finding slot for: ein=%s org='%s'", ein, org_name)

    now_ct = datetime.now(CT)
    search_start = now_ct + timedelta(days=_settings.GCAL_SLOT_MIN_DAYS)
    search_end   = now_ct + timedelta(days=_settings.GCAL_SLOT_MAX_DAYS)

    try:
        service = _get_gcal_service()
    except Exception as e:
        log.error("GCal auth failed: %s", e)
        await _dispatch_no_slots(pipeline_id, ein, str(e))
        return

    # ── Free/busy query ───────────────────────────────────────────────────────
    try:
        freebusy_resp = service.freebusy().query(body={
            "timeMin": search_start.astimezone(timezone.utc).isoformat(),
            "timeMax": search_end.astimezone(timezone.utc).isoformat(),
            "timeZone": "UTC",
            "items": [{"id": _settings.GCAL_CALENDAR_ID}],
        }).execute()
    except HttpError as e:
        log.error("GCal free/busy query failed: %s", e)
        await _dispatch_no_slots(pipeline_id, ein, str(e))
        return

    busy_periods = freebusy_resp.get("calendars", {}).get(
        _settings.GCAL_CALENDAR_ID, {}
    ).get("busy", [])

    busy_ranges = [
        (
            datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
            datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
        )
        for b in busy_periods
    ]

    # ── Find first free slot ──────────────────────────────────────────────────
    candidates = _generate_candidate_slots(
        from_dt=search_start,
        to_dt=search_end,
        duration_min=_settings.GCAL_APPT_DURATION_MIN,
        start_hour=_settings.GCAL_SLOT_START_HOUR,
        end_hour=_settings.GCAL_SLOT_END_HOUR,
    )

    chosen: Optional[tuple[datetime, datetime]] = None
    for (slot_start, slot_end) in candidates:
        if _is_free(slot_start, slot_end, busy_ranges):
            chosen = (slot_start, slot_end)
            break

    if not chosen:
        log.info("No available slots for pipeline %s", pipeline_id)
        await _dispatch_no_slots(pipeline_id, ein, "No slots in look-ahead window")
        return

    slot_start, slot_end = chosen

    # ── Create tentative hold event ───────────────────────────────────────────
    hold_event = {
        "summary":     f"[HOLD] Charity Meeting — {org_name}",
        "description": f"Pending PA approval. EIN: {ein}. Pipeline: {pipeline_id}",
        "start":       {"dateTime": slot_start.isoformat(), "timeZone": "UTC"},
        "end":         {"dateTime": slot_end.isoformat(),   "timeZone": "UTC"},
        "status":      "tentative",
        "transparency": "opaque",
        "colorId":     "5",   # Banana yellow — visuall flag as hold
        "reminders":   {"useDefault": False, "overrides": []},
    }

    try:
        created = service.events().insert(
            calendarId=_settings.GCAL_CALENDAR_ID,
            body=hold_event,
        ).execute()
        gcal_hold_id = created.get("id")
    except HttpError as e:
        log.error("Failed to create GCal hold event: %s", e)
        await _dispatch_no_slots(pipeline_id, ein, f"GCal insert failed: {e}")
        return

    log.info(
        "Hold created: pipeline=%s slot=%s gcal_id=%s",
        pipeline_id, slot_start.isoformat(), gcal_hold_id,
    )

    # ── Dispatch slot.found ───────────────────────────────────────────────────
    slot_ct = slot_start.astimezone(CT)
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.SLOT_FOUND,
            payload={
                "pipeline_id": pipeline_id,
                "ein":         ein,
                "proposed_slot": {
                    "start":        slot_start.isoformat(),
                    "end":          slot_end.isoformat(),
                    "gcal_hold_id": gcal_hold_id,
                    "display_ct":   slot_ct.strftime("%A, %B %-d at %-I:%M %p CT"),
                },
            },
            pipeline_id=pipeline_id,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch slot.found: %s", e)


def _is_free(
    slot_start: datetime,
    slot_end: datetime,
    busy_ranges: list[tuple[datetime, datetime]],
) -> bool:
    """Return True if the slot does not overlap any busy range."""
    for busy_start, busy_end in busy_ranges:
        # Overlap check: NOT (slot_end <= busy_start OR slot_start >= busy_end)
        if not (slot_end <= busy_start or slot_start >= busy_end):
            return False
    return True


async def _dispatch_no_slots(
    pipeline_id: Optional[str],
    ein: str,
    reason: str,
) -> None:
    """Dispatch no.slots to Orchestrator — org stays in priority queue."""
    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.NO_SLOTS,
            payload={
                "pipeline_id": pipeline_id,
                "ein":         ein,
                "reason":      reason,
            },
            pipeline_id=pipeline_id,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch no.slots: %s", e)
