"""Provider-independent Europe/Berlin scheduling decisions.

This module deliberately does not activate or call a scheduler provider.  The
caller supplies durable run lookup, atomic claim, and dispatch callbacks.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from engine.storage import PublishingScheduleEntry, StoredConfig


BERLIN_TIMEZONE = "Europe/Berlin"
_CADENCE = json.loads((Path(__file__).resolve().parents[1] / "data" / "publishing_cadence.json").read_text(encoding="utf-8"))
PRODUCTION_PUBLISHING_SLOTS = {entry["id"]: entry["time"] for entry in _CADENCE["slots"]}
PRODUCTION_PUBLISHING_TIMES = tuple(PRODUCTION_PUBLISHING_SLOTS.values())
if (_CADENCE.get("version") != 1 or _CADENCE.get("timezone") != BERLIN_TIMEZONE
        or len(PRODUCTION_PUBLISHING_SLOTS) != 3
        or PRODUCTION_PUBLISHING_TIMES != ("09:00", "16:00", "20:00")):
    raise ValueError("PUBLISHING_CADENCE_INVALID")


@dataclass(frozen=True)
class ScheduledOccurrence:
    slot_id: str
    local_date: str
    local_time: str


@dataclass(frozen=True)
class SchedulerTickResult:
    local_date: str
    local_time: str
    due_slot_ids: tuple[str, ...]
    dispatched_slot_ids: tuple[str, ...]
    skipped_slot_ids: tuple[str, ...]
    failed_slot_ids: tuple[str, ...]
    automation_enabled: bool


def berlin_now(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    """Return an aware Europe/Berlin datetime, interpreting naive input as UTC."""
    timezone = ZoneInfo(BERLIN_TIMEZONE)
    current = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    return current.astimezone(timezone)


def sorted_enabled_schedule(
    entries: Sequence[PublishingScheduleEntry],
) -> tuple[PublishingScheduleEntry, ...]:
    return tuple(sorted((entry for entry in entries if entry.enabled), key=lambda entry: (entry.time, entry.id)))


def production_schedule_entries(
    entries: Sequence[PublishingScheduleEntry],
) -> tuple[PublishingScheduleEntry, ...]:
    """Use 1–3 admin-configured Berlin times with stable slot identities.

    The frozen cadence JSON provides defaults, not immutable publication hours.
    The database persists the operator's times; existing claim IDs survive edits.
    Incomplete or duplicate schedules never dispatch.
    """
    active = sorted_enabled_schedule(entries)
    approved = tuple(entry for entry in active if entry.id in PRODUCTION_PUBLISHING_SLOTS)
    if (not 1 <= len(active) <= 3
            or len(approved) != len(active)
            or len({entry.time for entry in approved}) != len(approved)
            or len({entry.id for entry in approved}) != len(approved)):
        return ()
    return approved


DEFAULT_GRACE_WINDOW_MINUTES = 45


def _hhmm_to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":", 1)
    return int(hours) * 60 + int(minutes)


def is_slot_due(
    slot_time: str,
    now: Optional[datetime.datetime] = None,
    grace_window_minutes: int = DEFAULT_GRACE_WINDOW_MINUTES,
) -> bool:
    """Return True if local Berlin wall-clock time is at or within the grace window after slot_time."""
    local_now = berlin_now(now)
    current_minutes = local_now.hour * 60 + local_now.minute
    slot_minutes = _hhmm_to_minutes(slot_time)
    elapsed = current_minutes - slot_minutes
    return 0 <= elapsed <= grace_window_minutes


def due_schedule_entries(
    entries: Sequence[PublishingScheduleEntry],
    now: Optional[datetime.datetime] = None,
    grace_window_minutes: int = DEFAULT_GRACE_WINDOW_MINUTES,
) -> tuple[PublishingScheduleEntry, ...]:
    """Return today's enabled entries whose Berlin wall-clock time is due within the grace window."""
    return tuple(
        entry
        for entry in sorted_enabled_schedule(entries)
        if is_slot_due(entry.time, now=now, grace_window_minutes=grace_window_minutes)
    )


def next_schedule_occurrences(
    entries: Sequence[PublishingScheduleEntry],
    now: Optional[datetime.datetime] = None,
    limit: int = 4,
) -> tuple[ScheduledOccurrence, ...]:
    """Calculate upcoming local schedule labels without converting wall times to UTC."""
    if limit < 1:
        return ()
    enabled = sorted_enabled_schedule(entries)
    if not enabled:
        return ()
    local_now = berlin_now(now)
    current_date = local_now.date()
    current_hhmm = local_now.strftime("%H:%M")
    occurrences: list[ScheduledOccurrence] = []
    day_offset = 0
    while len(occurrences) < limit and day_offset <= 8:
        candidate_date = current_date + datetime.timedelta(days=day_offset)
        for entry in enabled:
            if day_offset == 0 and entry.time <= current_hhmm:
                continue
            occurrences.append(
                ScheduledOccurrence(
                    slot_id=entry.id,
                    local_date=candidate_date.isoformat(),
                    local_time=entry.time,
                )
            )
            if len(occurrences) >= limit:
                break
        day_offset += 1
    return tuple(occurrences)


def scheduler_tick(
    config: StoredConfig,
    *,
    run_exists: Callable[[str, str], bool],
    claim_slot: Callable[[str, str], bool],
    dispatch_slot: Callable[[str, str], None],
    now: Optional[datetime.datetime] = None,
    grace_window_minutes: int = DEFAULT_GRACE_WINDOW_MINUTES,
) -> SchedulerTickResult:
    """Dispatch missing due runs after an atomic durable claim.

    ``claim_slot`` must atomically create a local-date/stable-slot claim and
    return False when it already exists. This makes provider retries harmless;
    B2/provider integration remains outside this pure scheduling module.
    """
    local_now = berlin_now(now)
    local_date = local_now.date().isoformat()
    local_time = local_now.strftime("%H:%M")
    if not config.automation_enabled:
        return SchedulerTickResult(local_date, local_time, (), (), (), (), False)

    due = due_schedule_entries(
        production_schedule_entries(config.publishing_schedule),
        now=now,
        grace_window_minutes=grace_window_minutes,
    )
    dispatched: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for entry in due:
        if run_exists(local_date, entry.id):
            skipped.append(entry.id)
            continue
        if not claim_slot(local_date, entry.id):
            skipped.append(entry.id)
            continue
        try:
            dispatch_slot(local_date, entry.id)
            dispatched.append(entry.id)
        except Exception:
            failed.append(entry.id)

    return SchedulerTickResult(
        local_date=local_date,
        local_time=local_time,
        due_slot_ids=tuple(entry.id for entry in due),
        dispatched_slot_ids=tuple(dispatched),
        skipped_slot_ids=tuple(skipped),
        failed_slot_ids=tuple(failed),
        automation_enabled=True,
    )
