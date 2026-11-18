"""B2-state migration inventory remains read-only, fail-closed, and rerunnable."""

from __future__ import annotations

import unittest
from typing import Any

from engine.storage import StorageError
from scripts.migrate_b2_state_to_supabase import (
    classify_b2_key,
    generate_dry_run_report,
    inventory_b2_objects,
    inventory_state_keys,
    migration_report,
)


class MockPaginator:
    def __init__(self, contents: list[dict[str, Any]]) -> None:
        self.contents = contents

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"Contents": self.contents}]


class ErrorPaginator:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        raise self.error


class MockClient:
    def __init__(self, contents: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.contents = contents or []
        self.error = error

    def get_paginator(self, _name: str) -> Any:
        if self.error:
            return ErrorPaginator(self.error)
        return MockPaginator(self.contents)


class StateMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_contents = [
            {"Key": "quran-reels/config/current.json", "Size": 1500},
            {"Key": "quran-reels/config/secrets/meta-credentials.enc", "Size": 300},
            {"Key": "quran-reels/coverage/current.json", "Size": 2000},
            {"Key": "quran-reels/coverage/cycles/0001.json", "Size": 5000},
            {"Key": "quran-reels/runs/test/r1.json", "Size": 1000},
            {"Key": "quran-reels/runs/scheduled/r2.json", "Size": 1000},
            {"Key": "quran-reels/scheduler/claims/2026-08-28_morning.json", "Size": 400},
            {"Key": "quran-reels/background_pool/v1/verified/pexels/1/2/3.json", "Size": 500},
            {"Key": "quran-reels/background_pool/v1/builder_runs/run1.json", "Size": 600},
            {"Key": "quran-reels/background_pool/v2/verified/pexels/10/20/30.json", "Size": 700},
            {"Key": "quran-reels/background_pool/v2/query_metrics/qm1.json", "Size": 800},
            {"Key": "quran-reels/background_pool/v2/usage/u1.json", "Size": 300},
            {"Key": "quran-reels/background_pool/v2/stats/latest.json", "Size": 400},
            {"Key": "quran-reels/archive/plan_2026_07.json", "Size": 900},
            {"Key": "quran-reels/media/test/video.mp4", "Size": 5000000},
            {"Key": "quran-reels/mystery_blob.bin", "Size": 12345},
        ]

    def test_classify_b2_keys(self) -> None:
        self.assertEqual(classify_b2_key("config/current.json"), ("app_config", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("config/secrets/meta-credentials.enc"), ("meta_encrypted_envelope", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("coverage/current.json"), ("coverage", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("coverage/cycles/0001.json"), ("coverage_cycles", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("runs/test/r1.json"), ("run_records", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("scheduler/claims/s1.json"), ("scheduler_claims", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("background_pool/v1/verified/1.json"), ("background_v1", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("background_pool/v2/verified/2.json"), ("background_v2", "MIGRATE_TO_SUPABASE"))
        self.assertEqual(classify_b2_key("background_pool/v2/stats/latest.json"), ("background_v2", "KEEP_AS_B2_MEDIA"))
        self.assertEqual(classify_b2_key("media/test/r1.mp4"), ("media", "KEEP_AS_B2_MEDIA"))
        self.assertEqual(classify_b2_key("reels/2026/08/r1.mp4"), ("media", "KEEP_AS_B2_MEDIA"))
        self.assertEqual(classify_b2_key("unknown_payload.dat"), ("unknown_objects", "UNKNOWN_REQUIRES_REVIEW"))

    def test_inventory_and_dry_run_report_structure(self) -> None:
        client = MockClient(self.sample_contents)
        categorized = inventory_b2_objects(client, "bucket", "quran-reels/")
        report = generate_dry_run_report(categorized, schema_readiness={"app_config": True, "coverage_state": True})

        self.assertEqual(report["mode"], "DRY_RUN")
        self.assertEqual(report["writes_performed"], 0)
        self.assertEqual(report["deletions_performed"], 0)
        self.assertEqual(report["final_reel_media_count"], 1)
        self.assertEqual(report["final_reel_approximate_bytes"], 5000000)
        self.assertEqual(report["categories"]["unknown_objects"]["unsupported"], 1)
        self.assertEqual(report["categories"]["app_config"]["would_insert"], 1)
        self.assertEqual(report["categories"]["meta_encrypted_envelope"]["would_insert"], 1)
        self.assertEqual(report["categories"]["coverage"]["would_insert"], 1)
        self.assertEqual(report["categories"]["coverage_cycles"]["would_insert"], 1)
        self.assertEqual(report["categories"]["run_records"]["would_insert"], 2)

    def test_dry_run_is_idempotent(self) -> None:
        client = MockClient(self.sample_contents)
        cat1 = inventory_b2_objects(client, "bucket", "quran-reels/")
        report1 = generate_dry_run_report(cat1)

        cat2 = inventory_b2_objects(client, "bucket", "quran-reels/")
        report2 = generate_dry_run_report(cat2)

        self.assertEqual(report1, report2)

    def test_b2_access_denied_or_cap_exceeded_fails_closed(self) -> None:
        client = MockClient(error=Exception("An error occurred (AccessDenied) when calling the ListObjectsV2 operation: Cannot download file, download bandwidth or transaction (Class B) cap exceeded."))
        with self.assertRaises(StorageError) as raised:
            inventory_b2_objects(client, "bucket", "quran-reels/")
        self.assertIn("B2_INVENTORY_BLOCKED", str(raised.exception))

    def test_legacy_inventory_helper(self) -> None:
        client = MockClient([
            {"Key": "quran-reels/config/current.json", "Size": 100},
            {"Key": "quran-reels/coverage/current.json", "Size": 100},
            {"Key": "quran-reels/runs/test/a.json", "Size": 100},
            {"Key": "quran-reels/background_pool/v2/verified/pexels/a/b/c.json", "Size": 100},
            {"Key": "quran-reels/media/test/a.mp4", "Size": 1000},
        ])
        inventory = inventory_state_keys(client, "bucket", "quran-reels/")
        self.assertEqual(inventory, {"background_pool": 1, "config": 1, "coverage": 1, "media": 1, "runs": 1})
        report = migration_report(inventory)
        self.assertEqual(report["mode"], "DRY_RUN")
        self.assertEqual(report["state_object_count"], 4)

    def test_read_b2_json_object_cap_exceeded_fails_closed(self) -> None:
        class FailingB2Client:
            def get_object(self, **_kwargs: Any) -> Any:
                raise Exception("An error occurred (AccessDenied) when calling the GetObject operation: Cannot download file, download bandwidth or transaction (Class B) cap exceeded.")

        from scripts.migrate_b2_state_to_supabase import read_b2_json_object

        with self.assertRaises(StorageError) as raised:
            read_b2_json_object(FailingB2Client(), "bucket", "key.json")
        self.assertIn("B2_OBJECT_READ_BLOCKED", str(raised.exception))

    def test_execute_live_state_migration_mocked(self) -> None:
        import io
        import json
        from engine.supabase_state import SupabaseConfig, SupabaseStateStore
        from scripts.migrate_b2_state_to_supabase import execute_live_state_migration

        class MockB2Client:
            def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
                if "config" in Key:
                    payload = {"schema_version": 1, "reciter_id": "ar.alafasy"}
                elif "runs" in Key:
                    payload = {"run_id": "r1", "run_kind": "test", "date": "2026-08-28", "status": "SUCCESS"}
                elif "verified" in Key:
                    payload = {"provider": "pexels", "provider_asset_id": "1", "provider_variant_id": "2", "policy_fingerprint": "fp", "content_sha256": "h"}
                elif "builder_runs" in Key:
                    payload = {"builder_run_id": "b1", "status": "SUCCESS"}
                elif "query_metrics" in Key:
                    payload = {"builder_run_id": "b1", "provider": "pexels", "query": "nature"}
                elif "rejected" in Key or "safety_rejected" in Key:
                    payload = {"provider": "pexels", "asset_id": "10", "variant_id": "20"}
                elif "coverage" in Key:
                    payload = {"schema_version": 1, "cycle_number": 1, "total_ayahs": 6236, "covered_count": 0}
                elif "meta" in Key:
                    payload = "encrypted_envelope_data"
                else:
                    payload = {"data": "ok"}
                return {"Body": io.BytesIO(json.dumps(payload).encode("utf-8"))}

        class MockSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, Any]] = []

            def request(self, method: str, url: str, **kwargs: Any) -> Any:
                self.calls.append((method, url, kwargs))
                class Resp:
                    ok = True
                    status_code = 200
                    content = b"[]"
                    def json(self) -> Any:
                        return []
                return Resp()

        session = MockSession()
        sb_store = SupabaseStateStore(SupabaseConfig("https://example.supabase.co", "secret"), session=session)

        categorized = {
            "app_config": [{"key": "quran-reels/config/current.json", "relative": "config/current.json", "disposition": "MIGRATE_TO_SUPABASE"}],
            "run_records": [{"key": "quran-reels/runs/test/r1.json", "relative": "runs/test/r1.json", "disposition": "MIGRATE_TO_SUPABASE"}],
            "background_v2": [
                {"key": "quran-reels/background_pool/v2/verified/1.json", "relative": "background_pool/v2/verified/1.json", "disposition": "MIGRATE_TO_SUPABASE"},
                {"key": "quran-reels/background_pool/v2/safety_rejected/2.json", "relative": "background_pool/v2/safety_rejected/2.json", "disposition": "MIGRATE_TO_SUPABASE"},
            ],
            "builder_runs": [{"key": "quran-reels/background_pool/v2/builder_runs/b1.json", "relative": "background_pool/v2/builder_runs/b1.json", "disposition": "MIGRATE_TO_SUPABASE"}],
            "query_metrics": [{"key": "quran-reels/background_pool/v2/query_metrics/qm1.json", "relative": "background_pool/v2/query_metrics/qm1.json", "disposition": "MIGRATE_TO_SUPABASE"}],
            "coverage": [{"key": "quran-reels/coverage/current.json", "relative": "coverage/current.json", "disposition": "MIGRATE_TO_SUPABASE"}],
            "meta_encrypted_envelope": [{"key": "quran-reels/config/secrets/meta.enc", "relative": "config/secrets/meta.enc", "disposition": "MIGRATE_TO_SUPABASE"}],
        }

        res = execute_live_state_migration(MockB2Client(), "bucket", "quran-reels/", sb_store, categorized)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["mode"], "LIVE_COPY")
        self.assertEqual(res["total_inserts"], 8)
        self.assertEqual(res["b2_deletions"], 0)
