from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from engine.background_pool import (
    ANIMAL_POLICY,
    BACKGROUND_POOL_SCHEMA_VERSION,
    CURRENT_SAFETY_POLICY_FINGERPRINT,
    BackgroundPoolError,
    BackgroundPoolRepository,
    BackgroundUsageEvent,
    MetadataIneligibleRecord,
    SafetyRejectedRecord,
    SafetyEvidence,
    StaleBackgroundRecord,
    TransientFailureRecord,
    build_verified_record,
    compute_safety_policy_fingerprint,
    compute_eligibility_fingerprint,
    normalized_safety_policy,
    select_verified_pool_records,
    background_reuse_warnings,
    verified_record_from_dict,
    get_background_pool_repository,
    SupabaseBackgroundPoolRepository,
    ProviderQueryMetrics,
)
from engine.backgrounds import BackgroundCandidate
from engine.storage import B2Config, StorageError
from engine.supabase_state import SupabaseConfig, SupabaseStateStore


def candidate(asset_id: int = 1, file_id: int = 10, duration: float = 20.0) -> BackgroundCandidate:
    return BackgroundCandidate(
        pexels_video_id=asset_id,
        query="ocean waves",
        pexels_page_url=f"https://www.pexels.com/video/{asset_id}",
        videographer_name="Author",
        videographer_url="https://www.pexels.com/@author",
        duration_seconds=duration,
        source_width=1080,
        source_height=1920,
        video_file_id=file_id,
        file_url="https://cdn.example/video.mp4",
        file_type="video/mp4",
        fps=30.0,
        file_width=1080,
        file_height=1920,
        requires_center_crop=False,
        declared_file_size_bytes=1024,
    )


def pixabay_candidate(asset_id: int = 1, duration: float = 20.0) -> BackgroundCandidate:
    return replace(
        candidate(asset_id, 10, duration),
        provider="pixabay",
        provider_variant_name="medium:1080x1920",
        pexels_page_url=f"https://pixabay.com/videos/id-{asset_id}/",
    )


def verified(asset_id: int = 1, file_id: int = 10, duration: float = 20.0):
    return build_verified_record(
        candidate(asset_id, file_id, duration),
        content_sha256=(f"{asset_id:064x}"[-64:]),
        downloaded_file_size=1024,
        scanned_frames=81,
        animal_detector="opencv-zoo-yolox-2022nov",
        animal_classes=("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
        animal_threshold=0.35,
        animal_verdict="CLEAR",
        verified_at="2026-08-26T10:00:00+00:00",
    )


class TestFreshAssetSelection(unittest.TestCase):
    @staticmethod
    def event(record, day=20):
        return BackgroundUsageEvent(
            f"run-{day}", record.provider, record.provider_asset_id,
            record.provider_variant_id, record.record_id,
            f"2026-08-{day:02}T00:00:00+00:00", 15.0,
        )

    def select(self, records, events=(), **kwargs):
        return select_verified_pool_records(
            records, events, required_duration_seconds=15, seed=7,
            render_config_fingerprint="render-test", **kwargs,
        )

    @staticmethod
    def risk(record, rate):
        return replace(record, render_robustness={
            "fixture_config_fingerprint": "render-test", "failed_evaluation_rate": rate,
        })

    def test_verified_rotation_combines_saved_usage_and_older_render_history(self):
        from engine.storage import historical_background_usage

        pexels = verified(5)
        pixabay = replace(pexels, provider="pixabay", provider_variant_id="large")
        fresh = verified(9)
        history = historical_background_usage({
            "completed_at": "2026-08-16T10:00:00Z",
            "verified_backgrounds": [{"provider": "pixabay", "provider_asset_id": "5"}],
            "pexels_video_ids": [5],
        })
        uses = (*history, self.event(pexels, day=19))
        clips = (pexels, pixabay, fresh)
        self.assertEqual(self.select(clips, uses), (fresh, pixabay, pexels))

        # Once all clips have been used, a new use must update the rotation.
        uses += (self.event(fresh, day=20), self.event(pixabay, day=21))
        self.assertEqual(self.select(clips, uses), (pexels, fresh, pixabay))

    def test_lru_compares_instants_across_timezone_offsets(self):
        first, second = verified(1), verified(2)
        older = replace(self.event(first), used_at="2026-08-20T10:00:00+02:00")
        newer = replace(self.event(second), used_at="2026-08-20T09:00:00+00:00")
        self.assertEqual(self.select((second, first), (older, newer)), (first, second))

    def test_no_warning_for_used_prefetch_when_fresh_selected(self):
        fresh, used = verified(1), verified(2)
        self.assertEqual(background_reuse_warnings((fresh,), (fresh, used), (self.event(used),)), ())

    def test_multi_clip_warning_counts_distinct_actual_reused_assets(self):
        first, second, used = verified(1), verified(2), verified(3)
        alternate = replace(used, provider_variant_id="alternate")
        events = (self.event(used),)
        selected = self.select((used, first, second), events)
        self.assertEqual({r.provider_asset_id for r in selected[:2]}, {"1", "2"})
        warnings = background_reuse_warnings(selected, (first, second, used, alternate), events)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0], {
            "event": "BACKGROUND_REUSE_FALLBACK", "provider": "pexels",
            "provider_asset_id": "3", "provider_variant_id": "10", "last_used_at": events[0].used_at,
            "required_background_count": 3, "unused_selectable_count": 2,
            "selectable_count": 3, "reuse_count_for_this_render": 1,
            "reason": "UNUSED_POOL_EXHAUSTED",
        })

    def test_reverified_variant_warns_and_unknown_recency_is_explicit(self):
        record = verified(1)
        alternate = replace(record, provider_variant_id="alternate", content_sha256="f" * 64)
        warning = background_reuse_warnings((alternate,), (alternate,), (),
            recent_provider_identities=(("pexels", "1"),))[0]
        self.assertIsNone(warning["last_used_at"])
        self.assertEqual(warning["unused_selectable_count"], 0)

    def test_unused_medium_risk_precedes_used_robust(self):
        fresh = self.risk(verified(1), 0.0586)
        used = self.risk(verified(2), 0.0)
        self.assertEqual(self.select((used, fresh), (self.event(used),)), (fresh, used))

    def test_each_eligible_asset_is_consumed_before_reuse(self):
        records = (verified(1), verified(2), verified(3))
        events = []
        selected = []
        for day in range(20, 24):
            chosen = self.select(records, events)[0]
            selected.append(chosen.provider_asset_id)
            events.append(self.event(chosen, day))
        self.assertEqual(len(set(selected[:3])), 3)
        self.assertEqual(selected[3], selected[0])

    def test_high_risk_unused_stays_excluded(self):
        unsafe = self.risk(verified(1), 0.914)
        used = verified(2)
        self.assertEqual(self.select((unsafe, used), (self.event(used),)), (used,))

    def test_duration_and_dimensions_filter_before_fallback(self):
        used = verified(3)
        short = verified(1, duration=14.9)
        with self.assertRaises(BackgroundPoolError):
            replace(verified(2), width=720)
        self.assertEqual(self.select((short, used), (self.event(used),)), (used,))
        sufficient = verified(4, duration=15)
        self.assertEqual(self.select((used, sufficient), (self.event(used),)), (sufficient, used))

    def test_lru_outranks_risk_and_recent_history_when_dates_are_known(self):
        older = self.risk(verified(1), 0.0586)
        newer = self.risk(verified(2), 0.0)
        self.assertEqual(self.select(
            (newer, older), (self.event(older, 20), self.event(newer, 21)),
            recent_asset_ids=(1,),
        ), (older, newer))

    def test_variants_and_reverified_bytes_share_latest_asset_usage(self):
        old_variant = verified(1, file_id=10)
        alternate = verified(1, file_id=11)
        reverified = replace(old_variant, content_sha256="f" * 64, verified_at="2026-08-27T10:00:00+00:00")
        fresh = verified(2)
        older_used = verified(3)
        ordered = self.select(
            (old_variant, alternate, reverified, fresh, older_used),
            (self.event(old_variant, 20), self.event(alternate, 23), self.event(older_used, 21)),
        )
        self.assertEqual([r.provider_asset_id for r in ordered], ["2", "3", "1", "1"])
        self.assertNotIn(old_variant, ordered)

    def test_recent_history_alone_is_used_and_provider_identity_is_scoped(self):
        pexels = self.risk(verified(1), 0.0)
        pixabay = replace(verified(1), provider="pixabay")
        self.assertEqual(self.select((pexels, pixabay), recent_asset_ids=(1,)), (pixabay, pexels))
        self.assertEqual(self.select((pexels, pixabay), (self.event(pexels),)), (pixabay, pexels))

    def test_stale_inactive_and_old_policy_remain_excluded(self):
        current = verified(1)
        old_policy = dict(current.safety_policy)
        old_policy["preprocessing_version"] = "old-v0"
        old = replace(verified(2), safety_policy=old_policy, safety=replace(
            current.safety, policy_fingerprint=compute_safety_policy_fingerprint(old_policy),
        ))
        with self.assertRaises(BackgroundPoolError):
            replace(verified(3), status="stale")
        inactive = replace(verified(4), active=False)
        self.assertEqual(self.select((old, inactive, current)), (current,))

    def test_legacy_person_padding_policy_cannot_reuse_source_verification(self):
        current = verified(1)
        legacy_policy = dict(current.safety_policy)
        legacy_policy["preprocessing_version"] = "native-frame-sequential-sampling-v2"
        legacy = replace(current, safety_policy=legacy_policy, safety=replace(
            current.safety,
            policy_fingerprint=compute_safety_policy_fingerprint(legacy_policy),
        ))
        self.assertEqual(self.select((legacy,)), ())
        self.assertFalse(legacy.is_current_policy)

    def test_seed_is_deterministic_and_runtime_rejection_allows_used_fallback(self):
        records = tuple(verified(i) for i in range(1, 6))
        self.assertEqual(self.select(records), self.select(records))
        fresh, used = records[:2]
        self.assertEqual(self.select(
            (fresh, used), (self.event(used),), excluded_provider_identities=(("pexels", "1"),),
        ), (used, fresh))


class MemoryPaginator:
    def __init__(self, objects): self.objects = objects
    def paginate(self, **kwargs):
        prefix = kwargs["Prefix"]
        return [{"Contents": [{"Key": key} for key in self.objects if key.startswith(prefix)]}]


class MemoryClient:
    def __init__(self): self.objects: dict[str, bytes] = {}
    def put_object(self, **kwargs):
        body = kwargs["Body"]
        self.objects[kwargs["Key"]] = body.read() if hasattr(body, "read") else bytes(body)
    def get_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            err = RuntimeError("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}
    def get_paginator(self, _name): return MemoryPaginator(self.objects)


class TestBackgroundPoolModel(unittest.TestCase):
    def test_provider_qualified_identities_cannot_collide(self):
        pexels = verified(1)
        pixabay = build_verified_record(
            pixabay_candidate(1),
            content_sha256="f" * 64,
            downloaded_file_size=1024,
            scanned_frames=81,
            animal_detector="opencv-zoo-yolox-2022nov",
            animal_classes=("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
            animal_threshold=0.35,
            animal_verdict="CLEAR",
        )
        self.assertNotEqual(pexels.record_id, pixabay.record_id)
        ordered = select_verified_pool_records(
            (pexels, pixabay), (), required_duration_seconds=15, seed=2,
        )
        self.assertEqual({record.provider for record in ordered}, {"pexels", "pixabay"})

    def test_verified_schema_and_policy_document_validate(self):
        record = verified()
        parsed = verified_record_from_dict(asdict(record))
        self.assertEqual(parsed.record_id, record.record_id)
        self.assertTrue(parsed.is_current_policy)
        self.assertEqual(parsed.safety.animal_detector, "opencv-zoo-yolox-2022nov")
        self.assertEqual(parsed.safety.animal_verdict, "CLEAR")

    def test_unknown_schema_and_malformed_hash_rejected(self):
        data = asdict(verified())
        data["schema_version"] = 99
        with self.assertRaises(BackgroundPoolError): verified_record_from_dict(data)
        data = asdict(verified())
        data["active"] = "false"
        with self.assertRaises(BackgroundPoolError): verified_record_from_dict(data)
        data = asdict(verified())
        data["content_sha256"] = "bad"
        with self.assertRaises(BackgroundPoolError): verified_record_from_dict(data)

    def test_embedded_policy_must_match_fingerprint(self):
        data = asdict(verified())
        data["safety_policy"]["sampling_fps"] = 3.0
        with self.assertRaises(BackgroundPoolError): verified_record_from_dict(data)
        data = asdict(verified())
        data["safety_policy"]["animal_policy"]["threshold"] = 0.36
        with self.assertRaises(BackgroundPoolError): verified_record_from_dict(data)

    def test_old_policy_is_valid_but_not_current(self):
        old_policy = normalized_safety_policy(
            animal_detector="opencv-zoo-yolox-2022nov",
            animal_classes=("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
            animal_threshold=0.35,
        )
        old_policy["preprocessing_version"] = "old-v0"
        fingerprint = compute_safety_policy_fingerprint(old_policy)
        record = verified_record_from_dict({
            **asdict(verified()),
            "safety_policy": old_policy,
            "safety": {**asdict(verified().safety), "policy_fingerprint": fingerprint},
        })
        self.assertFalse(record.is_current_policy)

    def test_rejected_is_not_verified_and_transient_is_retryable(self):
        rejected = SafetyRejectedRecord("pexels", "1", "2", CURRENT_SAFETY_POLICY_FINGERPRINT, "REJECT_PERSON", "now")
        transient = TransientFailureRecord("pexels", "1", "2", "timeout", "now")
        self.assertEqual(rejected.status, "rejected")
        self.assertTrue(transient.retryable)

    def test_unused_then_lru_selection(self):
        first, second, third = verified(1), verified(2), verified(3)
        events = (
            BackgroundUsageEvent("run-a", "pexels", "1", "10", first.record_id, "2026-08-20T00:00:00+00:00", 15.0),
            BackgroundUsageEvent("run-b", "pexels", "2", "10", second.record_id, "2026-08-21T00:00:00+00:00", 15.0),
        )
        ordered = select_verified_pool_records((first, second, third), events, required_duration_seconds=15, seed=7)
        self.assertEqual(ordered[0].provider_asset_id, "3")
        self.assertEqual([item.provider_asset_id for item in ordered[1:]], ["1", "2"])

    def test_json_only_repository_paths_and_dedup(self):
        client = MemoryClient()
        config = B2Config("https://b2.example", "eu-central-003", "id", "secret", "bucket")
        repo = BackgroundPoolRepository(client, config)
        record = verified()
        key1 = repo.save_verified(record)
        key2 = repo.save_verified(record)
        self.assertEqual(key1, key2)
        self.assertEqual(len(client.objects), 1)
        self.assertTrue(key1.endswith(".json"))
        self.assertNotIn(".mp4", key1)
        with self.assertRaises(StorageError):
            repo._put_json(f"{config.prefix}background_pool/v2/bad.mp4", {"x": 1})

    def test_stats_exclude_stale_records_from_current_usable_count(self):
        client = MemoryClient()
        config = B2Config("https://b2.example", "eu-central-003", "id", "secret", "bucket")
        repo = BackgroundPoolRepository(client, config)
        record = verified()
        repo.save_verified(record)
        repo.save_stale(StaleBackgroundRecord(
            provider=record.provider,
            provider_asset_id=record.provider_asset_id,
            provider_variant_id=record.provider_variant_id,
            verified_record_id=record.record_id,
            reason="SOURCE_CHANGED",
            detected_at="2026-08-27T00:00:00+00:00",
            expected_sha256=record.content_sha256,
        ))
        stats = repo.compute_stats()
        self.assertEqual(stats["verified_current_policy"], 0)
        self.assertEqual(stats["production_unused"], 0)
        self.assertEqual(stats["missing_or_stale"], 1)

    def test_legacy_v1_records_are_preserved_and_counted_as_stale_policy(self):
        client = MemoryClient()
        config = B2Config("https://b2.example", "eu-central-003", "id", "secret", "bucket")
        legacy_key = f"{config.prefix}background_pool/v1/verified/pexels/123/456.json"
        client.objects[legacy_key] = b"{}"
        stats = BackgroundPoolRepository(client, config).compute_stats()
        self.assertEqual(stats["legacy_v1_stale"], 1)
        self.assertEqual(stats["verified_stale_policy"], 1)
        self.assertEqual(stats["verified_current_policy"], 0)

    def test_reconciliation_instruments_bounded_b2_operation_types(self):
        client = MemoryClient()
        config = B2Config("https://b2.example", "eu-central-003", "id", "secret", "bucket")
        repo = BackgroundPoolRepository(client, config)
        eligibility = compute_eligibility_fingerprint({"fixture": "v1"})
        record = verified()
        repo.save_verified(record)
        repo.save_metadata_ineligible(MetadataIneligibleRecord(
            "pexels", "99", "10", eligibility, "INSUFFICIENT_DURATION", "now",
        ))

        snapshot = repo.reconcile_pool_state(eligibility)

        self.assertEqual(snapshot.stats["verified_current_policy"], 1)
        self.assertIn(("pexels", "99", "10"), snapshot.metadata_ineligible_identities)
        operations = repo.operation_counts()
        self.assertGreaterEqual(operations["list_operations"], 8)
        self.assertEqual(operations["metadata_reads"], 2)
        self.assertEqual(operations["writes"], 2)

    def test_get_background_pool_repository_respects_state_authority(self):
        env_b2 = {
            "STATE_AUTHORITY": "b2",
            "B2_ENDPOINT": "https://b2.example",
            "B2_REGION": "eu-central-003",
            "B2_APPLICATION_KEY_ID": "id",
            "B2_APPLICATION_KEY": "secret",
            "B2_BUCKET_NAME": "bucket",
        }
        repo_b2 = get_background_pool_repository(env_b2)
        self.assertIsInstance(repo_b2, BackgroundPoolRepository)

        env_supabase = {
            "STATE_AUTHORITY": "supabase",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "secret-key",
        }
        repo_sb = get_background_pool_repository(env_supabase)
        self.assertIsInstance(repo_sb, SupabaseBackgroundPoolRepository)
        ops = repo_sb.operation_counts()
        self.assertEqual(ops, {"list_operations": 0, "metadata_reads": 0, "writes": 0})

    def test_runtime_pool_reads_verified_and_stale_evidence_beyond_first_page(self):
        store = SupabaseStateStore(SupabaseConfig("https://example.invalid", "fixture"))
        repo = SupabaseBackgroundPoolRepository(store)
        final = verified(501)
        asset_order = "provider.asc,provider_asset_id.asc,provider_variant_id.asc,policy_fingerprint.asc,content_sha256.asc"
        stale_asset = {"provider": "pexels", "provider_asset_id": "501", "provider_variant_id": "10",
                       "content_sha256": final.content_sha256,
                       "policy_fingerprint": final.safety.policy_fingerprint}
        requests = []

        def read(method, table, *, params):
            self.assertEqual(method, "GET")
            offset = int(params.get("offset", 0))
            requests.append((table, offset, params.get("order")))
            if table == "background_events":
                self.assertEqual(params["event_type"], "eq.stale")
                rows = [{"value": {}}, {"value": {}}, {"value": {"verified_record_id": "stale-by-event"}}]
            elif params.get("status") == "eq.stale":
                rows = [dict(stale_asset, provider_asset_id="1"),
                        dict(stale_asset, provider_asset_id="2"), stale_asset]
            else:
                self.assertNotIn("status", params)  # Builder reconciliation needs all statuses.
                rows = [{"value": {}}, {"value": {}}, {"value": asdict(final)}]
            return rows[offset:offset + 2]  # Simulate a server cap below the requested limit.

        with patch.object(store, "_request", side_effect=read):
            verified_records = repo.list_verified()
            stale_ids = repo.stale_record_ids()
        self.assertEqual(verified_records, (final,))
        self.assertIn(final.record_id, stale_ids)
        self.assertIn("stale-by-event", stale_ids)
        self.assertEqual(requests, [
            (table, offset, order)
            for table, order in (("background_assets", asset_order), ("background_assets", asset_order),
                                 ("background_events", "event_id.asc"))
            for offset in (0, 2, 3)
        ])

    def test_supabase_background_pool_repository_crud_and_reconciliation(self):
        class MockResponse:
            def __init__(self, payload, ok=True, status_code=200):
                self._payload = payload
                self.ok = ok
                self.status_code = status_code
                self.content = json.dumps(payload).encode() if payload is not None else b""

            def json(self):
                return self._payload

        class MockSession:
            def __init__(self):
                self.calls = []
                self.responses = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return self.responses.pop(0)

        session = MockSession()
        store = SupabaseStateStore(
            SupabaseConfig("https://example.supabase.co", "test-key"),
            session=session,
        )
        repo = SupabaseBackgroundPoolRepository(store)

        # 1. save_verified -> POST to background_assets
        rec = verified()
        session.responses.append(MockResponse([]))  # POST background_assets
        res = repo.save_verified(rec)
        self.assertEqual(res, rec.record_id)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertIn("background_assets", session.calls[0][1])

        # 2. save_safety_rejected -> POST to background_events
        session.responses.append(MockResponse([]))
        rej_rec = SafetyRejectedRecord(
            provider="pexels",
            provider_asset_id="123",
            provider_variant_id="456",
            policy_fingerprint=CURRENT_SAFETY_POLICY_FINGERPRINT,
            reason="PERSON_DETECTED",
            rejected_at="2026-08-28T00:00:00Z",
        )
        repo.save_safety_rejected(rej_rec)
        self.assertIn("background_events", session.calls[-1][1])

        # 3. reconcile_pool_state
        # list_verified -> GET background_assets
        session.responses.extend((MockResponse([{"value": asdict(rec)}]), MockResponse([])))
        # stale_record_ids -> GET background_assets (status=stale) + GET background_events (event_type=stale)
        session.responses.append(MockResponse([]))
        session.responses.append(MockResponse([]))
        # current_safety_rejected_identities -> GET background_events
        session.responses.append(MockResponse([{"provider": "pexels", "provider_asset_id": "123", "provider_variant_id": "456"}]))
        # list_usage -> GET background_events
        session.responses.append(MockResponse([]))
        # metadata_ineligible_count -> GET background_events
        session.responses.append(MockResponse([]))
        # transient_count -> GET background_events
        session.responses.append(MockResponse([]))
        # query_metrics_rollup -> GET background_query_metrics
        session.responses.append(MockResponse([]))

        reconciliation = repo.reconcile_pool_state()
        self.assertEqual(reconciliation.stats["verified_current_policy"], 1)
        self.assertEqual(reconciliation.stats["known_safety_rejected"], 1)
        self.assertEqual(repo.operation_counts()["writes"], 0)
        self.assertEqual(repo.operation_counts()["list_operations"], 0)

    def test_supabase_mode_builder_performs_zero_b2_state_writes(self) -> None:
        """Regression test: all builder persistence operations perform 0 B2 writes in Supabase mode."""
        class MockResponse:
            def __init__(self, payload=None, status_code=200):
                self.status_code = status_code
                self.ok = 200 <= status_code < 300
                self._payload = payload if payload is not None else []
                self.content = json.dumps(payload).encode() if payload is not None else b""

            def json(self):
                return self._payload

        class MockSession:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return MockResponse([])

        class FailingB2Client:
            def __getattr__(self, name):
                def _fail(*args, **kwargs):
                    raise AssertionError(f"B2 client method '{name}' called in Supabase mode!")
                return _fail

        session = MockSession()
        store = SupabaseStateStore(
            SupabaseConfig("https://example.supabase.co", "test-key"),
            session=session,
        )
        repo = SupabaseBackgroundPoolRepository(store)

        # 1. save_verified
        rec = verified()
        repo.save_verified(rec)

        # 2. save_safety_rejected
        repo.save_safety_rejected(
            SafetyRejectedRecord("pexels", "101", "1010", CURRENT_SAFETY_POLICY_FINGERPRINT, "ANIMAL", "2026-08-29T00:00:00Z")
        )

        # 3. save_metadata_ineligible
        repo.save_metadata_ineligible(
            MetadataIneligibleRecord("pexels", "102", "1020", "a" * 64, "DURATION_SHORT", "2026-08-29T00:00:00Z")
        )

        # 4. save_transient
        repo.save_transient(
            TransientFailureRecord("pexels", "103", "1030", "TIMEOUT", "2026-08-29T00:00:00Z")
        )

        # 5. save_builder_run
        repo.save_builder_run("pool_test_run", {"state": "completed", "found_safe": 1})

        # 6. save_query_metrics
        repo.save_query_metrics(
            ProviderQueryMetrics(builder_run_id="pool_test", provider="pexels", query="sky", api_requests=1, pages_attempted=1, candidates_returned=15)
        )

        # 7. save_usage
        repo.save_usage(
            BackgroundUsageEvent("run_123", "pexels", "101", "1010", "rec_101", "2026-08-29T00:00:00Z", 20.0)
        )

        # 8. save_stats
        repo.save_stats({"schema_version": 2})

        # Assert zero B2 operations recorded in metrics
        op_counts = repo.operation_counts()
        self.assertEqual(op_counts["writes"], 0)
        self.assertEqual(op_counts["list_operations"], 0)
        self.assertEqual(op_counts["metadata_reads"], 0)

        # Assert all HTTP calls went strictly to Supabase PostgREST endpoints
        for method, url, kwargs in session.calls:
            self.assertTrue(url.startswith("https://example.supabase.co/rest/v1/"))

        # Factory resolution with failing B2 client does not touch B2 in supabase mode
        env = {
            "STATE_AUTHORITY": "supabase",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test-key",
        }
        resolved_repo = get_background_pool_repository(
            env=env,
            b2_client=FailingB2Client(),
            supabase_store=store,
        )
        self.assertIsInstance(resolved_repo, SupabaseBackgroundPoolRepository)


if __name__ == "__main__":
    unittest.main()
