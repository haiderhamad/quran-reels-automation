from __future__ import annotations

import unittest

from engine.job import _record_production_pool_usage
from engine.storage import RunRecord


class UsageRepo:
    def __init__(self): self.events = []
    def save_usage(self, event): self.events.append(event); return event.run_id


def record(run_kind: str, mode: str = "VERIFIED_POOL") -> RunRecord:
    slot = "test" if run_kind == "test" else "morning"
    return RunRecord(
        run_id=f"run-{run_kind}",
        date="2026-08-26",
        slot=slot,
        run_kind=run_kind,
        status="SUCCESS",
        started_at="2026-08-26T00:00:00+00:00",
        duration_seconds=15.0,
        provenance={
            "background_source_mode": mode,
            "verified_backgrounds": [{
                "provider": "pexels",
                "provider_asset_id": "123",
                "provider_file_id": "456",
                "verified_record_id": "pexels:123:456:hash:policy",
            }],
        },
    )


class TestBackgroundPoolUsageCheckpoint(unittest.TestCase):
    def test_test_mode_never_records_production_usage(self):
        repo = UsageRepo()
        _record_production_pool_usage(repo, record("test"), used_at="2026-08-26T01:00:00+00:00")
        self.assertEqual(repo.events, [])

    def test_live_search_never_records_pool_usage(self):
        repo = UsageRepo()
        _record_production_pool_usage(repo, record("scheduled", "LIVE_SEARCH_FALLBACK"), used_at="2026-08-26T01:00:00+00:00")
        self.assertEqual(repo.events, [])

    def test_successful_production_checkpoint_records_append_only_event(self):
        repo = UsageRepo()
        _record_production_pool_usage(repo, record("scheduled"), used_at="2026-08-26T01:00:00+00:00")
        self.assertEqual(len(repo.events), 1)
        self.assertEqual(repo.events[0].provider_asset_id, "123")
        self.assertEqual(repo.events[0].source_mode, "VERIFIED_POOL")


if __name__ == "__main__": unittest.main()
