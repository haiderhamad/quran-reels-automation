"""Crash-safe Buffer publication and validation-draft lifecycle tests."""

from __future__ import annotations

import threading
import unittest

from engine.buffer_lifecycle import (
    BufferDraftCoordinator,
    BufferPublicationCoordinator,
    DRAFT_CLEANUP_PENDING,
    DRAFT_CREATED,
    DRAFT_DELETED,
    PUBLICATION_DISPATCHING,
    PUBLICATION_SUCCESS,
    PUBLICATION_UNKNOWN,
    ReconciliationResult,
    publication_fingerprint,
    publication_source,
)


class MemoryStore:
    def __init__(self) -> None:
        self.publications = {}
        self.drafts = {}
        self.lock = threading.Lock()

    @staticmethod
    def _target(run_id, platform, publisher_backend, channel_id):
        return run_id, platform, publisher_backend, channel_id

    def get_buffer_publication_claim(self, run_id, platform, publisher_backend, channel_id):
        return self.publications.get(self._target(run_id, platform, publisher_backend, channel_id))
    def claim_buffer_publication_dispatch(self, claim):
        target = self._target(claim["run_id"], claim["platform"], claim["publisher_backend"], claim["channel_id"])
        with self.lock:
            existing = self.publications.get(target)
            if existing: return {**existing, "_claim_created": False}
            row = {**claim, "value": dict(claim), "_claim_created": True}; self.publications[target] = row; return row
    def transition_buffer_publication_claim(self, run_id, platform, publisher_backend, channel_id, expected, state, updates):
        with self.lock:
            row = self.publications.get(self._target(run_id, platform, publisher_backend, channel_id))
            if not row or row["state"] not in expected: return None
            row.update(updates); row["state"] = state; row["value"].update(updates); row["value"]["state"] = state
            return row
    def get_buffer_draft_validation(self, validation_id): return self.drafts.get(validation_id)
    def claim_buffer_draft_validation(self, draft):
        existing = self.drafts.get(draft["validation_id"])
        if existing: return {**existing, "_claim_created": False}
        row = {**draft, "value": dict(draft), "_claim_created": True}; self.drafts[draft["validation_id"]] = row; return row
    def transition_buffer_draft_validation(self, validation_id, expected, state, updates):
        row = self.drafts.get(validation_id)
        if not row or row["state"] not in expected: return None
        row.update(updates); row["state"] = state; row["value"].update(updates); row["value"]["state"] = state
        return row


class TestBufferPublicationLifecycle(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(); self.coordinator = BufferPublicationCoordinator(self.store, platform="facebook", publisher_backend="buffer", channel_id="channel_A")
        self.fingerprint = publication_fingerprint(run_id="run_1", platform="facebook", channel_id="channel_A", media_identity="sha", caption="caption")
        self.claim = self.coordinator.claim(run_id="run_1", fingerprint=self.fingerprint,
            source=publication_source("run_1", self.fingerprint))

    @property
    def target(self):
        return ("run_1", "facebook", "buffer", "channel_A")

    def test_success_checkpoint_and_rerun_make_one_write(self):
        self.coordinator.mark_success("run_1", post_id="post_1", external_link=None, accepted_at="now")
        same = self.coordinator.claim(run_id="run_1", fingerprint=self.fingerprint,
            source=publication_source("run_1", self.fingerprint))
        self.assertEqual(same["state"], PUBLICATION_SUCCESS)
        self.assertEqual(len(self.store.publications), 1)

    def test_two_workers_share_one_atomic_claim(self):
        other = BufferPublicationCoordinator(self.store, platform="facebook", publisher_backend="buffer", channel_id="channel_A").claim(run_id="run_1", fingerprint=self.fingerprint,
            source=publication_source("run_1", self.fingerprint))
        self.assertFalse(other["_claim_created"])
        self.assertEqual(other["dispatch_attempts"], 1)

    def test_ambiguous_timeout_never_blind_retries(self):
        self.coordinator.mark_unknown_outcome("run_1", "BUFFER_PUBLISH_OUTCOME_UNKNOWN")
        self.assertEqual(self.coordinator.recover(self.claim, ReconciliationResult("INDETERMINATE")), PUBLICATION_UNKNOWN)
        self.assertEqual(self.store.publications[self.target]["state"], PUBLICATION_UNKNOWN)

    def test_reconciliation_found_confirms_success_without_second_write(self):
        self.coordinator.mark_unknown_outcome("run_1", "timeout")
        self.assertEqual(self.coordinator.recover(self.claim, ReconciliationResult("FOUND", post_id="post_1")), PUBLICATION_SUCCESS)
        self.assertEqual(self.store.publications[self.target]["buffer_post_id"], "post_1")

    def test_only_proven_absence_allows_one_controlled_retry(self):
        self.coordinator.mark_unknown_outcome("run_1", "timeout")
        self.assertEqual(self.coordinator.recover(self.claim, ReconciliationResult("ABSENT")), PUBLICATION_DISPATCHING)
        self.assertEqual(self.store.publications[self.target]["dispatch_attempts"], 2)
        self.coordinator.mark_unknown_outcome("run_1", "timeout-again")
        self.assertEqual(self.coordinator.recover(self.store.publications[self.target], ReconciliationResult("ABSENT")), PUBLICATION_UNKNOWN)

    def test_fingerprint_is_stable_but_channel_specific(self):
        again = publication_fingerprint(run_id="run_1", platform="facebook", channel_id="channel_A", media_identity="sha", caption="caption")
        other = publication_fingerprint(run_id="run_1", platform="facebook", channel_id="channel_B", media_identity="sha", caption="caption")
        self.assertEqual(self.fingerprint, again); self.assertNotEqual(self.fingerprint, other)

    def test_same_target_with_changed_media_or_caption_fails_closed(self):
        changed = publication_fingerprint(
            run_id="run_1", platform="facebook", channel_id="channel_A",
            media_identity="different-sha", caption="changed caption",
        )
        with self.assertRaisesRegex(RuntimeError, "BUFFER_PUBLICATION_TARGET_FINGERPRINT_MISMATCH"):
            self.coordinator.claim(
                run_id="run_1", fingerprint=changed,
                source=publication_source("run_1", changed),
            )
        self.assertEqual(len(self.store.publications), 1)

    def test_success_checkpoint_retains_buffer_acceptance_status(self):
        self.coordinator.mark_success(
            "run_1", post_id="post_1", external_link=None, accepted_at="now",
            buffer_post_status="sending",
        )
        self.assertEqual(self.store.publications[self.target]["buffer_post_status"], "sending")

    def test_same_run_allows_independent_platform_and_channel_targets(self):
        instagram = BufferPublicationCoordinator(self.store, platform="instagram", publisher_backend="buffer", channel_id="ig_A")
        other_facebook = BufferPublicationCoordinator(self.store, platform="facebook", publisher_backend="buffer", channel_id="channel_B")
        ig_fingerprint = publication_fingerprint(run_id="run_1", platform="instagram", channel_id="ig_A", media_identity="sha", caption="caption")
        other_fingerprint = publication_fingerprint(run_id="run_1", platform="facebook", channel_id="channel_B", media_identity="sha", caption="caption")
        self.assertTrue(instagram.claim(run_id="run_1", fingerprint=ig_fingerprint, source=publication_source("run_1", ig_fingerprint))["_claim_created"])
        self.assertTrue(other_facebook.claim(run_id="run_1", fingerprint=other_fingerprint, source=publication_source("run_1", other_fingerprint))["_claim_created"])
        self.assertEqual(len(self.store.publications), 3)

    def test_concurrent_same_target_claims_create_exactly_one_owner(self):
        store = MemoryStore()
        barrier = threading.Barrier(2)
        results = []

        def worker() -> None:
            coordinator = BufferPublicationCoordinator(store, platform="facebook", publisher_backend="buffer", channel_id="channel_A")
            fingerprint = publication_fingerprint(run_id="run_1", platform="facebook", channel_id="channel_A", media_identity="sha", caption="caption")
            barrier.wait()
            results.append(coordinator.claim(run_id="run_1", fingerprint=fingerprint, source=publication_source("run_1", fingerprint)))

        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(bool(result["_claim_created"]) for result in results), 1)
        self.assertEqual(len(store.publications), 1)


class TestBufferDraftLifecycle(unittest.TestCase):
    def test_created_draft_can_be_deleted_and_never_targets_public_claim(self):
        store = MemoryStore(); coordinator = BufferDraftCoordinator(store)
        row = coordinator.claim({"validation_id": "draft_1", "run_id": "run_1", "channel_id": "channel_A", "buffer_source": "quran-reels-draft:run_1:x"})
        self.assertEqual(row["state"], "CREATING")
        coordinator.mark_created("draft_1", "draft_post_1")
        coordinator.mark_deleted("draft_1", "draft_post_1")
        self.assertEqual(store.drafts["draft_1"]["state"], DRAFT_DELETED)
        self.assertEqual(store.publications, {})

    def test_downstream_or_delete_failure_is_durably_cleanup_pending(self):
        store = MemoryStore(); coordinator = BufferDraftCoordinator(store)
        coordinator.claim({"validation_id": "draft_1", "run_id": "run_1", "channel_id": "channel_A", "buffer_source": "quran-reels-draft:run_1:x"})
        coordinator.mark_created("draft_1", "draft_post_1")
        coordinator.mark_cleanup_pending("draft_1", "draft_post_1", "BUFFER_TRANSIENT_ERROR")
        self.assertEqual(store.drafts["draft_1"]["state"], DRAFT_CLEANUP_PENDING)
        # Restart sees the stored ID and can finish cleanup without creating another draft.
        coordinator.mark_deleted("draft_1", "draft_post_1")
        self.assertEqual(store.drafts["draft_1"]["state"], DRAFT_DELETED)
