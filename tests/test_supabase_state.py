"""Supabase state adapter tests use a fake HTTP session only."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from engine.storage import RunRecord, StoredConfig
from engine.supabase_state import (
    LIVE_TEST_WRITE_OPT_IN_ENV,
    REQUIRED_SUPABASE_TABLES,
    SupabaseConfig,
    SupabaseStateError,
    SupabaseStateStore,
)


class Response:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        return self._payload


class Session:
    def __init__(self):
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class SupabaseStateTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.key = "server-secret-fixture"
        self.store = SupabaseStateStore(SupabaseConfig("https://project.supabase.co", self.key), self.session)

    def test_config_is_server_only_and_upserts_json(self):
        self.session.responses.append(Response([]))
        self.store.save_config(StoredConfig())
        method, _, kwargs = self.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertNotIn(self.key.encode("utf-8"), kwargs["data"])
        self.assertEqual(kwargs["headers"]["apikey"], self.key)
        self.assertEqual(kwargs["headers"]["Prefer"], "resolution=merge-duplicates,return=minimal")

    def test_loads_and_validates_run_record(self):
        record = RunRecord(run_id="r1", date="2026-08-28", slot="test", status="SUCCESS", started_at="2026-08-28T00:00:00Z", run_kind="test")
        self.session.responses.append(Response([{"value": {**record.__dict__, "translation_languages": ["en"], "pexels_video_ids": [], "translation_sources": {}, "provenance": {}}}]))
        self.assertEqual(self.store.get_run("r1"), record)

    def test_http_failure_is_fail_closed_without_key_echo(self):
        self.session.responses.append(Response({"message": "no"}, ok=False, status_code=503))
        with self.assertRaises(SupabaseStateError) as raised:
            self.store.load_config()
        self.assertNotIn(self.key, str(raised.exception))

    def test_reads_production_authorization_passages_only_through_the_server_rpc(self):
        payload = [{
            "policy_version": "calm-inviting-v1",
            "surah_number": 2,
            "passage_first_ayah": 21,
            "passage_last_ayah": 21,
            "authorization_ids": [1],
            "approval_bases": ["SCREENING_ELIGIBLE"],
            "candidate_ayah_keys": ["2:21"],
            "authorization_fingerprint": "a" * 32,
        }]
        self.session.responses.append(Response(payload))

        self.assertEqual(
            self.store.get_production_authorized_passages("calm-inviting-v1"),
            payload,
        )
        method, url, kwargs = self.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/rest/v1/rpc/get_production_authorized_passages_v2"))
        self.assertIn(b"calm-inviting-v1", kwargs["data"])

    def test_rejects_malformed_production_authorization_rpc_result(self):
        self.session.responses.append(Response({"not": "a list"}))
        with self.assertRaisesRegex(SupabaseStateError, "malformed"):
            self.store.get_production_authorized_passages("calm-inviting-v1")

    def test_env_requires_server_role_key(self):
        with self.assertRaises(SupabaseStateError):
            SupabaseConfig.from_env({"SUPABASE_URL": "https://project.supabase.co"})

    def test_supports_modern_sb_secret_format(self):
        modern_key = "sb_secret_" + "prod_1234567890abcdef"
        cfg = SupabaseConfig("https://example.supabase.co", modern_key)
        store = SupabaseStateStore(cfg, self.session)
        self.session.responses.append(Response([]))
        store.save_config(StoredConfig())
        method, _, kwargs = self.session.calls[0]
        self.assertEqual(kwargs["headers"]["apikey"], modern_key)
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {modern_key}")

    def test_supports_legacy_jwt_service_role_format(self):
        jwt_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig"
        cfg = SupabaseConfig("https://example.supabase.co", jwt_key)
        store = SupabaseStateStore(cfg, self.session)
        self.session.responses.append(Response([]))
        store.save_config(StoredConfig())
        method, _, kwargs = self.session.calls[0]
        self.assertEqual(kwargs["headers"]["apikey"], jwt_key)
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {jwt_key}")

    def test_rejects_non_https_url(self):
        with self.assertRaises(SupabaseStateError):
            SupabaseConfig("http://insecure.supabase.co", "secret")

    def test_check_schema_readiness_passes_when_all_tables_respond_200(self):
        for _ in REQUIRED_SUPABASE_TABLES:
            self.session.responses.append(Response([], status_code=200))
        readiness = self.store.check_schema_readiness()
        self.assertEqual(len(readiness), len(REQUIRED_SUPABASE_TABLES))
        self.assertTrue(all(readiness.values()))

    def test_check_schema_readiness_fails_when_table_missing(self):
        self.session.responses.append(Response(None, ok=False, status_code=404))
        for _ in range(len(REQUIRED_SUPABASE_TABLES) - 1):
            self.session.responses.append(Response([], status_code=200))
        readiness = self.store.check_schema_readiness()
        self.assertFalse(all(readiness.values()))
        self.assertFalse(readiness["app_config"])

    def test_default_live_transport_is_blocked_during_unittests_without_opt_in(self):
        store = SupabaseStateStore(
            SupabaseConfig("https://production-looking.supabase.co", "server-key")
        )
        record = RunRecord(
            run_id="blocked-live-test",
            date="2026-08-30",
            slot="test",
            status="STARTED",
            run_kind="test",
            started_at="2026-08-30T00:00:00Z",
        )
        from tests.editorial_fixtures import legacy_authorization_catalog
        guarded_operations = (
            lambda: store.start_run_record(record),
            lambda: store.complete_run_record(record),
            lambda: store.fail_run_record(record),
            lambda: store.save_run(record),
            lambda: store.record_covered_ayahs("blocked-live-test", ("2:21",),
                                               production_authorization=legacy_authorization_catalog()),
            lambda: store.record_background_event(
                "stale", "pexels", "1", "1", "fingerprint", {"test": True}
            ),
            lambda: store.record_render_safety_failure(
                provider="pexels", provider_asset_id="1", provider_variant_id="1",
                content_sha256="", run_id="blocked-live-test", attempt_number=1,
                start_offset_seconds=0.0, verse_keys=("2:21",),
                render_config_fingerprint="fingerprint", failure_reason="synthetic",
                detector_counts={},
            ),
        )
        with patch.dict(os.environ, {LIVE_TEST_WRITE_OPT_IN_ENV: ""}, clear=False):
            for operation in guarded_operations:
                with self.assertRaisesRegex(SupabaseStateError, "Live Supabase access is blocked"):
                    operation()

    def test_background_candidate_rpc_mapping_and_sequence_handling(self):
        candidate_dict = {
            "candidate_id": "cand-1",
            "provider": "pexels",
            "provider_asset_id": "100",
            "provider_variant_id": "200",
            "status": "PROCESSING",
            "lease_token": "token-1",
        }
        completed_dict = {
            "candidate_id": "cand-1",
            "status": "VERIFIED",
        }

        # Case 1: PostgREST returns a single dict (Mapping)
        self.session.responses.append(Response(candidate_dict))
        claimed = self.store.claim_background_candidate("worker-1", 900)
        self.assertEqual(claimed["candidate_id"], "cand-1")

        self.session.responses.append(Response(completed_dict))
        completed = self.store.complete_background_candidate("cand-1", "token-1", "VERIFIED")
        self.assertEqual(completed["status"], "VERIFIED")

        # Case 2: PostgREST returns a Sequence of dicts
        self.session.responses.append(Response([candidate_dict]))
        claimed_seq = self.store.claim_background_candidate("worker-1", 900)
        self.assertEqual(claimed_seq["candidate_id"], "cand-1")

        self.session.responses.append(Response([completed_dict]))
        completed_seq = self.store.complete_background_candidate("cand-1", "token-1", "VERIFIED")
        self.assertEqual(completed_seq["status"], "VERIFIED")

        # Case 3: Empty RPC results
        self.session.responses.append(Response(None))
        self.assertIsNone(self.store.claim_background_candidate("worker-1", 900))

        self.session.responses.append(Response([]))
        self.assertIsNone(self.store.claim_background_candidate("worker-1", 900))

        self.session.responses.append(Response({}))
        self.assertIsNone(self.store.claim_background_candidate("worker-1", 900))

        self.session.responses.append(Response([]))
        with self.assertRaises(SupabaseStateError):
            self.store.complete_background_candidate("cand-1", "token-1", "VERIFIED")

        self.session.responses.append(Response(None))
        with self.assertRaises(SupabaseStateError):
            self.store.complete_background_candidate("cand-1", "token-1", "VERIFIED")

        # Case 4: Malformed RPC results (non-mapping items, strings, scalar numbers)
        self.session.responses.append(Response("malformed_string_response"))
        self.assertIsNone(self.store.claim_background_candidate("worker-1", 900))

        self.session.responses.append(Response([12345]))
        self.assertIsNone(self.store.claim_background_candidate("worker-1", 900))

        self.session.responses.append(Response("malformed_string_response"))
        with self.assertRaises(SupabaseStateError):
            self.store.complete_background_candidate("cand-1", "token-1", "VERIFIED")

        self.session.responses.append(Response([12345]))
        with self.assertRaises(SupabaseStateError):
            self.store.complete_background_candidate("cand-1", "token-1", "VERIFIED")

    def test_background_candidate_summary_paginates_empty_and_short_pages(self):
        for payload, expected_total in (([], 0), ([{"status": "PENDING", "updated_at": "2026-01-01T00:00:00Z", "discovered_at": "2026-01-01T00:00:00Z"}], 1)):
            session = Session()
            session.responses.append(Response(payload))
            store = SupabaseStateStore(SupabaseConfig("https://project.supabase.co", self.key), session)
            summary = store.background_candidate_summary()
            self.assertEqual(summary["total"], expected_total)
            self.assertEqual(len(session.calls), 1)
            params = session.calls[0][2]["params"]
            self.assertEqual(params["limit"], "500")
            self.assertEqual(params["offset"], "0")
            self.assertEqual(params["order"], "candidate_id.asc")

    def test_background_candidate_summary_paginates_exactly_500_rows(self):
        page = [{"status": "PENDING", "updated_at": f"2026-01-01T00:{i:02d}:00Z", "discovered_at": "2026-01-01T00:00:00Z"} for i in range(500)]
        session = Session()
        session.responses.extend((Response(page), Response([])))
        store = SupabaseStateStore(SupabaseConfig("https://project.supabase.co", self.key), session)
        summary = store.background_candidate_summary()
        self.assertEqual(summary["total"], 500)
        self.assertEqual(summary["pending"], 500)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1][2]["params"]["offset"], "500")

    def test_background_candidate_summary_paginates_more_than_1000_rows_and_counts_all_pages(self):
        rows = [{"status": "PENDING", "updated_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z", "discovered_at": "2026-01-01T00:00:00Z"} for i in range(1000)]
        rows.append({"status": "PROCESSING", "updated_at": "2026-02-01T00:00:00Z", "discovered_at": "2026-02-01T00:00:00Z"})
        session = Session()
        session.responses.extend((Response(rows[:500]), Response(rows[500:1000]), Response(rows[1000:])))
        store = SupabaseStateStore(SupabaseConfig("https://project.supabase.co", self.key), session)
        summary = store.background_candidate_summary()
        self.assertEqual(summary["total"], 1001)
        self.assertEqual(summary["pending"], 1000)
        self.assertEqual(summary["processing"], 1)
        self.assertEqual(summary["last_activity"], "2026-02-01T00:00:00Z")
        self.assertEqual([call[2]["params"]["offset"] for call in session.calls], ["0", "500", "1000"])

    def test_background_candidate_summary_fails_closed_on_malformed_page(self):
        self.session.responses.append(Response({"status": "PENDING"}))
        with self.assertRaisesRegex(SupabaseStateError, "malformed page"):
            self.store.background_candidate_summary()

    def test_other_background_reads_use_complete_pagination(self):
        asset = {"provider": "pixabay", "provider_asset_id": "1", "provider_variant_id": "v1", "policy_fingerprint": "f" * 64, "content_sha256": "a" * 64, "value": {}}
        session = Session()
        session.responses.extend((Response([asset] * 500), Response([asset]), Response([asset]), Response([{"event_type": "usage"}])))
        store = SupabaseStateStore(SupabaseConfig("https://project.supabase.co", self.key), session)
        assets = store.get_verified_background_assets()
        self.assertEqual(len(assets), 501)
        stats = store.get_background_pool_stats()
        self.assertEqual(stats["verified_count"], 1)
        self.assertEqual(stats["total_events"], 1)
        self.assertEqual([call[1].rsplit("/", 1)[-1] for call in session.calls], ["background_assets", "background_assets", "background_assets", "background_events"])
