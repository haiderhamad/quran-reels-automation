"""Regression for explicitly owner-reviewed contiguous Quran passages (no network/post)."""
from __future__ import annotations

from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from engine.calm_reels_cycle import FreshCycleError, FreshCycleSnapshot, choose_next_owner_ayah
from engine.config import DurationConfig, VerseSelectionLimits
from engine.main import GenerationError, RunRequest
from engine.owner_reviewed_passages import ReviewedPassageApproval
from engine.verses import AyahTiming, VerseSelectionError, select_owner_v1_ayah_candidate


FATIHA_KEYS = ("1:3", "1:4", "1:5", "1:6", "1:7")
SYNTHETIC_REVIEW = ReviewedPassageApproval(
    "1:3", "1:7", "private-owner-synthetic-mock-review", "2000-01-01"
)
TEST_REGISTRY = (SYNTHETIC_REVIEW.ayah_keys,)
TIMINGS = ((3, 0.0, 4.0), (4, 4.0, 9.0), (5, 9.0, 16.0),
           (6, 16.0, 25.0), (7, 25.0, 37.5))


def fatiha_timings():
    return [AyahTiming(1, ayah, start, end, f"arabic-{ayah}", f"english-{ayah}")
            for ayah, start, end in TIMINGS]


class ReviewedFatihaPassageTests(TestCase):
    def test_clean_install_requires_operator_review_for_extended_passage(self):
        cycle = FreshCycleSnapshot(1, "2000-01-01T00:00:00Z", frozenset(("1:1",)))
        self.assertEqual(choose_next_owner_ayah(cycle).reviewed_ayah_keys, ())
        with self.assertRaises(GenerationError):
            RunRequest(slot="morning", owner_ayah_key="1:3", owner_cycle_number=1,
                       owner_reviewed_ayah_keys=FATIHA_KEYS)

    def test_cycle_after_confirmed_basmala_targets_exact_reviewed_five(self):
        cycle = FreshCycleSnapshot(1, "2026-11-01T00:00:00Z", frozenset(("1:1",)))
        decision = choose_next_owner_ayah(cycle, reviewed_passages=TEST_REGISTRY)
        self.assertEqual(decision.next_ayah_key, "1:3")
        self.assertEqual(decision.reviewed_ayah_keys, FATIHA_KEYS)
        self.assertEqual(decision.confirmed_count, 1)
        request = RunRequest(slot="morning", owner_ayah_key="1:3",
                             owner_cycle_number=1, owner_reviewed_ayah_keys=FATIHA_KEYS,
                             owner_reviewed_approval=SYNTHETIC_REVIEW, owner_reviewed_registry=TEST_REGISTRY)
        self.assertEqual(request.owner_reviewed_ayah_keys, FATIHA_KEYS)

    def test_exact_five_are_selected_with_authentic_duration_and_every_source_checked(self):
        with patch("engine.calm_reels_cycle.require_owner_ayah_source", return_value="trusted") as verify:
            candidate = select_owner_v1_ayah_candidate(
                fatiha_timings(), "1:3", reviewed_ayah_keys=FATIHA_KEYS, reviewed_registry=TEST_REGISTRY,
                duration_config=DurationConfig(10, 60), limits=VerseSelectionLimits(maximum_ayahs=None),
            )
        self.assertEqual(tuple(f"1:{row.ayah}" for row in candidate.records), FATIHA_KEYS)
        self.assertAlmostEqual(candidate.duration_seconds, 37.5)
        self.assertEqual([call.args[0] for call in verify.call_args_list], list(FATIHA_KEYS))

    def test_interior_missing_duplicate_overlapping_or_source_mismatch_fails_closed(self):
        base = fatiha_timings()
        with patch("engine.calm_reels_cycle.require_owner_ayah_source", return_value="trusted"):
            for records in (
                base[:2] + base[3:],
                base + [base[2]],
                base[:2] + [replace(base[2], start_time=8.0)] + base[3:],
            ):
                with self.subTest(records=records):
                    with self.assertRaises(VerseSelectionError):
                        select_owner_v1_ayah_candidate(
                            records, "1:3", reviewed_ayah_keys=FATIHA_KEYS, reviewed_registry=TEST_REGISTRY,
                            duration_config=DurationConfig(10, 60), limits=VerseSelectionLimits())
        with patch("engine.calm_reels_cycle.require_owner_ayah_source", side_effect=FreshCycleError("SOURCE_CHANGED")):
            with self.assertRaises(VerseSelectionError):
                select_owner_v1_ayah_candidate(
                    base, "1:3", reviewed_ayah_keys=FATIHA_KEYS, reviewed_registry=TEST_REGISTRY,
                    duration_config=DurationConfig(10, 60), limits=VerseSelectionLimits())

    def test_excess_duration_and_limits_never_shorten_exact_reviewed_passage(self):
        with patch("engine.calm_reels_cycle.require_owner_ayah_source", return_value="trusted"):
            for duration, limits in ((DurationConfig(10, 30), VerseSelectionLimits()),
                                     (DurationConfig(10, 60), VerseSelectionLimits(maximum_ayahs=2))):
                with self.subTest(duration=duration, limits=limits):
                    with self.assertRaises(VerseSelectionError):
                        select_owner_v1_ayah_candidate(
                            fatiha_timings(), "1:3", reviewed_ayah_keys=FATIHA_KEYS, reviewed_registry=TEST_REGISTRY,
                            duration_config=duration, limits=limits)

    def test_partial_prior_receipt_cannot_publish_remaining_fragment_as_reviewed(self):
        cycle = FreshCycleSnapshot(1, "2026-11-01T00:00:00Z", frozenset(("1:1", "1:5")))
        with self.assertRaises(FreshCycleError):
            choose_next_owner_ayah(cycle, reviewed_passages=TEST_REGISTRY)

    def test_caller_cannot_fabricate_reviewed_range_or_enable_test_mode(self):
        for params in (
            {"owner_reviewed_ayah_keys": ("1:3", "1:4", "1:5")},
            {"owner_reviewed_ayah_keys": ("1:3", "1:5", "1:6", "1:7")},
            {"owner_reviewed_ayah_keys": ("1:3", "1:4", "1:5", "1:6", "2:7")},
        ):
            with self.subTest(params=params):
                with self.assertRaises(GenerationError):
                    RunRequest(slot="morning", owner_ayah_key="1:3", owner_cycle_number=1, **params)


class ReviewedPassageDispatchTests(TestCase):
    def make_record(self):
        import hashlib
        from engine.alignments import normalized_sha256
        from tests.test_calm_reels_dispatch import ARABIC, valid_record
        base = valid_record()
        owner = base.provenance["owner_v1"]
        src = hashlib.sha256(ARABIC.encode()).hexdigest()
        p = base.provenance
        approval = SYNTHETIC_REVIEW
        return replace(base, first_ayah=3, last_ayah=7, provenance={
            **p,
            "owner_v1": {
                **owner, "ayah_key": "1:3", "ayah_keys": list(FATIHA_KEYS),
                "source_sha256_by_key": dict.fromkeys(FATIHA_KEYS, src),
                "review_approval": approval.snapshot(), "review_approval_sha256": approval.sha256,
            },
            "caption_ayahs": [dict(p["caption_ayahs"][0], verse_key=key) for key in FATIHA_KEYS],
            "subtitle_ayahs": [dict(p["subtitle_ayahs"][0], verse_key=key) for key in FATIHA_KEYS],
        })

    def test_five_source_verified_ayahs_make_one_exclusive_passage_reservation(self):
        import hashlib
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from engine.alignments import normalized_sha256
        from engine.calm_reels_dispatch import (
            claim_owner_v1_before_facebook_dispatch, require_owner_v1_dispatch_record)
        from tests.test_calm_reels_dispatch import ARABIC, TRANSLATION
        record = self.make_record()
        state = MagicMock()
        state.claim_calm_reels_v1_reviewed_passage.return_value = {
            "state": "CLAIMED", "cycle_number": 1,
            "ayah_key": "1:3", "last_ayah_key": "1:7",
        }
        with (patch("engine.calm_reels_dispatch.require_owner_ayah_source",
                    return_value=hashlib.sha256(ARABIC.encode()).hexdigest()) as source,
              patch("engine.reel_publication_policy._trusted_source_hashes",
                    return_value=(normalized_sha256(ARABIC), normalized_sha256(TRANSLATION)))):
            self.assertEqual(require_owner_v1_dispatch_record(record), (1, "1:3"))
            claim = claim_owner_v1_before_facebook_dispatch(
                record, store=state, stored_config=SimpleNamespace(reciter_reuse_confirmed=True),
                env={"RECITER_REUSE_CONFIRMED": "true"})
        self.assertEqual(claim, {"state": "CLAIMED", "cycle_number": 1, "ayah_key": "1:3"})
        self.assertEqual([call.args[0] for call in source.call_args_list], list(FATIHA_KEYS) * 2)
        state.claim_calm_reels_v1_reviewed_passage.assert_called_once_with(
            cycle_number=1, first_ayah_key="1:3", last_ayah_key="1:7", run_id=record.run_id,
            review_approval=ReviewedPassageApproval.from_snapshot(record.provenance["owner_v1"]["review_approval"]))
        state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_replaced_review_boundary_is_not_vetoed_by_historical_fatiha_tuple(self):
        """Source validation must not supersede a later exact Supabase approval."""
        import hashlib
        from engine.alignments import normalized_sha256
        from engine.calm_reels_dispatch import require_owner_v1_dispatch_record
        from tests.test_calm_reels_dispatch import ARABIC, TRANSLATION

        record = self.make_record()
        keys = FATIHA_KEYS[:3]  # Synthetic registry replacement, NOT owner approval.
        provenance = record.provenance
        owner = dict(provenance["owner_v1"])
        owner["ayah_keys"] = list(keys)
        owner["source_sha256_by_key"] = {
            key: owner["source_sha256_by_key"][key] for key in keys
        }
        approval = ReviewedPassageApproval("1:3", "1:5", "private-owner-synthetic-replacement", "2026-11-02")
        owner.update(review_approval=approval.snapshot(), review_approval_sha256=approval.sha256)
        trimmed = replace(record, last_ayah=5, provenance={
            **provenance,
            "owner_v1": owner,
            "caption_ayahs": provenance["caption_ayahs"][:3],
            "subtitle_ayahs": provenance["subtitle_ayahs"][:3],
        })
        with (
            patch("engine.calm_reels_dispatch.require_owner_ayah_source",
                  return_value=hashlib.sha256(ARABIC.encode()).hexdigest()),
            patch("engine.reel_publication_policy._trusted_source_hashes",
                  return_value=(normalized_sha256(ARABIC), normalized_sha256(TRANSLATION))),
        ):
            self.assertEqual(require_owner_v1_dispatch_record(trimmed), (1, "1:3"))

    def test_repeated_same_run_claim_cannot_send_again(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from engine.calm_reels_dispatch import claim_owner_v1_before_facebook_dispatch, OwnerV1DispatchError
        state = MagicMock()
        state.claim_calm_reels_v1_reviewed_passage.return_value = {
            "state": "ALREADY_CLAIMED", "cycle_number": 1, "ayah_key": "1:3", "last_ayah_key": "1:7"}
        from tests.test_calm_reels_dispatch import ARABIC, TRANSLATION
        import hashlib
        from engine.alignments import normalized_sha256
        with (patch("engine.calm_reels_dispatch.require_owner_ayah_source",
                    return_value=hashlib.sha256(ARABIC.encode()).hexdigest()),
              patch("engine.reel_publication_policy._trusted_source_hashes",
                    return_value=(normalized_sha256(ARABIC), normalized_sha256(TRANSLATION)))):
            with self.assertRaisesRegex(OwnerV1DispatchError, "NO_REDISPATCH"):
                claim_owner_v1_before_facebook_dispatch(
                    self.make_record(), store=state,
                    stored_config=SimpleNamespace(reciter_reuse_confirmed=True),
                    env={"RECITER_REUSE_CONFIRMED": "true"})
