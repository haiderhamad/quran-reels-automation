"""Tests for provider-independent dynamic daily scheduling."""

from __future__ import annotations

import datetime
import unittest

from engine.scheduler import (
    berlin_now,
    due_schedule_entries,
    next_schedule_occurrences,
    scheduler_tick,
)
from engine.storage import PublishingScheduleEntry, StoredConfig


class TestSchedulerTimeLogic(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = (
            PublishingScheduleEntry(id="morning", time="08:00"),
            PublishingScheduleEntry(id="midday", time="13:00"),
            PublishingScheduleEntry(id="evening", time="20:00"),
            PublishingScheduleEntry(id="disabled", time="07:00", enabled=False),
        )

    def test_berlin_dst_summer_and_winter(self) -> None:
        summer = berlin_now(datetime.datetime(2026, 8, 24, 6, 0, tzinfo=datetime.timezone.utc))
        winter = berlin_now(datetime.datetime(2026, 1, 24, 7, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(summer.strftime("%H:%M %z"), "08:00 +0200")
        self.assertEqual(winter.strftime("%H:%M %z"), "08:00 +0100")

    def test_due_and_not_due_entries(self) -> None:
        # At 13:30 Berlin, midday (13:00) is within 45m grace window; morning (08:00) is expired
        now_midday = datetime.datetime(2026, 8, 24, 11, 30, tzinfo=datetime.timezone.utc)  # 13:30 Berlin
        self.assertEqual(
            tuple(entry.id for entry in due_schedule_entries(self.entries, now_midday)),
            ("midday",),
        )
        # At 08:20 Berlin, morning (08:00) is within 45m grace window
        now_morning = datetime.datetime(2026, 8, 24, 6, 20, tzinfo=datetime.timezone.utc)  # 08:20 Berlin
        self.assertEqual(
            tuple(entry.id for entry in due_schedule_entries(self.entries, now_morning)),
            ("morning",),
        )

    def test_spring_forward_missed_wall_time_is_caught_up(self) -> None:
        entry = PublishingScheduleEntry(id="spring", time="02:30")
        # 01:05 UTC is 03:05 CEST after the skipped local hour (35m elapsed <= 45m grace).
        now = datetime.datetime(2026, 3, 29, 1, 5, tzinfo=datetime.timezone.utc)
        self.assertEqual(tuple(item.id for item in due_schedule_entries((entry,), now)), ("spring",))

    def test_fall_back_repeat_has_same_local_date_and_slot_identity(self) -> None:
        first = berlin_now(datetime.datetime(2026, 10, 25, 0, 30, tzinfo=datetime.timezone.utc))
        second = berlin_now(datetime.datetime(2026, 10, 25, 1, 30, tzinfo=datetime.timezone.utc))
        self.assertEqual(first.strftime("%Y-%m-%d %H:%M"), "2026-10-25 02:30")
        self.assertEqual(second.strftime("%Y-%m-%d %H:%M"), "2026-10-25 02:30")

    def test_next_schedule_calculation_rolls_to_tomorrow(self) -> None:
        now = datetime.datetime(2026, 8, 24, 19, 0, tzinfo=datetime.timezone.utc)  # 21:00 Berlin
        upcoming = next_schedule_occurrences(self.entries, now, limit=4)
        self.assertEqual(
            tuple((item.local_date, item.local_time) for item in upcoming),
            (
                ("2026-08-25", "08:00"),
                ("2026-08-25", "13:00"),
                ("2026-08-25", "20:00"),
                ("2026-08-26", "08:00"),
            ),
        )


class TestSchedulerTick(unittest.TestCase):
    def test_paused_automation_dispatches_nothing(self) -> None:
        config = StoredConfig(
            automation_enabled=False,
            publishing_schedule=(PublishingScheduleEntry(id="slot-a", time="08:00"),),
        )
        dispatched: list[tuple[str, str]] = []
        result = scheduler_tick(
            config,
            run_exists=lambda _date, _slot: False,
            claim_slot=lambda _date, _slot: True,
            dispatch_slot=lambda date, slot: dispatched.append((date, slot)),
            now=datetime.datetime(2026, 8, 24, 6, 10, tzinfo=datetime.timezone.utc),
        )
        self.assertFalse(result.automation_enabled)
        self.assertEqual(dispatched, [])

    def test_tick_dispatches_only_missing_atomically_claimed_due_slots(self) -> None:
        config = StoredConfig(
            automation_enabled=True,
            publishing_schedule=(
                PublishingScheduleEntry(id="morning", time="09:00"),
                PublishingScheduleEntry(id="extra", time="13:00", enabled=False),
                PublishingScheduleEntry(id="slot-00000000-0000-4000-8000-000000000002", time="16:00"),
            ),
        )
        for existing, claim, dispatched, skipped in (
            (False, True, ("morning",), ()),
            (True, True, (), ("morning",)),
            (False, False, (), ("morning",)),
        ):
            result = scheduler_tick(
                config,
                run_exists=lambda _date, _slot: existing,
                claim_slot=lambda _date, _slot: claim,
                dispatch_slot=lambda _date, _slot: None,
                now=datetime.datetime(2026, 8, 24, 7, 20, tzinfo=datetime.timezone.utc),
            )
            self.assertEqual(result.due_slot_ids, ("morning",))
            self.assertEqual(result.dispatched_slot_ids, dispatched)
            self.assertEqual(result.skipped_slot_ids, skipped)

    def test_dispatch_failure_is_reported_after_claim(self) -> None:
        config = StoredConfig(
            automation_enabled=True,
            publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
        )
        result = scheduler_tick(
            config,
            run_exists=lambda _date, _slot: False,
            claim_slot=lambda _date, _slot: True,
            dispatch_slot=lambda _date, _slot: (_ for _ in ()).throw(RuntimeError("network")),
            now=datetime.datetime(2026, 8, 24, 7, 15, tzinfo=datetime.timezone.utc),  # 09:15 Berlin
        )
        self.assertEqual(result.failed_slot_ids, ("morning",))


if __name__ == "__main__":
    unittest.main()
