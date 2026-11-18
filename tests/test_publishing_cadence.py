"""Production cadence gates retain Berlin calendar and durable identities."""
import contextlib
import datetime
import io
import unittest

from engine.scheduler import PRODUCTION_PUBLISHING_SLOTS, production_schedule_entries
from engine.storage import PublishingScheduleEntry, StoredConfig, stored_config_from_dict, stored_config_to_dict
from scripts.run_publishing_scheduler import evaluate_and_claim_due_slot
from tests.test_publishing_scheduler import MockSupabaseStore


class TestProductionCadence(unittest.TestCase):
    def entries(self):
        return tuple(PublishingScheduleEntry(id=slot, time=time) for slot, time in list(PRODUCTION_PUBLISHING_SLOTS.items())[:2])

    def evaluate(self, store, now, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return evaluate_and_claim_due_slot(store, now=now, **kwargs)

    def test_two_editable_admin_times_with_stable_claim_ids(self):
        self.assertEqual(tuple(PRODUCTION_PUBLISHING_SLOTS.values()), ('09:00', '16:00', '20:00'))
        editable = (PublishingScheduleEntry(id='morning', time='10:25'),
                    PublishingScheduleEntry(id='slot-00000000-0000-4000-8000-000000000002', time='18:40'))
        self.assertEqual(production_schedule_entries(editable), editable)
        self.assertEqual(production_schedule_entries(
            editable + (PublishingScheduleEntry(id='old', time='20:00', enabled=False),)
        ), editable)
        # A third approved stable slot is allowed; an unknown active one blocks dispatch.
        third = PublishingScheduleEntry(id='slot-third', time='21:00')
        self.assertEqual(production_schedule_entries(editable + (third,)), editable + (third,))
        self.assertEqual(production_schedule_entries(
            editable + (PublishingScheduleEntry(id='third', time='21:00'),)
        ), ())
        self.assertEqual(production_schedule_entries(
            (PublishingScheduleEntry(id='morning', time='10:25'),)
        ), (PublishingScheduleEntry(id='morning', time='10:25'),))
        self.assertEqual(production_schedule_entries(
            (PublishingScheduleEntry(id='one', time='10:25'),
             PublishingScheduleEntry(id='two', time='10:25'))
        ), ())

    def test_one_or_three_admin_times_use_stable_ids(self):
        first = PublishingScheduleEntry(id='morning', time='10:00')
        second = PublishingScheduleEntry(id='slot-00000000-0000-4000-8000-000000000002', time='18:00')
        third = PublishingScheduleEntry(id='slot-third', time='21:00')
        self.assertEqual(production_schedule_entries((first,)), (first,))
        self.assertEqual(production_schedule_entries((first, second, third)), (first, second, third))
        for hour, expected_id in ((8, first.id), (16, second.id), (19, third.id)):
            store = MockSupabaseStore(StoredConfig(
                automation_enabled=True, publishing_schedule=(first, second, third),
            ))
            _, claimed = self.evaluate(store, datetime.datetime(
                2026, 8, 24, hour, 5, tzinfo=datetime.timezone.utc,
            ))
            self.assertEqual(claimed['slot_id'], expected_id)

    def test_ten_minute_ticks_across_dst_days_claim_two_admin_times_once(self):
        custom = (PublishingScheduleEntry(id='morning', time='10:25'),
                  PublishingScheduleEntry(id='slot-00000000-0000-4000-8000-000000000002', time='18:40'))
        for date in (datetime.date(2026, 3, 29), datetime.date(2026, 10, 25)):
            store = MockSupabaseStore(StoredConfig(
                automation_enabled=True,
                publishing_schedule=custom + (PublishingScheduleEntry(id='extra', time='13:00', enabled=False),),
            ))
            results = []
            for hour in range(24):
                for minute in (3, 13, 23, 33, 43, 53):
                    now = datetime.datetime.combine(
                        date, datetime.time(hour, minute), tzinfo=datetime.timezone.utc,
                    )
                    for _ in range(2):
                        _, slot = self.evaluate(store, now)
                        if slot:
                            results.append(slot)
            self.assertEqual(len(results), 2)
            self.assertEqual({result['slot_id'] for result in results}, {e.id for e in custom})
            self.assertEqual(len(store.claims), 2)
            self.assertEqual({key[0] for key in store.claims}, {date.isoformat()})

    def test_same_day_time_edits_keep_claims_and_cannot_double_post(self):
        entries = self.entries()
        store = MockSupabaseStore(StoredConfig(automation_enabled=True, publishing_schedule=entries))
        for hour in (7, 14):
            self.evaluate(store, datetime.datetime(2026, 9, 5, hour, 3, tzinfo=datetime.timezone.utc))
        self.assertEqual(len(store.claims), 2)
        store.config = StoredConfig(
            automation_enabled=True,
            publishing_schedule=(
                PublishingScheduleEntry(id=entries[0].id, time='10:25'),
                PublishingScheduleEntry(id=entries[1].id, time='18:40'),
            ),
        )
        for hour, minute in ((8, 33), (16, 43)):
            _, slot = self.evaluate(
                store, datetime.datetime(2026, 9, 5, hour, minute, tzinfo=datetime.timezone.utc),
            )
            self.assertIsNone(slot)
        self.assertEqual(len(store.claims), 2)

    def test_config_roundtrip_preserves_historical_slots_and_visual_settings(self):
        entries = self.entries() + (PublishingScheduleEntry(id='evening', time='13:00', enabled=False),)
        original = StoredConfig(publishing_schedule=entries, arabic_font_size=160, instagram_enabled=False)
        restored = stored_config_from_dict(stored_config_to_dict(original))
        self.assertEqual(restored.publishing_schedule, entries)
        self.assertEqual(restored.timezone, 'Europe/Berlin')
        self.assertEqual(restored.arabic_font_size, 160)
        self.assertFalse(restored.instagram_enabled)
