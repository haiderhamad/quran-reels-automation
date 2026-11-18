"""Tests for dynamic Europe/Berlin Quran publishing scheduler heartbeat.

Verifies requirements A through Q (Section 21 of Phase 8F specification).
"""

from __future__ import annotations

import datetime
from typing import Any, Optional
import unittest

from engine.scheduler import (
    DEFAULT_GRACE_WINDOW_MINUTES,
    berlin_now,
    is_slot_due,
)
from engine.storage import PublishingScheduleEntry, RunRecord, StoredConfig
from scripts.run_publishing_scheduler import evaluate_and_claim_due_slot


class MockSupabaseStore:
    def __init__(self, config: Optional[StoredConfig] = None) -> None:
        self.config: Optional[StoredConfig] = config
        self.claims: dict[tuple[str, str], dict[str, Any]] = {}
        self.run_records: list[RunRecord] = []

    def load_config(self) -> Optional[StoredConfig]:
        return self.config

    def claim_scheduler_slot(
        self,
        berlin_date: str,
        schedule_slot_id: str,
        claimed_at: Optional[str] = None,
        worker_run_id: Optional[str] = None,
    ) -> bool:
        key = (berlin_date, schedule_slot_id)
        if key in self.claims:
            return False
        run_id = worker_run_id or f"claim_{berlin_date}_{schedule_slot_id}"
        self.claims[key] = {
            "berlin_date": berlin_date,
            "slot_id": schedule_slot_id,
            "run_id": run_id,
            "status": "CLAIMED",
        }
        return True

    def is_scheduler_slot_claimed(
        self, berlin_date: str, schedule_slot_id: str
    ) -> bool:
        return (berlin_date, schedule_slot_id) in self.claims

    def get_scheduler_claim(
        self, berlin_date: str, schedule_slot_id: str
    ) -> Optional[dict[str, Any]]:
        return self.claims.get((berlin_date, schedule_slot_id))

    def get_run_record(
        self,
        date: str,
        slot: str,
        run_id: Optional[str] = None,
        run_kind: Optional[str] = None,
    ) -> Optional[RunRecord]:
        for rec in self.run_records:
            if rec.date != date:
                continue
            if slot and slot not in ("manual-production", "test"):
                if rec.schedule_slot_id != slot:
                    continue
            if run_kind and rec.run_kind != run_kind:
                continue
            if run_id and rec.run_id != run_id:
                continue
            return rec
        return None

    def update_scheduler_claim_status(
        self,
        berlin_date: str,
        schedule_slot_id: str,
        status: str,
        failure_reason: Optional[str] = None,
    ) -> None:
        key = (berlin_date, schedule_slot_id)
        if key in self.claims:
            self.claims[key]["status"] = status
            if failure_reason:
                self.claims[key]["failure_reason"] = failure_reason


class TestPublishingScheduler(unittest.TestCase):
    """Verify requirements A through Q."""

    def test_A_automation_disabled_causes_no_scheduled_run(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=False,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)  # 09:05 Berlin
        active, due_slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertFalse(active)
        self.assertIsNone(due_slot)
        self.assertEqual(len(store.claims), 0)

    def test_B_enabled_09_00_slot_at_berlin_09_xx_is_claimed_once(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00", enabled=True),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 7, tzinfo=datetime.timezone.utc)  # 09:07 Berlin
        active, due_slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertTrue(active)
        self.assertIsNotNone(due_slot)
        assert due_slot is not None
        self.assertEqual(due_slot["slot_id"], "morning")
        self.assertEqual(due_slot["run_id"], "scheduled_20260905_morning")
        self.assertTrue(store.is_scheduler_slot_claimed("2026-09-05", "morning"))

    def test_C_disabled_slot_is_skipped(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00", enabled=False),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)  # 09:05 Berlin
        active, due_slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertTrue(active)
        self.assertIsNone(due_slot)
        self.assertEqual(len(store.claims), 0)

    def test_D_same_slot_and_date_checked_twice_yields_only_one_claim(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        now1 = datetime.datetime(2026, 9, 5, 7, 3, tzinfo=datetime.timezone.utc)  # 09:03 Berlin
        now2 = datetime.datetime(2026, 9, 5, 7, 15, tzinfo=datetime.timezone.utc)  # 09:15 Berlin
        _, slot1 = evaluate_and_claim_due_slot(store, now=now1)
        self.assertIsNotNone(slot1)
        self.assertEqual(len(store.claims), 1)

        _, slot2 = evaluate_and_claim_due_slot(store, now=now2)
        self.assertIsNone(slot2)
        self.assertEqual(len(store.claims), 1)

    def test_E_only_approved_times_are_dispatched(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(
                    PublishingScheduleEntry(id="morning", time="09:00"),
                    PublishingScheduleEntry(id="slot-00000000-0000-4000-8000-000000000002", time="16:00"),
                    PublishingScheduleEntry(id="slot-20", time="20:00", enabled=False),
                ),
            )
        )
        # At 16:03 Berlin, slot-00000000-0000-4000-8000-000000000002 is due and claimed
        now_15 = datetime.datetime(2026, 9, 5, 14, 3, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now_15)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["slot_id"], "slot-00000000-0000-4000-8000-000000000002")

    def test_F_evening_approved_identity_is_dispatched(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="slot-00000000-0000-4000-8000-000000000002", time="16:00"),),
            )
        )
        # At 16:04 Berlin the configured evening cadence identity is due.
        now = datetime.datetime(2026, 9, 5, 14, 4, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["slot_id"], "slot-00000000-0000-4000-8000-000000000002")

    def test_G_europe_berlin_dst_conversion_in_winter_and_summer(self) -> None:
        # Summer (CEST = UTC+2): 07:05 UTC is 09:05 Berlin
        summer_utc = datetime.datetime(2026, 7, 15, 7, 5, tzinfo=datetime.timezone.utc)
        summer_berlin = berlin_now(summer_utc)
        self.assertEqual(summer_berlin.strftime("%H:%M %z"), "09:05 +0200")
        self.assertTrue(is_slot_due("09:00", now=summer_utc))

        # Winter (CET = UTC+1): 08:05 UTC is 09:05 Berlin
        winter_utc = datetime.datetime(2026, 1, 15, 8, 5, tzinfo=datetime.timezone.utc)
        winter_berlin = berlin_now(winter_utc)
        self.assertEqual(winter_berlin.strftime("%H:%M %z"), "09:05 +0100")
        self.assertTrue(is_slot_due("09:00", now=winter_utc))

    def test_H_and_I_scheduled_run_kind_and_approved_slot_id_preserved(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["slot_id"], "morning")
        self.assertTrue(slot["run_id"].startswith("scheduled_20260905_morning"))

    def test_J_failed_claim_causes_no_duplicate_dispatch(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        # Pre-seed claim by another concurrent worker
        store.claims[("2026-09-05", "morning")] = {"status": "CLAIMED"}
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNone(slot)

    def test_K_facebook_and_instagram_settings_propagate_correctly(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                facebook_enabled=True,
                instagram_enabled=False,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["publish_facebook"], "true")
        self.assertEqual(slot["publish_instagram"], "false")

    def test_L_test_runs_never_consume_scheduled_claims(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        # Add a completed test run
        store.run_records.append(
            RunRecord(
                run_id="test_run_123",
                date="2026-09-05",
                slot="test",
                status="SUCCESS",
                started_at="2026-09-05T07:00:00Z",
                run_kind="test",
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        self.assertEqual(len(store.claims), 1)

    def test_M_approved_morning_slot_runs_today_if_unclaimed(self) -> None:
        # Approved morning slot has no existing claim.
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)  # 09:05 Berlin
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["slot_id"], "morning")
        self.assertTrue(store.is_scheduler_slot_claimed("2026-09-05", "morning"))

    def test_N_approved_evening_slot_runs_today_if_unclaimed(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="slot-00000000-0000-4000-8000-000000000002", time="16:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 14, 3, tzinfo=datetime.timezone.utc)  # 16:03 Berlin
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["slot_id"], "slot-00000000-0000-4000-8000-000000000002")

    def test_O_slot_already_claimed_today_changed_to_new_time_does_not_run_again(self) -> None:
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                # Morning was changed to 09:00 after running at 08:00
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        # Pre-existing claim for morning today
        store.claims[("2026-09-05", "morning")] = {
            "status": "DISPATCHED",
            "run_id": "scheduled_20260905_morning",
        }
        # At 09:05 Berlin:
        now = datetime.datetime(2026, 9, 5, 7, 5, tzinfo=datetime.timezone.utc)
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNone(slot)

    def test_P_approved_slot_outside_grace_window_does_not_catch_up(self) -> None:
        # Evening slot at 16:50 is outside the 45-minute grace window.
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="slot-00000000-0000-4000-8000-000000000002", time="16:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 14, 50, tzinfo=datetime.timezone.utc)  # 16:50 Berlin
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNone(slot)
        self.assertEqual(len(store.claims), 0)

    def test_Q_time_changed_to_time_inside_grace_window_may_run_once(self) -> None:
        # User changes slot to 09:00 at 09:20 (elapsed = 20 min <= 45 min)
        store = MockSupabaseStore(
            StoredConfig(
                automation_enabled=True,
                publishing_schedule=(PublishingScheduleEntry(id="morning", time="09:00"),),
            )
        )
        now = datetime.datetime(2026, 9, 5, 7, 20, tzinfo=datetime.timezone.utc)  # 09:20 Berlin
        _, slot = evaluate_and_claim_due_slot(store, now=now)
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertEqual(slot["slot_id"], "morning")
        self.assertEqual(len(store.claims), 1)


if __name__ == "__main__":
    unittest.main()
