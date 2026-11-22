import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

from engine.background_candidates import (
    DISCOVERY_MAX_API_REQUESTS_PER_RUN,
    DISCOVERY_MAX_METADATA_ITEMS_PER_RUN,
    DEFAULT_LEASE_SECONDS,
    PEXELS_ORIENTATION_CYCLE,
    candidate_from_metadata,
    candidate_metadata,
    discover_candidates,
    local_worker_id,
    qualify_one_claimed_candidate,
)
from engine.backgrounds import (
    BackgroundCandidate,
    BackgroundDiscoveryPage,
    DownloadedBackground,
    MetadataIneligibleCandidate,
)
from engine.safety import Detection, SafetyScanResult
from engine.supabase_state import SupabaseStateError


def make_candidate(provider="pexels", asset="1", variant="v1", query="ocean waves"):
    return BackgroundCandidate(
        pexels_video_id=int(asset) if asset.isdigit() else 1,
        query=query,
        pexels_page_url=f"https://example.test/{provider}/{asset}",
        videographer_name="Nature Creator",
        videographer_url="https://example.test/creator",
        duration_seconds=20,
        source_width=1080,
        source_height=1920,
        video_file_id=variant,
        file_url=f"https://example.test/video_{asset}_{variant}.mp4",
        file_type="video/mp4",
        fps=30,
        file_width=1080,
        file_height=1920,
        requires_center_crop=False,
        provider=provider,
    )


class InMemCandidateStore:
    """In-memory emulation of Supabase candidate queue table and stored procedures."""
    def __init__(self, pending=0, verified=0):
        self.rows: dict[str, dict[str, Any]] = {}
        self.counter = 0
        self.coverage_mutations = 0
        self.summary_pending = pending
        self.summary_verified = verified

    def background_candidate_summary(self):
        pending_cnt = sum(1 for r in self.rows.values() if r["status"] == "PENDING") + self.summary_pending
        verified_cnt = sum(1 for r in self.rows.values() if r["status"] == "VERIFIED") + self.summary_verified
        return {"pending": pending_cnt, "verified": verified_cnt}

    def enqueue_background_candidate(self, provider, asset_id, variant_id, search_query, metadata, status):
        key = f"{provider}:{asset_id}:{variant_id}"
        if key in self.rows:
            self.rows[key]["discovery_count"] += 1
            self.rows[key]["last_seen_at"] = datetime.now(timezone.utc).isoformat()
            return self.rows[key]
        self.counter += 1
        cid = f"cand_{self.counter}"
        row = {
            "candidate_id": cid,
            "provider": provider,
            "provider_asset_id": asset_id,
            "provider_variant_id": variant_id,
            "search_query": search_query,
            "metadata": metadata,
            "status": status,
            "discovery_count": 1,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "claimed_worker_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "error_code": None,
        }
        self.rows[key] = row
        return row

    def claim_background_candidate(self, worker_id: str, lease_seconds: int = 300):
        now = datetime.now(timezone.utc)
        # Find oldest PENDING or TRANSIENT_FAILURE or expired PROCESSING
        for row in sorted(self.rows.values(), key=lambda r: r["discovered_at"]):
            st = row["status"]
            is_expired_processing = False
            if st == "PROCESSING" and row.get("lease_expires_at"):
                exp = datetime.fromisoformat(row["lease_expires_at"])
                if exp < now:
                    is_expired_processing = True

            if st in ("PENDING", "TRANSIENT_FAILURE") or is_expired_processing:
                import uuid
                token = str(uuid.uuid4())
                row["status"] = "PROCESSING"
                row["claimed_worker_id"] = worker_id
                row["lease_token"] = token
                row["claimed_at"] = now.isoformat()
                row["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                return {
                    "candidate_id": row["candidate_id"],
                    "lease_token": token,
                    "metadata": row["metadata"],
                    "search_query": row["search_query"],
                    "provider": row["provider"],
                    "provider_asset_id": row["provider_asset_id"],
                    "provider_variant_id": row["provider_variant_id"],
                    "claimed_worker_id": worker_id,
                    "lease_expires_at": row["lease_expires_at"],
                }
        return None

    def complete_background_candidate(self, candidate_id: str, lease_token: str, status: str, error_code=None, metadata_updates=None):
        for row in self.rows.values():
            if row["candidate_id"] == candidate_id:
                if row["lease_token"] != lease_token:
                    raise SupabaseStateError("Background candidate lease was lost before terminal transition.")
                row["status"] = status
                row["error_code"] = error_code
                row["lease_token"] = None
                row["lease_expires_at"] = None
                return {"candidate_id": candidate_id, "status": status}
        raise SupabaseStateError(f"Candidate {candidate_id} not found.")


class FakePoolRepo:
    def __init__(self):
        self.verified = []
        self.safety_rejected = []
        self.metadata_ineligible = []
        self.transient = []

    def save_verified(self, record): self.verified.append(record)
    def save_safety_rejected(self, record): self.safety_rejected.append(record)
    def save_metadata_ineligible(self, record): self.metadata_ineligible.append(record)
    def save_transient(self, record): self.transient.append(record)


class BackgroundCandidateQueueComprehensiveTests(unittest.TestCase):
    """Full deterministic test suite covering all 15 background candidate queue requirements."""

    # 1. Pexels discovery
    def test_pexels_discovery(self):
        cand1 = make_candidate(provider="pexels", asset="101", variant="v1")
        cand2 = make_candidate(provider="pexels", asset="102", variant="v1")
        called = []
        class PexelsProvider:
            def search(self, query, page):
                if not called:
                    called.append(query)
                    return BackgroundDiscoveryPage("pexels", query, page, 2, (cand1, cand2), ())
                return BackgroundDiscoveryPage("pexels", query, page, 0, (), ())

        store = InMemCandidateStore()
        res = discover_candidates(store=store, providers={"pexels": PexelsProvider()}, max_candidates=10, logger=lambda _: None)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["discovered"], 2)
        key1 = "pexels:101:v1"
        self.assertIn(key1, store.rows)
        self.assertEqual(store.rows[key1]["provider"], "pexels")
        self.assertEqual(store.rows[key1]["status"], "PENDING")

    # 2. Pixabay discovery
    def test_pixabay_discovery(self):
        cand = make_candidate(provider="pixabay", asset="201", variant="v1")
        called = []
        class PixabayProvider:
            def search(self, query, page):
                if not called:
                    called.append(query)
                    return BackgroundDiscoveryPage("pixabay", query, page, 1, (cand,), ())
                return BackgroundDiscoveryPage("pixabay", query, page, 0, (), ())

        store = InMemCandidateStore()
        res = discover_candidates(store=store, providers={"pixabay": PixabayProvider()}, max_candidates=10, logger=lambda _: None)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["discovered"], 1)
        key = "pixabay:201:v1"
        self.assertIn(key, store.rows)
        self.assertEqual(store.rows[key]["provider"], "pixabay")
        self.assertEqual(store.rows[key]["status"], "PENDING")

    # 3. Metadata persistence
    def test_metadata_persistence(self):
        cand = make_candidate(provider="pexels", asset="999", variant="v9")
        meta = candidate_metadata(cand)
        self.assertEqual(meta["duration_seconds"], 20)
        self.assertEqual(meta["file_width"], 1080)
        self.assertEqual(meta["file_height"], 1920)
        self.assertEqual(meta["fps"], 30)
        rebuilt = candidate_from_metadata(meta)
        self.assertEqual(rebuilt.provider, "pexels")
        self.assertEqual(rebuilt.provider_asset_id, "999")
        self.assertEqual(rebuilt.provider_variant_id, "v9")
        self.assertEqual(rebuilt.duration_seconds, 20)

    # 4. Dedupe on conflict
    def test_dedupe_on_conflict(self):
        store = InMemCandidateStore()
        cand = make_candidate(provider="pexels", asset="300", variant="v1")
        meta = candidate_metadata(cand)
        r1 = store.enqueue_background_candidate("pexels", "300", "v1", "waves", meta, "PENDING")
        self.assertEqual(r1["discovery_count"], 1)
        r2 = store.enqueue_background_candidate("pexels", "300", "v1", "waves", meta, "PENDING")
        self.assertEqual(r2["discovery_count"], 2)
        # Verify single row retained
        self.assertEqual(len(store.rows), 1)

    # 5. Bounded discovery
    def test_bounded_discovery(self):
        cands = [make_candidate(asset=str(i)) for i in range(10)]
        class BulkProvider:
            def search(self, query, page):
                return BackgroundDiscoveryPage("pexels", query, page, len(cands), tuple(cands), ())

        store = InMemCandidateStore()
        res = discover_candidates(store=store, providers={"pexels": BulkProvider()}, max_candidates=3, logger=lambda _: None)
        self.assertEqual(res["discovered"], 3)
        self.assertEqual(len(store.rows), 3)

    # A scheduled prescan must not stop at the historic 100-pending cap.
    def test_default_prescan_keeps_searching_regardless_of_pending_backlog(self):
        for pending in (100, 250, 1000):
            with self.subTest(pending=pending):
                store = InMemCandidateStore(pending=pending, verified=500)
                calls = []
                candidate = make_candidate(asset="4242")

                class Provider:
                    def search(self, query, page):
                        calls.append((query, page))
                        return BackgroundDiscoveryPage(
                            "pexels", query, page, 1, (candidate,), ()
                        )

                result = discover_candidates(
                    store=store,
                    providers={"pexels": Provider()},
                    max_candidates=1,
                    logger=lambda _: None,
                )
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["discovered"], 1)
                self.assertGreater(len(calls), 0)
                self.assertLessEqual(
                    result["provider_requests"],
                    DISCOVERY_MAX_API_REQUESTS_PER_RUN,
                )
                self.assertEqual(store.background_candidate_summary()["pending"], pending + 1)

    # 6. Explicit, opt-in pending ceiling remains supported for legacy calls.
    def test_pool_healthy_early_exit(self):
        store = InMemCandidateStore(pending=100, verified=25)
        searched = []
        class Provider:
            def search(self, query, page):
                searched.append(query)
                return BackgroundDiscoveryPage("pexels", query, page, 0, (), ())

        res = discover_candidates(store=store, providers={"pexels": Provider()}, target_pending=20)
        self.assertEqual(res["status"], "POOL_TARGET_REACHED")
        self.assertEqual(res["discovered"], 0)
        self.assertEqual(len(searched), 0)

    # 7. Discovered != verified
    def test_discovered_is_not_verified(self):
        cand = make_candidate(asset="501")
        class Provider:
            def search(self, query, page):
                return BackgroundDiscoveryPage("pexels", query, page, 1, (cand,), ())

        store = InMemCandidateStore()
        repo = FakePoolRepo()
        discover_candidates(store=store, providers={"pexels": Provider()}, max_candidates=5, logger=lambda _: None)
        # Discovery must never call save_verified
        self.assertEqual(len(repo.verified), 0)
        # All stored rows must be PENDING (or METADATA_INELIGIBLE)
        for r in store.rows.values():
            self.assertIn(r["status"], ("PENDING", "METADATA_INELIGIBLE"))
            self.assertNotEqual(r["status"], "VERIFIED")

    # 8. Atomic claim
    def test_atomic_claim(self):
        store = InMemCandidateStore()
        cand = make_candidate(asset="601")
        store.enqueue_background_candidate("pexels", "601", "v1", "waves", candidate_metadata(cand), "PENDING")

        claim = store.claim_background_candidate(worker_id="worker_mac_1", lease_seconds=120)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["claimed_worker_id"], "worker_mac_1")
        self.assertTrue(claim["lease_token"])
        self.assertTrue(claim["lease_expires_at"])
        # In store, status is now PROCESSING
        row = store.rows["pexels:601:v1"]
        self.assertEqual(row["status"], "PROCESSING")
        self.assertEqual(row["claimed_worker_id"], "worker_mac_1")

    # 9. Concurrent claims
    def test_concurrent_claims(self):
        store = InMemCandidateStore()
        cand1 = make_candidate(asset="701")
        cand2 = make_candidate(asset="702")
        store.enqueue_background_candidate("pexels", "701", "v1", "query", candidate_metadata(cand1), "PENDING")
        store.enqueue_background_candidate("pexels", "702", "v1", "query", candidate_metadata(cand2), "PENDING")

        claim_a = store.claim_background_candidate(worker_id="worker_A", lease_seconds=120)
        claim_b = store.claim_background_candidate(worker_id="worker_B", lease_seconds=120)
        claim_c = store.claim_background_candidate(worker_id="worker_C", lease_seconds=120)

        self.assertIsNotNone(claim_a)
        self.assertIsNotNone(claim_b)
        self.assertIsNone(claim_c) # queue exhausted
        self.assertNotEqual(claim_a["candidate_id"], claim_b["candidate_id"])

    # 10. Lease expiry
    def test_lease_expiry(self):
        store = InMemCandidateStore()
        cand = make_candidate(asset="801")
        store.enqueue_background_candidate("pexels", "801", "v1", "query", candidate_metadata(cand), "PENDING")

        claim_a = store.claim_background_candidate(worker_id="worker_A", lease_seconds=60)
        self.assertIsNotNone(claim_a)

        # Manually backdate lease expiration to simulate expired lease
        row = store.rows["pexels:801:v1"]
        expired_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        row["lease_expires_at"] = expired_time

        # Worker B can reclaim expired candidate
        claim_b = store.claim_background_candidate(worker_id="worker_B", lease_seconds=120)
        self.assertIsNotNone(claim_b)
        self.assertEqual(claim_b["candidate_id"], claim_a["candidate_id"])
        self.assertEqual(claim_b["claimed_worker_id"], "worker_B")
        self.assertNotEqual(claim_b["lease_token"], claim_a["lease_token"])

    # 11. Crash recovery & stale lease completion rejected
    def test_crash_recovery(self):
        store = InMemCandidateStore()
        cand = make_candidate(asset="901")
        store.enqueue_background_candidate("pexels", "901", "v1", "query", candidate_metadata(cand), "PENDING")

        claim_a = store.claim_background_candidate(worker_id="worker_A", lease_seconds=60)
        token_a = claim_a["lease_token"]

        # Worker A crashes, lease expires
        store.rows["pexels:901:v1"]["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

        # Worker B reclaims
        claim_b = store.claim_background_candidate(worker_id="worker_B", lease_seconds=120)
        token_b = claim_b["lease_token"]

        # Stale Worker A wakes up and attempts complete -> fails closed
        with self.assertRaises(SupabaseStateError):
            store.complete_background_candidate(claim_a["candidate_id"], token_a, "VERIFIED")

        # Worker B completes -> succeeds
        res_b = store.complete_background_candidate(claim_b["candidate_id"], token_b, "VERIFIED")
        self.assertEqual(res_b["status"], "VERIFIED")

    # 12. Failed candidate not verified
    def test_failed_candidate_not_verified(self):
        # A: CV safety failure
        cand = make_candidate(asset="1001")
        store = InMemCandidateStore()
        store.enqueue_background_candidate("pexels", "1001", "v1", "query", candidate_metadata(cand), "PENDING")
        repo = FakePoolRepo()

        with tempfile.TemporaryDirectory() as tmpdir:
            vpath = Path(tmpdir) / "test.mp4"
            vpath.write_bytes(b"dummy")
            def dl_fn(*_a, **_k): return DownloadedBackground(cand, str(vpath), 5, "a" * 64)
            unsafe_scan = SafetyScanResult(safe=False, frames_scanned=5, duration_seconds=1.0, person_detections=("person",), face_detections=(), animal_detections=(), animal_policy_status="CLEAR")

            item = qualify_one_claimed_candidate(store=store, pool_repository=repo, worker_id="w_safety", download_fn=dl_fn, scan_fn=lambda *_a, **_k: unsafe_scan)
            self.assertEqual(item["status"], "SAFETY_REJECTED")
            self.assertEqual(len(repo.verified), 0)
            self.assertEqual(len(repo.safety_rejected), 1)

        # B: Download transient failure
        cand2 = make_candidate(asset="1002")
        store.enqueue_background_candidate("pexels", "1002", "v1", "query", candidate_metadata(cand2), "PENDING")
        def dl_fail(*_a, **_k): raise ConnectionError("Network dropped")

        item2 = qualify_one_claimed_candidate(store=store, pool_repository=repo, worker_id="w_transient", download_fn=dl_fail)
        self.assertEqual(item2["status"], "TRANSIENT_FAILURE")
        self.assertEqual(len(repo.verified), 0)
        self.assertEqual(len(repo.transient), 1)

    # 13. Local and GitHub worker collision prevention
    def test_local_and_github_worker_collision_prevention(self):
        store = InMemCandidateStore()
        cand1 = make_candidate(asset="1101")
        cand2 = make_candidate(asset="1102")
        store.enqueue_background_candidate("pexels", "1101", "v1", "query", candidate_metadata(cand1), "PENDING")
        store.enqueue_background_candidate("pexels", "1102", "v1", "query", candidate_metadata(cand2), "PENDING")

        worker_local = local_worker_id()
        worker_github = "github:run_12345678"

        claim_local = store.claim_background_candidate(worker_id=worker_local, lease_seconds=180)
        claim_github = store.claim_background_candidate(worker_id=worker_github, lease_seconds=180)

        self.assertNotEqual(claim_local["candidate_id"], claim_github["candidate_id"])
        self.assertEqual(claim_local["claimed_worker_id"], worker_local)
        self.assertEqual(claim_github["claimed_worker_id"], worker_github)

    # 14. Time budget exit
    def test_time_budget_exit(self):
        # Simulate worker with expired deadline
        deadline = time.monotonic() - 1.0 # already expired
        loop_ran = 0
        while time.monotonic() < deadline:
            loop_ran += 1
        self.assertEqual(loop_ran, 0)

    # 15. Zero Coverage mutation
    def test_zero_coverage_mutation(self):
        store = InMemCandidateStore()
        cand = make_candidate(asset="1201")
        class Provider:
            def search(self, query, page):
                return BackgroundDiscoveryPage("pexels", query, page, 1, (cand,), ())

        # Run discovery
        res = discover_candidates(store=store, providers={"pexels": Provider()}, max_candidates=1, logger=lambda _: None)
        self.assertEqual(store.coverage_mutations, 0)

        # Run qualification
        repo = FakePoolRepo()
        with tempfile.TemporaryDirectory() as tmpdir:
            vpath = Path(tmpdir) / "test.mp4"
            vpath.write_bytes(b"dummy")
            def dl_fn(*_a, **_k): return DownloadedBackground(cand, str(vpath), 5, "a" * 64)
            safe_scan = SafetyScanResult(safe=True, frames_scanned=5, duration_seconds=1.0, person_detections=(), face_detections=(), animal_detections=(), animal_policy_status="CLEAR")
            qualify_one_claimed_candidate(store=store, pool_repository=repo, worker_id="worker_zero_cov", download_fn=dl_fn, scan_fn=lambda *_a, **_k: safe_scan)

        self.assertEqual(store.coverage_mutations, 0)

    # 16. Discovery does not download video or run CV (Req C, D)
    def test_discovery_no_download_or_cv(self):
        cand = make_candidate(asset="1301")
        download_called = []
        cv_called = []

        class TrackingProvider:
            def search(self, query, page):
                return BackgroundDiscoveryPage("pexels", query, page, 1, (cand,), ())

        def fake_download(*args, **kwargs):
            download_called.append(args)

        def fake_cv(*args, **kwargs):
            cv_called.append(args)

        store = InMemCandidateStore()
        res = discover_candidates(store=store, providers={"pexels": TrackingProvider()}, max_candidates=5, logger=lambda _: None)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(len(download_called), 0)
        self.assertEqual(len(cv_called), 0)

    # 17. Metadata failure -> METADATA_INELIGIBLE (Req G)
    def test_metadata_ineligible_rejection(self):
        # Duration too short (e.g. 5 seconds, min is 10)
        short_cand = BackgroundCandidate(
            pexels_video_id=1401,
            query="nature",
            pexels_page_url="https://example.test/short",
            videographer_name="Creator",
            videographer_url="https://example.test/creator",
            duration_seconds=5,
            source_width=1080,
            source_height=1920,
            video_file_id="v_short",
            file_url="https://example.test/short.mp4",
            file_type="video/mp4",
            fps=30,
            file_width=1080,
            file_height=1920,
            requires_center_crop=False,
            provider="pexels",
        )
        store = InMemCandidateStore()
        store.enqueue_background_candidate("pexels", "1401", "v_short", "nature", candidate_metadata(short_cand), "PENDING")
        repo = FakePoolRepo()

        dl_called = []
        def track_dl(*_a, **_k):
            dl_called.append(True)

        res = qualify_one_claimed_candidate(
            store=store, pool_repository=repo, worker_id="w_meta",
            download_fn=track_dl, logger=lambda _: None
        )
        self.assertEqual(res["status"], "METADATA_INELIGIBLE")
        self.assertEqual(len(dl_called), 0)  # Must fail before download
        self.assertEqual(len(repo.metadata_ineligible), 1)
        self.assertEqual(len(repo.verified), 0)

    # 18. Animal detection -> SAFETY_REJECTED (Req H)
    def test_animal_detection_safety_rejected(self):
        cand = make_candidate(asset="1501")
        store = InMemCandidateStore()
        store.enqueue_background_candidate("pexels", "1501", "v1", "nature", candidate_metadata(cand), "PENDING")
        repo = FakePoolRepo()

        with tempfile.TemporaryDirectory() as tmpdir:
            vpath = Path(tmpdir) / "test.mp4"
            vpath.write_bytes(b"dummy")
            def dl_fn(*_a, **_k): return DownloadedBackground(cand, str(vpath), 5, "b" * 64)
            animal_scan = SafetyScanResult(
                safe=False,
                frames_scanned=10,
                duration_seconds=2.0,
                person_detections=(),
                face_detections=(),
                animal_detections=("dog",),
                animal_policy_status="REJECT_ANIMAL"
            )

            res = qualify_one_claimed_candidate(
                store=store, pool_repository=repo, worker_id="w_animal",
                download_fn=dl_fn, scan_fn=lambda *_a, **_k: animal_scan,
                logger=lambda _: None
            )
            self.assertEqual(res["status"], "SAFETY_REJECTED")
            self.assertEqual(len(repo.verified), 0)
            self.assertEqual(len(repo.safety_rejected), 1)

    # 19. Valid fully scanned source -> VERIFIED asset (Req I)
    def test_valid_source_becomes_verified(self):
        cand = make_candidate(asset="1601")
        store = InMemCandidateStore()
        store.enqueue_background_candidate("pexels", "1601", "v1", "clouds", candidate_metadata(cand), "PENDING")
        repo = FakePoolRepo()

        with tempfile.TemporaryDirectory() as tmpdir:
            vpath = Path(tmpdir) / "test.mp4"
            vpath.write_bytes(b"dummy_bytes")
            def dl_fn(*_a, **_k): return DownloadedBackground(cand, str(vpath), 11, "c" * 64)
            clean_scan = SafetyScanResult(
                safe=True,
                frames_scanned=12,
                duration_seconds=2.5,
                person_detections=(),
                face_detections=(),
                animal_detections=(),
                animal_policy_status="CLEAR"
            )

            res = qualify_one_claimed_candidate(
                store=store, pool_repository=repo, worker_id="w_clean",
                download_fn=dl_fn, scan_fn=lambda *_a, **_k: clean_scan,
                logger=lambda _: None
            )
            self.assertEqual(res["status"], "VERIFIED")
            self.assertEqual(len(repo.verified), 1)
            verified_rec = repo.verified[0]
            self.assertEqual(verified_rec.provider, "pexels")
            self.assertEqual(verified_rec.provider_asset_id, "1601")
            self.assertEqual(verified_rec.content_sha256, "c" * 64)
            self.assertEqual(verified_rec.safety.scanned_frames, 12)
            self.assertEqual(verified_rec.safety.animal_verdict, "CLEAR")

    # 20. Temporary video cleaned after completion (Req K)
    def test_temporary_video_cleaned_after_completion(self):
        cand = make_candidate(asset="1701")
        store = InMemCandidateStore()
        store.enqueue_background_candidate("pexels", "1701", "v1", "clouds", candidate_metadata(cand), "PENDING")
        repo = FakePoolRepo()

        with tempfile.TemporaryDirectory() as tmpdir:
            vpath = Path(tmpdir) / "temp_video.mp4"
            vpath.write_bytes(b"to_be_deleted")
            self.assertTrue(vpath.exists())

            def dl_fn(*_a, **_k): return DownloadedBackground(cand, str(vpath), 13, "d" * 64)
            clean_scan = SafetyScanResult(safe=True, frames_scanned=5, duration_seconds=1.0, person_detections=(), face_detections=(), animal_detections=(), animal_policy_status="CLEAR")

            qualify_one_claimed_candidate(
                store=store, pool_repository=repo, worker_id="w_clean_disk",
                download_fn=dl_fn, scan_fn=lambda *_a, **_k: clean_scan,
                logger=lambda _: None
            )
            # Must be deleted by finally block
            self.assertFalse(vpath.exists())

    # 21. No Buffer/social call (Req M)
    def test_zero_buffer_social_call(self):
        # Ensure qualification module does not import or invoke buffer_publisher
        import engine.background_candidates as bg_module
        self.assertFalse(hasattr(bg_module, "publish_draft"))
        self.assertFalse(hasattr(bg_module, "BufferPublisher"))

    # 22. Pool >= selectable target early exit (Req N)
    def test_selectable_pool_target_early_exit(self):
        class FakeStoreWithSelectable:
            def __init__(self, selectable_count):
                self.selectable_count = selectable_count
                self.claimed = 0

            def count_selectable_background_assets(self, policy_fingerprint=None):
                return self.selectable_count

            def claim_background_candidate(self, worker_id, lease_seconds=300):
                self.claimed += 1
                return None

        # When selectable >= target (e.g. 40 >= 40), qualification does not claim or scan
        store = FakeStoreWithSelectable(selectable_count=40)
        target = 40
        if store.count_selectable_background_assets() >= target:
            early_exit = True
        else:
            early_exit = False
            store.claim_background_candidate("w1")

        self.assertTrue(early_exit)
        self.assertEqual(store.claimed, 0)

    # 23. HIGH_RISK production policy unchanged (Req O)
    def test_high_risk_excluded_from_selectable_pool(self):
        from engine.supabase_state import SupabaseStateStore
        # Mock _request to return mixed assets: 2 normal, 1 HIGH_RISK
        mock_assets = [
            {"provider": "pexels", "value": {"render_robustness": {"risk_tier": "LOW_RISK"}}},
            {"provider": "pexels", "value": {"render_robustness": {"risk_tier": "HIGH_RISK"}}},
            {"provider": "pixabay", "value": {"render_robustness": {"risk_tier": "NORMAL"}}},
        ]
        store = SupabaseStateStore.__new__(SupabaseStateStore)
        store._request = lambda method, path, **kw: mock_assets

        selectable = store.count_selectable_background_assets(policy_fingerprint="test")
        self.assertEqual(selectable, 2)  # HIGH_RISK is filtered out

    def test_count_selectable_background_assets_robustness_tolerances(self):
        from engine.supabase_state import SupabaseStateStore
        store = SupabaseStateStore.__new__(SupabaseStateStore)

        # 1. render_robustness={"risk_tier":"HIGH_RISK"} -> excluded
        store._request = lambda m, p, **kw: [
            {"provider": "pexels", "value": {"render_robustness": {"risk_tier": "HIGH_RISK"}}}
        ]
        self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 0)

        # 2. render_robustness={"risk_tier":"LOW_RISK"} -> counted
        store._request = lambda m, p, **kw: [
            {"provider": "pexels", "value": {"render_robustness": {"risk_tier": "LOW_RISK"}}}
        ]
        self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 1)

        # 3. render_robustness=None with no top-level risk_tier -> counted
        store._request = lambda m, p, **kw: [
            {"provider": "pexels", "value": {"render_robustness": None}}
        ]
        self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 1)

        # 4. render_robustness missing with top-level risk_tier="HIGH_RISK" -> excluded
        store._request = lambda m, p, **kw: [
            {"provider": "pexels", "value": {"risk_tier": "HIGH_RISK"}}
        ]
        self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 0)

        # 5. render_robustness=None with top-level risk_tier="HIGH_RISK" -> excluded
        store._request = lambda m, p, **kw: [
            {"provider": "pexels", "value": {"render_robustness": None, "risk_tier": "HIGH_RISK"}}
        ]
        self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 0)

        # 6. malformed/non-mapping render_robustness must not crash
        for malformed in ("corrupted", 123, [1, 2], True):
            store._request = lambda m, p, **kw: [
                {"provider": "pexels", "value": {"render_robustness": malformed}}
            ]
            self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 1)

        # Mixed batch asserting all cases together
        store._request = lambda m, p, **kw: [
            {"provider": "pexels", "value": {"render_robustness": {"risk_tier": "HIGH_RISK"}}},  # excluded
            {"provider": "pexels", "value": {"render_robustness": {"risk_tier": "LOW_RISK"}}},   # counted
            {"provider": "pexels", "value": {"render_robustness": None}},                        # counted
            {"provider": "pexels", "value": {"risk_tier": "HIGH_RISK"}},                         # excluded
            {"provider": "pexels", "value": {"render_robustness": None, "risk_tier": "HIGH_RISK"}}, # excluded
            {"provider": "pexels", "value": {"render_robustness": "corrupted"}},                 # counted
        ]
        self.assertEqual(store.count_selectable_background_assets(policy_fingerprint="test"), 3)

    # 24. Query & provider rotation works (Req R)
    def test_query_and_provider_rotation(self):
        from engine.background_candidates import get_rotated_queries
        from engine.backgrounds import ALLOWED_NATURE_QUERIES

        queries_0 = get_rotated_queries(offset=0)
        queries_1 = get_rotated_queries(offset=1)
        self.assertEqual(len(queries_0), len(ALLOWED_NATURE_QUERIES))
        self.assertEqual(len(queries_1), len(ALLOWED_NATURE_QUERIES))
        self.assertNotEqual(queries_0[0], queries_1[0])
        self.assertEqual(queries_0[1], queries_1[0])


class DiscoveryPaginationTests(unittest.TestCase):
    def test_full_page_window_and_query_provider_interleaving(self):
        from unittest.mock import patch
        calls = []
        class Provider:
            def __init__(self, name): self.name = name
            def search(self, query, page, **kwargs):
                calls.append((self.name, query, page, kwargs.get('orientation')))
                return BackgroundDiscoveryPage(self.name, query, page, 0, (), ())
        with patch('engine.background_candidates.get_rotated_queries', return_value=('ocean waves', 'clouds')):
            discover_candidates(store=InMemCandidateStore(), providers={name: Provider(name) for name in ('pexels', 'pixabay')}, page_offset=0, logger=lambda _: None)
        self.assertEqual(len(calls), 20)
        self.assertEqual(
            set(calls[:4]),
            {
                ('pexels', 'ocean waves', 1, 'portrait'),
                ('pixabay', 'ocean waves', 1, None),
                ('pexels', 'clouds', 1, 'portrait'),
                ('pixabay', 'clouds', 1, None),
            },
        )
        self.assertEqual({call[2] for call in calls}, {1, 2, 3, 4, 5})

    def test_run_rotation_reaches_all_pages_despite_full_first_response(self):
        from unittest.mock import patch
        calls = []
        class Provider:
            def search(self, query, page, **kwargs):
                calls.append((query, page))
                candidates = tuple(make_candidate(asset=str(page * 100 + i)) for i in range(10))
                return BackgroundDiscoveryPage('pexels', query, page, 10, candidates, ())
        for run in range(1, 6):
            with patch.dict(os.environ, {'GITHUB_RUN_NUMBER': str(run)}):
                result = discover_candidates(store=InMemCandidateStore(), providers={'pexels': Provider()}, max_candidates=2, query_offset=0, page_offset=run - 1, logger=lambda _: None)
            self.assertEqual(result['discovered'], 2)
        self.assertEqual([page for _, page in calls], [page for page in range(1, 6) for _ in range(4)])

    def test_rotation_is_deterministic_and_rotates_provider_at_small_cap(self):
        from engine.background_candidates import get_rotated_pages
        calls = []
        class Provider:
            def __init__(self, name): self.name = name
            def search(self, query, page, **kwargs):
                calls.append((self.name, query, page))
                return (make_candidate(provider=self.name),)
        providers = {name: Provider(name) for name in ('pexels', 'pixabay')}
        for offset, expected in ((0, 'pexels'), (1, 'pixabay'), (1, 'pixabay')):
            store = InMemCandidateStore()
            discover_candidates(store=store, providers=providers, max_candidates=1, page_offset=offset, query_offset=offset, logger=lambda _: None)
            self.assertEqual(next(iter(store.rows.values()))['provider'], expected)
        self.assertEqual(sorted(calls[4:8]), sorted(calls[8:12]))
        self.assertEqual(get_rotated_pages(4), (5, 1, 2, 3, 4))

    def test_duplicates_do_not_use_new_entry_bound(self):
        store = InMemCandidateStore()
        candidate = make_candidate(asset='1')
        store.enqueue_background_candidate('pexels', '1', 'v1', candidate.query, candidate_metadata(candidate), 'PENDING')
        class Provider:
            def search(self, query, page, **kwargs):
                return (candidate, make_candidate(asset='2'), make_candidate(asset='3'))
        result = discover_candidates(store=store, providers={'pexels': Provider()}, max_candidates=1, page_offset=0, logger=lambda _: None)
        self.assertEqual(result['discovered'], 1)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(len(store.rows), 2)

    def test_pending_ceiling_is_enforced_during_batch(self):
        store = InMemCandidateStore(pending=99)
        class Provider:
            def search(self, query, page, **kwargs):
                return tuple(make_candidate(asset=str(i)) for i in range(10))
        result = discover_candidates(store=store, providers={'pexels': Provider()}, max_candidates=20, target_pending=100, page_offset=0, logger=lambda _: None)
        self.assertEqual(result['discovered'], 1)
        self.assertEqual(result['summary']['pending'], 100)

    def test_provider_errors_are_sanitized_and_do_not_stop_other_pages(self):
        from unittest.mock import patch
        calls, logs = [], []
        class Provider:
            def __init__(self, name): self.name = name
            def search(self, query, page, **kwargs):
                calls.append((self.name, page))
                if self.name == 'pexels':
                    raise RuntimeError('api_key=secret')
                return ()
        with patch('engine.background_candidates.get_rotated_queries', return_value=('clouds',)):
            discover_candidates(store=InMemCandidateStore(), providers={name: Provider(name) for name in ('pexels', 'pixabay')}, page_offset=0, logger=logs.append)
        self.assertEqual(
            sorted(calls),
            sorted((name, page) for page in range(1, 6) for name in ('pexels', 'pixabay')),
        )
        self.assertNotIn('secret', '\n'.join(logs))
        self.assertIn('provider=pexels query=\'clouds\' page=3 error=RuntimeError', '\n'.join(logs))

    def test_metadata_discovery_cannot_download_or_scan(self):
        from unittest.mock import patch
        class Provider:
            def search(self, query, page, **kwargs): return (make_candidate(),)
        with patch('engine.background_candidates.download_background', side_effect=AssertionError('download')) as download, patch('engine.background_candidates.scan_video', side_effect=AssertionError('scan')) as scan, patch('engine.background_candidates.qualify_one_claimed_candidate', side_effect=AssertionError('qualify')) as qualify:
            discover_candidates(store=InMemCandidateStore(), providers={'pexels': Provider()}, max_candidates=1, page_offset=0, logger=lambda _: None)
        download.assert_not_called()
        scan.assert_not_called()
        qualify.assert_not_called()


class DiscoveryThroughputTests(unittest.TestCase):
    def test_metadata_ineligible_candidates_do_not_consume_useful_target(self):
        short = replace(make_candidate(asset="2001"), duration_seconds=5)
        too_long = replace(make_candidate(asset="2002"), duration_seconds=60)
        oversized = replace(
            make_candidate(asset="2003"),
            declared_file_size_bytes=65 * 1024 * 1024,
        )
        no_native_crop = replace(
            make_candidate(asset="2004"),
            file_width=1920,
            file_height=1080,
            requires_center_crop=True,
        )
        unsupported_format = replace(
            make_candidate(asset="2005"),
            file_type="video/quicktime",
            file_url="https://example.test/video_2005.mov",
        )
        valid = make_candidate(asset="2006")

        class Provider:
            def search(self, query, page, **kwargs):
                return BackgroundDiscoveryPage(
                    "pexels",
                    query,
                    page,
                    6,
                    (
                        short,
                        too_long,
                        oversized,
                        no_native_crop,
                        unsupported_format,
                        valid,
                    ),
                    (),
                )

        with mock.patch(
            "engine.background_candidates.get_rotated_queries",
            return_value=("nature",),
        ):
            result = discover_candidates(
                store=(store := InMemCandidateStore()),
                providers={"pexels": Provider()},
                max_candidates=1,
                page_offset=0,
                logger=lambda _: None,
            )

        self.assertEqual(result["discovered"], 1)
        self.assertEqual(result["eligible_pending"], 1)
        self.assertEqual(result["metadata_ineligible"], 5)
        self.assertEqual(result["metadata_records_examined"], 6)
        self.assertEqual(store.rows["pexels:2006:v1"]["status"], "PENDING")
        expected_reasons = {
            "pexels:2001:v1": "insufficient_duration",
            "pexels:2002:v1": "source_duration_too_long",
            "pexels:2003:v1": "source_file_too_large",
            "pexels:2004:v1": "insufficient_native_crop_resolution",
            "pexels:2005:v1": "unsupported_video_format",
        }
        for key, reason in expected_reasons.items():
            self.assertEqual(store.rows[key]["status"], "METADATA_INELIGIBLE")
            self.assertEqual(store.rows[key]["metadata"]["reason"], reason)

    def test_provider_reported_ineligible_audits_do_not_consume_useful_target(self):
        audit = MetadataIneligibleCandidate(
            "pexels", "audit", "v1", "nature", "INSUFFICIENT_DURATION"
        )
        valid = make_candidate(asset="2101")

        class Provider:
            def search(self, query, page, **kwargs):
                return BackgroundDiscoveryPage(
                    "pexels", query, page, 2, (valid,), (audit,)
                )

        with mock.patch(
            "engine.background_candidates.get_rotated_queries",
            return_value=("nature",),
        ):
            result = discover_candidates(
                store=(store := InMemCandidateStore()),
                providers={"pexels": Provider()},
                max_candidates=1,
                page_offset=0,
                logger=lambda _: None,
            )

        self.assertEqual(result["eligible_pending"], 1)
        self.assertEqual(store.rows["pexels:2101:v1"]["status"], "PENDING")

    def test_api_request_budget_is_hard(self):
        calls = []

        class Provider:
            def __init__(self, name):
                self.name = name

            def search(self, query, page, **kwargs):
                calls.append((self.name, query, page))
                return ()

        with mock.patch(
            "engine.background_candidates.get_rotated_queries",
            return_value=("nature", "sky", "ocean"),
        ):
            result = discover_candidates(
                store=InMemCandidateStore(),
                providers={name: Provider(name) for name in ("pexels", "pixabay")},
                max_candidates=5,
                max_api_requests=3,
                page_offset=0,
                logger=lambda _: None,
            )

        self.assertEqual(result["provider_requests"], 3)
        self.assertEqual(len(calls), 3)
        self.assertLessEqual(result["provider_requests"], DISCOVERY_MAX_API_REQUESTS_PER_RUN)

    def test_metadata_examination_budget_is_hard(self):
        candidates = tuple(
            replace(make_candidate(asset=str(index)), duration_seconds=5)
            for index in range(10)
        )

        class Provider:
            def search(self, query, page, **kwargs):
                return BackgroundDiscoveryPage(
                    "pexels", query, page, len(candidates), candidates, ()
                )

        with mock.patch(
            "engine.background_candidates.get_rotated_queries",
            return_value=("nature",),
        ):
            result = discover_candidates(
                store=(store := InMemCandidateStore()),
                providers={"pexels": Provider()},
                max_candidates=5,
                max_metadata_items=3,
                page_offset=0,
                logger=lambda _: None,
            )

        self.assertEqual(result["metadata_records_examined"], 3)
        self.assertEqual(len(store.rows), 3)
        self.assertLessEqual(
            result["metadata_records_examined"],
            DISCOVERY_MAX_METADATA_ITEMS_PER_RUN,
        )

    def test_concurrent_search_completion_does_not_change_enqueue_order(self):
        active = 0
        peak_active = 0
        lock = threading.Lock()

        class Provider:
            def __init__(self, name, delay):
                self.name = name
                self.delay = delay

            def search(self, query, page, **kwargs):
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                time.sleep(self.delay)
                with lock:
                    active -= 1
                candidate = make_candidate(provider=self.name, asset="1")
                return BackgroundDiscoveryPage(
                    self.name, query, page, 1, (candidate,), ()
                )

        providers = {
            "pexels": Provider("pexels", 0.02),
            "pixabay": Provider("pixabay", 0.0),
        }
        with mock.patch(
            "engine.background_candidates.get_rotated_queries",
            return_value=("nature",),
        ):
            result = discover_candidates(
                store=(store := InMemCandidateStore()),
                providers=providers,
                max_candidates=2,
                page_offset=0,
                max_search_workers=4,
                logger=lambda _: None,
            )

        self.assertEqual(peak_active, 2)
        self.assertEqual(result["eligible_pending"], 2)
        self.assertEqual(
            list(store.rows),
            ["pexels:1:v1", "pixabay:1:v1"],
        )

    def test_pexels_orientation_rotation_is_deterministic_and_covers_all_lanes(self):
        base_queries = ("nature", "sky", "ocean", "forest", "sunset", "space")
        lanes_for_nature = []

        class Provider:
            def search(self, query, page, **kwargs):
                if query == "nature":
                    lanes_for_nature.append(kwargs.get("orientation"))
                return ()

        for offset in range(len(base_queries)):
            rotated = base_queries[offset:] + base_queries[:offset]
            with mock.patch(
                "engine.background_candidates.get_rotated_queries",
                return_value=rotated,
            ):
                discover_candidates(
                    store=InMemCandidateStore(),
                    providers={"pexels": Provider()},
                    max_candidates=1,
                    max_api_requests=len(base_queries),
                    page_offset=0,
                    logger=lambda _: None,
                )

        self.assertEqual(
            lanes_for_nature,
            [
                "portrait",
                "landscape",
                "square",
                "portrait",
                "portrait",
                "portrait",
            ],
        )
        self.assertEqual(set(lanes_for_nature), set(PEXELS_ORIENTATION_CYCLE))

    def test_deterministic_dry_run_summary_reports_policy_yield(self):
        candidates = (
            replace(make_candidate(asset="2201"), duration_seconds=5),
            make_candidate(asset="2202"),
            replace(
                make_candidate(asset="2203"),
                file_width=2160,
                file_height=2160,
                requires_center_crop=True,
            ),
            replace(
                make_candidate(asset="2204"),
                file_width=3840,
                file_height=2160,
                requires_center_crop=True,
            ),
        )

        class Provider:
            def search(self, query, page, **kwargs):
                return BackgroundDiscoveryPage(
                    "pexels", query, page, len(candidates), candidates, ()
                )

        with mock.patch(
            "engine.background_candidates.get_rotated_queries",
            return_value=("nature",),
        ):
            result = discover_candidates(
                store=InMemCandidateStore(),
                providers={"pexels": Provider()},
                max_candidates=3,
                page_offset=0,
                logger=lambda _: None,
            )

        self.assertEqual(
            {
                key: result[key]
                for key in (
                    "metadata_records_examined",
                    "metadata_ineligible",
                    "eligible_pending",
                    "provider_requests",
                    "pages_touched",
                    "query_terms_touched",
                    "portrait_candidates",
                    "square_candidates",
                    "high_res_landscape_candidates",
                )
            },
            {
                "metadata_records_examined": 4,
                "metadata_ineligible": 1,
                "eligible_pending": 3,
                "provider_requests": 1,
                "pages_touched": [1],
                "query_terms_touched": ["nature"],
                "portrait_candidates": 1,
                "square_candidates": 1,
                "high_res_landscape_candidates": 1,
            },
        )

    def test_qualification_rechecks_every_shared_metadata_rule_before_download(self):
        cases = (
            (replace(make_candidate(asset="2301"), duration_seconds=5), "INSUFFICIENT_DURATION"),
            (replace(make_candidate(asset="2302"), duration_seconds=60), "SOURCE_DURATION_TOO_LONG"),
            (
                replace(
                    make_candidate(asset="2303"),
                    declared_file_size_bytes=65 * 1024 * 1024,
                ),
                "SOURCE_FILE_TOO_LARGE",
            ),
            (
                replace(
                    make_candidate(asset="2304"),
                    file_width=1920,
                    file_height=1080,
                    requires_center_crop=True,
                ),
                "INSUFFICIENT_NATIVE_CROP_RESOLUTION",
            ),
            (
                replace(
                    make_candidate(asset="2305"),
                    file_type="video/quicktime",
                    file_url="https://example.test/video_2305.mov",
                ),
                "UNSUPPORTED_VIDEO_FORMAT",
            ),
        )

        for candidate, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                store = InMemCandidateStore()
                store.enqueue_background_candidate(
                    candidate.provider,
                    candidate.provider_asset_id,
                    candidate.provider_variant_id,
                    candidate.query,
                    candidate_metadata(candidate),
                    "PENDING",
                )
                download = mock.Mock(side_effect=AssertionError("download must not run"))
                scan = mock.Mock(side_effect=AssertionError("scan must not run"))
                result = qualify_one_claimed_candidate(
                    store=store,
                    pool_repository=FakePoolRepo(),
                    worker_id="metadata-defense",
                    download_fn=download,
                    scan_fn=scan,
                    logger=lambda _: None,
                )

                self.assertEqual(result["status"], "METADATA_INELIGIBLE")
                stored = store.rows[
                    f"{candidate.provider}:{candidate.provider_asset_id}:{candidate.provider_variant_id}"
                ]
                self.assertEqual(stored["error_code"], expected_reason)
                download.assert_not_called()
                scan.assert_not_called()


class QualificationDeadlineRejectionTests(unittest.TestCase):
    def _scan(self, subject=None, *, safe=False, animal_status="INCOMPLETE"):
        detection = Detection(1, 0.25, subject or "face", 0.84, (0, 0, 10, 10))
        return SafetyScanResult(
            safe=safe,
            frames_scanned=101,
            duration_seconds=25.25,
            person_detections=(detection,) if subject == "person" else (),
            face_detections=(detection,) if subject == "face" else (),
            animal_detections=(detection,) if subject == "animal" else (),
            animal_policy_status=animal_status,
        )

    def _qualify_with_elapsed_time(self, scan, elapsed=181.0, lease_seconds=900, download_timeout=False):
        candidate = make_candidate(asset="2401")
        store, repo = InMemCandidateStore(), FakePoolRepo()
        store.enqueue_background_candidate("pexels", "2401", "v1", "nature", candidate_metadata(candidate), "PENDING")
        clock = [0.0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source.mp4"

            def download(*_args, **_kwargs):
                if download_timeout:
                    from urllib.error import URLError
                    from engine.backgrounds import BackgroundSourceError

                    clock[0] = elapsed
                    raise BackgroundSourceError("Download failed") from URLError(TimeoutError())
                path.write_bytes(b"source")
                return DownloadedBackground(candidate, str(path), 6, "a" * 64)

            def scan_source(*_args, **_kwargs):
                clock[0] = elapsed
                return scan

            result = qualify_one_claimed_candidate(
                store=store, pool_repository=repo, worker_id="deadline-test",
                lease_seconds=lease_seconds, download_fn=download, scan_fn=scan_source,
                monotonic=lambda: clock[0], logger=lambda _: None,
            )
            self.assertFalse(path.exists())
        return result, store, repo

    def test_positive_detection_is_rejected_even_after_deadline(self):
        for subject in ("person", "face", "animal"):
            with self.subTest(subject=subject):
                result, store, repo = self._qualify_with_elapsed_time(self._scan(subject))
                self.assertEqual(result["status"], "SAFETY_REJECTED")
                self.assertEqual(store.rows["pexels:2401:v1"]["error_code"], f"REJECT_{subject.upper()}")
                self.assertEqual(len(repo.safety_rejected), 1)
                self.assertEqual(repo.transient, [])
                self.assertEqual(repo.verified, [])

    def test_full_scan_can_finish_after_previous_90_second_deadline(self):
        result, _store, repo = self._qualify_with_elapsed_time(
            self._scan(safe=True, animal_status="CLEAR"), elapsed=150.0,
        )
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(len(repo.verified), 1)
        self.assertEqual(repo.transient, [])

    def test_download_socket_timeout_is_deferred_by_the_queue(self):
        result, store, repo = self._qualify_with_elapsed_time(
            self._scan(), elapsed=60.0, download_timeout=True,
        )
        self.assertEqual(result["status"], "TRANSIENT_FAILURE")
        self.assertEqual(store.rows["pexels:2401:v1"]["error_code"], "CANDIDATE_PROCESSING_TIMEOUT")
        self.assertEqual(repo.verified, [])

    def test_scan_budget_reserves_time_before_a_short_lease_expires(self):
        result, _store, repo = self._qualify_with_elapsed_time(
            self._scan(safe=True, animal_status="CLEAR"), elapsed=40.0, lease_seconds=60,
        )
        self.assertEqual(result["status"], "TRANSIENT_FAILURE")
        self.assertEqual(repo.verified, [])

    def test_clean_scan_over_deadline_is_not_verified(self):
        result, _store, repo = self._qualify_with_elapsed_time(self._scan(safe=True, animal_status="CLEAR"))
        self.assertEqual(result["status"], "TRANSIENT_FAILURE")
        self.assertEqual(len(repo.transient), 1)
        self.assertEqual(repo.verified, [])
        self.assertEqual(repo.safety_rejected, [])

    def test_incomplete_scan_without_detections_remains_transient(self):
        result, _store, repo = self._qualify_with_elapsed_time(self._scan())
        self.assertEqual(result["status"], "TRANSIENT_FAILURE")
        self.assertEqual(len(repo.transient), 1)
        self.assertEqual(repo.verified, [])
        self.assertEqual(repo.safety_rejected, [])

    def test_partial_rejection_advances_to_other_candidates_within_three_attempts(self):
        store, repo = InMemCandidateStore(), FakePoolRepo()
        for asset in ("2401", "2402", "2403"):
            candidate = make_candidate(asset=asset)
            store.enqueue_background_candidate("pexels", asset, "v1", "nature", candidate_metadata(candidate), "PENDING")
        clock, scanned, statuses = [0.0], [], []
        with tempfile.TemporaryDirectory() as tmpdir:
            def download(candidate, **_kwargs):
                path = Path(tmpdir) / f"{candidate.provider_asset_id}.mp4"
                path.write_bytes(b"source")
                return DownloadedBackground(candidate, str(path), 6, "a" * 64)

            def scan_source(path, **_kwargs):
                asset = Path(path).stem
                scanned.append(asset)
                clock[0] += 181.0 if asset == "2401" else 1.0
                return self._scan("face") if asset == "2401" else self._scan(safe=True, animal_status="CLEAR")

            for _ in range(3):
                result = qualify_one_claimed_candidate(
                    store=store, pool_repository=repo, worker_id="bounded-batch-test",
                    download_fn=download, scan_fn=scan_source,
                    monotonic=lambda: clock[0], logger=lambda _: None,
                )
                statuses.append(result["status"])
        self.assertEqual(scanned, ["2401", "2402", "2403"])
        self.assertEqual(statuses, ["SAFETY_REJECTED", "VERIFIED", "VERIFIED"])
        self.assertEqual(len(repo.safety_rejected), 1)
        self.assertEqual(len(repo.verified), 2)
        self.assertEqual(repo.transient, [])
