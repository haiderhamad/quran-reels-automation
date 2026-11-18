"""Archive planning is independent of B2 object listing and Google Drive."""

from __future__ import annotations

import tempfile
import unittest

from engine.archive import ArchiveError, archive_manifest, build_monthly_archive_plan, sha256_file, zip_member_name
from engine.storage import RunRecord


def record(run_id: str, *, kind: str = "scheduled", status: str = "SUCCESS", safe: bool = True, media_key: str | None = "reels/2026/08/r.mp4") -> RunRecord:
    return RunRecord(run_id=run_id, date="2026-08-28", slot="slot-a" if kind == "scheduled" else "test", status=status, started_at="2026-08-28T00:00:00Z", run_kind=kind, schedule_slot_id="slot-a" if kind == "scheduled" else None, final_safety_passed=safe, media_key=media_key)


class ArchiveTests(unittest.TestCase):
    def test_selects_final_safe_production_only(self):
        plan = build_monthly_archive_plan(2026, 8, [record("scheduled"), record("test", kind="test"), record("failed", status="FAILED"), record("unsafe", safe=False)])
        self.assertEqual([item.run_id for item in plan.entries], ["scheduled"])
        self.assertEqual(plan.archive_name, "quran-reels-2026-08.zip")

    def test_manifest_is_exact_and_no_cleanup_is_implied(self):
        plan = build_monthly_archive_plan(2026, 8, [record("one")])
        manifest = archive_manifest(plan, [{"run_id": "one", "b2_object_key": "reels/2026/08/one.mp4", "byte_size": 5, "sha256": "a" * 64}], "b" * 64, "2026-09-01T00:00:00Z")
        self.assertEqual(manifest["run_ids"], ["one"])
        self.assertNotIn("delete", str(manifest).lower())

    def test_hash_and_member_name_are_safe(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"quran")
            handle.flush()
            self.assertEqual(sha256_file(handle.name), "8987fb9276868758b95cfacff8c2845c8bcfb905819b8e37cc3260da4f25e5aa")
        plan = build_monthly_archive_plan(2026, 8, [])
        self.assertEqual(zip_member_name(plan, "run_123"), "2026-08/run_123.mp4")
        with self.assertRaises(ArchiveError):
            zip_member_name(plan, "../bad")
