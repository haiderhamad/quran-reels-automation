from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from engine.background_pool import (
    CURRENT_SAFETY_POLICY_FINGERPRINT,
    BackgroundPoolReconciliation,
)
from engine.background_pool_builder import LocalBuilderLock, PoolBuilderError, StopController, balanced_nature_queries, build_verified_pool
from engine.backgrounds import (
    ALLOWED_NATURE_QUERIES,
    BACKGROUND_SEARCH_CATEGORIES,
    PEXELS_DISCOVERY_ORIENTATION_CYCLE,
    BackgroundDiscoveryPage,
    DownloadedBackground,
    MetadataIneligibleCandidate,
)
from engine.safety import Detection, SafetyScanResult
from tests.test_engine_background_pool import candidate
from scripts.refresh_background_pool_stats import refresh_authoritative_stats


class MemoryRepo:
    def __init__(self):
        self.verified = []
        self.rejected = []
        self.metadata = []
        self.transient = []
        self.builder_runs = []
        self.reconcile_calls = 0
        self.compute_stats_calls = 0
        self.stats_writes = 0
        self.operation_count_values = {"list_operations": 0, "metadata_reads": 0, "writes": 0}
    def list_verified(self): return tuple(self.verified)
    def stale_record_ids(self): return set()
    def current_safety_rejected_identities(self):
        return {(r.provider, r.provider_asset_id, r.provider_variant_id) for r in self.rejected if r.policy_fingerprint == CURRENT_SAFETY_POLICY_FINGERPRINT}
    def current_metadata_ineligible_identities(self, _fingerprint): return set()
    def query_metrics_rollup(self): return {}
    def save_verified(self, record): self.verified.append(record); return record.record_id
    def save_safety_rejected(self, record): self.rejected.append(record); return "rejected"
    def save_metadata_ineligible(self, record): self.metadata.append(record); return "metadata"
    def save_transient(self, record): self.transient.append(record); return "transient"
    def save_query_metrics(self, _record): return "metrics"
    def save_builder_run(self, run_id, data): self.builder_runs.append((run_id, data)); return run_id
    def save_stats(self, data): self.stats_writes += 1; return "stats"
    def operation_counts(self): return dict(self.operation_count_values)
    def _stats(self):
        return {
            "verified_current_policy": len([r for r in self.verified if r.is_current_policy]),
            "verified_stale_policy": 0,
            "known_safety_rejected": len(self.rejected),
            "metadata_ineligible": len(self.metadata),
            "transient_failures": len(self.transient),
            "production_used_unique": 0,
            "production_unused": len(self.verified),
            "missing_or_stale": 0,
            "pexels_verified_current_policy": len([r for r in self.verified if r.is_current_policy and r.provider == "pexels"]),
            "pixabay_verified_current_policy": len([r for r in self.verified if r.is_current_policy and r.provider == "pixabay"]),
            "query_metrics": {},
        }
    def reconcile_pool_state(self, fingerprint=None):
        self.reconcile_calls += 1
        return BackgroundPoolReconciliation(
            stats=self._stats(),
            verified_records=tuple(self.verified),
            stale_record_ids=frozenset(),
            safety_rejected_identities=frozenset(self.current_safety_rejected_identities()),
            metadata_ineligible_identities=frozenset(
                (record.provider, record.provider_asset_id, record.provider_variant_id)
                for record in self.metadata
                if fingerprint is not None and record.eligibility_fingerprint == fingerprint
            ),
        )
    def compute_stats(self):
        self.compute_stats_calls += 1
        return self._stats()


class Provider:
    name = "pexels"
    def __init__(self, candidates): self.candidates = tuple(candidates)
    def search(self, _query, page=1): return self.candidates if page == 1 else ()


def safe_result():
    return SafetyScanResult(True, 61, 15.0, (), (), animal_policy_status="CLEAR")


def unsafe_result():
    detection = Detection(0, 0.0, "person", 0.9, (0, 0, 1, 1))
    return SafetyScanResult(False, 1, 15.0, (detection,), (), "person detected")


class TestPoolBuilder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths: list[str] = []

    def tearDown(self): self.temp.cleanup()

    def downloader(self, cand, **_kwargs):
        path = Path(self.temp.name) / f"{cand.pexels_video_id}.mp4"
        data = f"video-{cand.pexels_video_id}".encode()
        path.write_bytes(data)
        self.paths.append(str(path))
        return DownloadedBackground(cand, str(path), len(data), hashlib.sha256(data).hexdigest())

    def test_add_safe_means_new_safe_not_candidates_and_temp_cleanup(self):
        repo = MemoryRepo()
        scans = iter((unsafe_result(), safe_result(), safe_result()))
        result = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(1), candidate(2), candidate(3))),
            add_safe=2,
            max_candidates=10,
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: next(scans),
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.found_safe, 2)
        self.assertEqual(result.candidates_processed, 3)
        self.assertEqual(len(repo.rejected), 1)
        self.assertTrue(all(not os.path.exists(path) for path in self.paths))

    def test_metadata_ineligible_is_not_counted_as_safety_rejected(self):
        repo = MemoryRepo()
        class DiscoveryProvider:
            def search(self, query, page=1):
                if page > 1: return BackgroundDiscoveryPage("pexels", query, page, 0, (), ())
                return BackgroundDiscoveryPage(
                    "pexels", query, page, 42, (candidate(7),),
                    tuple(MetadataIneligibleCandidate("pexels", str(i), "metadata", query, "INSUFFICIENT_DURATION") for i in range(100, 141)),
                )
        result = build_verified_pool(
            repository=repo, provider=DiscoveryProvider(), add_safe=1,
            download_fn=self.downloader, scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(result.metadata_skipped, 41)
        self.assertEqual(result.safety_rejected, 0)
        self.assertEqual(result.safety_scanned, 1)
        self.assertEqual(result.found_safe, 1)

    def test_discovery_cap_is_never_exceeded_by_a_provider_page(self):
        repo = MemoryRepo()

        class OversizedPage:
            def search(self, query, page=1):
                if page > 1:
                    return BackgroundDiscoveryPage("pexels", query, page, 0, (), ())
                return BackgroundDiscoveryPage(
                    "pexels",
                    query,
                    page,
                    42,
                    (),
                    tuple(
                        MetadataIneligibleCandidate(
                            "pexels", str(index), "metadata", query,
                            "INSUFFICIENT_DURATION",
                        )
                        for index in range(42)
                    ),
                )

        result = build_verified_pool(
            repository=repo,
            provider=OversizedPage(),
            add_safe=1,
            max_candidates=10,
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "candidate_exhausted")
        self.assertEqual(result.discovered, 10)
        self.assertEqual(result.metadata_skipped, 10)
        self.assertEqual(len(repo.metadata), 10)

    def test_query_subset_stays_within_curated_taxonomy(self):
        repo = MemoryRepo()
        provider = Provider((candidate(1),))
        result = build_verified_pool(
            repository=repo,
            provider=provider,
            add_safe=1,
            query_subset=("moving clouds",),
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "completed")
        with self.assertRaisesRegex(PoolBuilderError, "LOCAL_POOL_INVALID_QUERY"):
            build_verified_pool(
                repository=MemoryRepo(),
                provider=provider,
                add_safe=1,
                query_subset=("not an approved query",),
                logger=lambda _line: None,
            )

    def test_both_provider_mode_isolates_one_provider_failure(self):
        repo = MemoryRepo()
        class Failing:
            def search(self, _query, page=1): raise RuntimeError("provider unavailable")
        result = build_verified_pool(
            repository=repo,
            provider={"pexels": Failing(), "pixabay": Provider((replace(candidate(9), provider="pixabay", provider_variant_name="medium:1080x1920"),))},
            provider_name="both", add_safe=1,
            download_fn=self.downloader, scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.found_safe, 1)
        self.assertGreaterEqual(result.transient_failures, 1)
        self.assertEqual(repo.verified[0].provider, "pixabay")

    def test_known_verified_and_same_policy_rejected_are_skipped(self):
        repo = MemoryRepo()
        first = candidate(1)
        downloaded = self.downloader(first)
        from engine.background_pool import build_verified_record, SafetyRejectedRecord
        repo.save_verified(build_verified_record(
            first,
            content_sha256=downloaded.content_sha256,
            downloaded_file_size=downloaded.file_size_bytes,
            scanned_frames=60,
            animal_detector="opencv-zoo-yolox-2022nov",
            animal_classes=("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
            animal_threshold=0.35,
            animal_verdict="CLEAR",
        ))
        os.remove(downloaded.local_path)
        repo.save_safety_rejected(SafetyRejectedRecord("pexels", "2", "10", CURRENT_SAFETY_POLICY_FINGERPRINT, "REJECT_PERSON", "now"))
        result = build_verified_pool(
            repository=repo,
            provider=Provider((first, candidate(2), candidate(3))),
            add_safe=1,
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(result.candidates_processed, 1)
        self.assertEqual(repo.verified[-1].provider_asset_id, "3")

    def test_transient_candidate_is_retryable_and_timeout_never_safe(self):
        repo = MemoryRepo()
        now = [0.0]
        calls = [0]
        def clock(): return now[0]
        def scan(*_args, **_kwargs):
            calls[0] += 1
            if calls[0] == 1:
                now[0] += 100.0
            return safe_result()
        result = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(1), candidate(2))),
            add_safe=1,
            per_candidate_timeout_seconds=45,
            download_fn=self.downloader,
            scan_fn=scan,
            logger=lambda _line: None,
            monotonic=clock,
        )
        self.assertTrue(repo.transient)
        self.assertNotEqual(repo.transient[0].status, "verified")
        self.assertTrue(repo.verified)
        self.assertEqual(repo.verified[-1].provider_asset_id, "2")
        self.assertIn(result.state, ("completed", "candidate_exhausted"))

    def test_same_provider_asset_is_not_rescanned_via_another_query_or_variant(self):
        repo = MemoryRepo()
        calls = 0

        class RepeatingProvider:
            def search(self, _query, page=1):
                nonlocal calls
                calls += 1
                if page > 1:
                    return ()
                if calls == 1:
                    return (candidate(1, 10),)
                return (candidate(1, 11), candidate(2, 20))

        scans = iter((unsafe_result(), safe_result()))
        result = build_verified_pool(
            repository=repo,
            provider=RepeatingProvider(),
            add_safe=1,
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: next(scans),
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual([Path(path).stem for path in self.paths], ["1", "2"])
        self.assertEqual(repo.verified[0].provider_asset_id, "2")

    def test_duplicate_content_sha_is_persisted_and_never_safety_scanned(self):
        repo = MemoryRepo()
        original = candidate(1)
        original_download = self.downloader(original)
        from engine.background_pool import build_verified_record
        repo.save_verified(build_verified_record(
            original,
            content_sha256=original_download.content_sha256,
            downloaded_file_size=original_download.file_size_bytes,
            scanned_frames=60,
            animal_detector="opencv-zoo-yolox-2022nov",
            animal_classes=("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
            animal_threshold=0.35,
            animal_verdict="CLEAR",
        ))
        os.remove(original_download.local_path)

        scans = []

        def duplicate_then_unique(cand, **_kwargs):
            path = Path(self.temp.name) / f"{cand.pexels_video_id}.mp4"
            payload = b"video-1" if cand.pexels_video_id == 2 else b"video-3"
            path.write_bytes(payload)
            self.paths.append(str(path))
            return DownloadedBackground(cand, str(path), len(payload), hashlib.sha256(payload).hexdigest())

        result = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(2), candidate(3))),
            add_safe=1,
            download_fn=duplicate_then_unique,
            scan_fn=lambda *_args, **_kwargs: scans.append(True) or safe_result(),
            logger=lambda _line: None,
        )

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.duplicate_content_sha, 1)
        self.assertEqual(result.safety_scanned, 1)
        self.assertEqual(len(scans), 1)
        self.assertEqual(repo.metadata[-1].reason, "DUPLICATE_CONTENT_SHA256")
        self.assertEqual(repo.verified[-1].provider_asset_id, "3")

    def test_stop_before_work_is_fail_safe(self):
        repo = MemoryRepo()
        stop = StopController()
        stop.request()
        result = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(1),)),
            add_safe=1,
            stop_controller=stop,
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "stopped")
        self.assertFalse(repo.verified)
        self.assertFalse(self.paths)

    def test_interruption_preserves_prior_completed_record_and_cleans_current_temp(self):
        repo = MemoryRepo()
        stop = StopController()
        calls = 0

        def scan(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                stop.request()
            return safe_result()

        result = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(1), candidate(2), candidate(3))),
            add_safe=2,
            stop_controller=stop,
            download_fn=self.downloader,
            scan_fn=scan,
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "stopped")
        self.assertEqual([item.provider_asset_id for item in repo.verified], ["1"])
        self.assertTrue(all(not os.path.exists(path) for path in self.paths))

    def test_165_candidates_use_one_startup_and_one_completion_reconciliation(self):
        repo = MemoryRepo()

        class OneLargeDiscoveryPage:
            calls = 0

            def search(self, query, page=1):
                self.calls += 1
                if self.calls > 1:
                    return BackgroundDiscoveryPage("pexels", query, page, 0, (), ())
                metadata = tuple(
                    MetadataIneligibleCandidate(
                        "pexels", str(index), "metadata", query, "INSUFFICIENT_DURATION"
                    )
                    for index in range(165)
                )
                return BackgroundDiscoveryPage(
                    "pexels", query, page, 165, (), metadata
                )

        result = build_verified_pool(
            repository=repo,
            provider=OneLargeDiscoveryPage(),
            add_safe=1,
            max_candidates=200,
            logger=lambda _line: None,
        )
        self.assertEqual(result.state, "candidate_exhausted")
        self.assertEqual(result.discovered, 165)
        self.assertEqual(result.metadata_skipped, 165)
        self.assertEqual(len(repo.metadata), 165)
        self.assertEqual(repo.reconcile_calls, 1)
        self.assertEqual(repo.compute_stats_calls, 1)
        self.assertEqual(repo.stats_writes, 1)
        self.assertEqual(result.stats["metadata_ineligible"], 165)

    def test_manual_refresh_performs_one_reconciliation_and_updates_cache(self):
        repo = MemoryRepo()
        stats = refresh_authoritative_stats(repo)
        self.assertEqual(repo.compute_stats_calls, 1)
        self.assertEqual(repo.stats_writes, 1)
        self.assertEqual(stats["verified_current_policy"], 0)

    def test_resume_reconciles_once_and_skips_durable_completed_candidate(self):
        repo = MemoryRepo()
        first = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(1),)),
            add_safe=1,
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(first.state, "completed")
        scans_before = len(self.paths)
        second = build_verified_pool(
            repository=repo,
            provider=Provider((candidate(1), candidate(2))),
            add_safe=1,
            download_fn=self.downloader,
            scan_fn=lambda *_args, **_kwargs: safe_result(),
            logger=lambda _line: None,
        )
        self.assertEqual(second.state, "completed")
        self.assertEqual(len(self.paths), scans_before + 1)
        self.assertEqual([record.provider_asset_id for record in repo.verified], ["1", "2"])
        self.assertEqual(repo.reconcile_calls, 2)
        self.assertEqual(repo.compute_stats_calls, 2)

    def test_stale_local_lock_is_recovered_after_restart(self):
        lock_path = Path(self.temp.name) / "builder.lock"
        lock_path.write_text("999999999", encoding="utf-8")
        with LocalBuilderLock(str(lock_path)):
            self.assertEqual(lock_path.read_text(encoding="utf-8"), str(os.getpid()))
        self.assertTrue(lock_path.exists())

    def test_live_local_lock_rejects_second_builder(self):
        lock_path = Path(self.temp.name) / "builder.lock"
        with LocalBuilderLock(str(lock_path)):
            with self.assertRaisesRegex(PoolBuilderError, "LOCAL_POOL_BUILDER_ALREADY_RUNNING"):
                with LocalBuilderLock(str(lock_path)):
                    pass

    def test_balanced_query_rounds_and_rotation(self):
        categories = BACKGROUND_SEARCH_CATEGORIES
        for offset in range(len(categories) * 2):
            queries = balanced_nature_queries(offset)
            self.assertEqual(set(queries), set(ALLOWED_NATURE_QUERIES))
            self.assertEqual(len(queries), len(set(queries)))
            expected = categories[offset % len(categories):] + categories[:offset % len(categories)]
            self.assertEqual(
                queries[:len(categories)],
                tuple(terms[offset % len(terms)] for _, terms in expected),
            )

    def test_historical_yield_cannot_override_balanced_builder_order(self):
        repo = MemoryRepo()
        original_stats = repo._stats
        high_yield_query = BACKGROUND_SEARCH_CATEGORIES[-1][1][-1]

        def yield_stats():
            return {**original_stats(), "query_metrics": {
                "historically-dominant": {"query": high_yield_query,
                    "scans_attempted": 1000, "verified_safe": 1000}
            }}

        repo._stats = yield_stats
        calls = []

        class EmptyProvider:
            def search(self, query, page=1):
                calls.append(query)
                return ()

        with patch("engine.background_pool_builder.balanced_nature_queries",
                   return_value=balanced_nature_queries(0)):
            build_verified_pool(
                repository=repo, provider=EmptyProvider(), add_safe=1,
                max_pages_per_query=1, logger=lambda _: None,
            )
        self.assertEqual(tuple(calls), balanced_nature_queries(0))

    def test_manual_builder_uses_weighted_pexels_orientation_lanes(self):
        calls: list[tuple[str, int, str | None]] = []

        class OrientedProvider:
            name = "pexels"

            def search(self, query, page=1, orientation=None):
                calls.append((query, page, orientation))
                return ()

        queries = ALLOWED_NATURE_QUERIES[: len(PEXELS_DISCOVERY_ORIENTATION_CYCLE)]
        result = build_verified_pool(
            repository=MemoryRepo(),
            provider=OrientedProvider(),
            add_safe=1,
            max_candidates=10,
            max_pages_per_query=1,
            query_subset=queries,
            logger=lambda _line: None,
        )

        self.assertEqual(result.state, "candidate_exhausted")
        self.assertEqual(
            [orientation for _, _, orientation in calls],
            list(PEXELS_DISCOVERY_ORIENTATION_CYCLE),
        )


if __name__ == "__main__": unittest.main()
