"""Regression coverage for automatic, evidence-bound production authorization."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from types import SimpleNamespace

from engine.config import DurationConfig, VerseSelectionLimits
from engine.production_authorization import (
    ProductionAuthorizationCatalog,
    ProductionAuthorizationError,
)
from engine.reel_publication_policy import require_record_eligible, require_publication_request
from engine.reel_theme_policy import EditorialPolicyError, evaluate_range
from engine.verses import AyahTiming, VerseSelectionError, select_verse_candidate
from tests.editorial_fixtures import eligible_record
from engine.alignments import normalized_sha256
from tests.editorial_fixtures import ARABIC, ENGLISH


def authorized_catalog(*, surah: int = 3, first: int = 8, last: int = 8,
                       candidates: list[str] | None = None) -> ProductionAuthorizationCatalog:
    keys = candidates or [f"{surah}:{first}"]
    return ProductionAuthorizationCatalog.from_rpc_rows(
        "calm-inviting-v1",
        [{
            "policy_version": "calm-inviting-v1",
            "surah_number": surah,
            "passage_first_ayah": first,
            "passage_last_ayah": last,
            "authorization_ids": [101],
            "approval_bases": ["SCREENING_ELIGIBLE"],
            "candidate_ayah_keys": keys,
            "authorization_fingerprint": "a" * 32,
            "source_evidence": {
                f"{surah}:{ayah}": {
                    "source_hash": normalized_sha256(ARABIC),
                    "translation_hash": normalized_sha256(ENGLISH),
                    "translation_language": "en",
                    "translation_version": "1.0.19",
                }
                for ayah in range(first, last + 1)
            },
        }],
    )


class ProductionAuthorizationCatalogTests(unittest.TestCase):
    def test_final_eligible_authorization_allows_only_the_exact_contextual_range(self):
        catalog = authorized_catalog(surah=3, first=8, last=9, candidates=["3:8"])

        self.assertTrue(evaluate_range(3, 8, 9, production_authorization=catalog).eligible)
        self.assertEqual(
            evaluate_range(3, 8, 8, production_authorization=catalog).reason_code,
            "EDITORIAL_NOT_PRODUCTION_AUTHORIZED",
        )
        self.assertEqual(
            evaluate_range(3, 9, 9, production_authorization=catalog).reason_code,
            "EDITORIAL_NOT_PRODUCTION_AUTHORIZED",
        )

    def test_dynamic_production_selection_never_falls_back_to_candidate_only_or_unreviewed_ayahs(self):
        catalog = authorized_catalog()
        approved = AyahTiming(3, 8, 0, 15, "عربي", "meaning")
        unreviewed = AyahTiming(3, 9, 15, 30, "عربي", "meaning")

        selected = select_verse_candidate(
            [approved],
            DurationConfig(15, 15),
            target_duration=15,
            production_authorization=catalog,
        )
        self.assertEqual((selected.surah, selected.first_ayah, selected.last_ayah), (3, 8, 8))

        with self.assertRaisesRegex(VerseSelectionError, "NO_ELIGIBLE_VERSE_RANGE"):
            select_verse_candidate(
                [unreviewed],
                DurationConfig(15, 15),
                target_duration=15,
                production_authorization=catalog,
            )

    def test_surah_prioritization_uses_fresh_exact_passages_without_bypassing_checks(self):
        import random
        from engine.main import _prioritize_authorized_surahs

        def row(surah, ayah, identity):
            return {
                "policy_version": "calm-inviting-v1",
                "surah_number": surah,
                "passage_first_ayah": ayah,
                "passage_last_ayah": ayah,
                "authorization_ids": [identity],
                "approval_bases": ["SCREENING_ELIGIBLE"],
                "candidate_ayah_keys": [f"{surah}:{ayah}"],
                "authorization_fingerprint": format(identity, "032x"),
                "source_evidence": {f"{surah}:{ayah}": {
                    "source_hash": normalized_sha256(ARABIC),
                    "translation_hash": normalized_sha256(ENGLISH),
                    "translation_language": "en",
                    "translation_version": "1.0.19",
                }},
            }

        catalog = ProductionAuthorizationCatalog.from_rpc_rows(
            "calm-inviting-v1", [row(3, 8, 101), row(3, 9, 102), row(4, 1, 103), row(5, 1, 104)],
        )
        quran = MagicMock()
        quran.get_ayah.return_value = SimpleNamespace(arabic_text=ARABIC)
        translation = MagicMock()
        translation.get_ayah.return_value = SimpleNamespace(translation_text=ENGLISH)
        source_kwargs = {
            "quran_data": quran,
            "translations_by_language": {"en": translation},
            "translation_languages": ("en",),
            "limits": VerseSelectionLimits(),
        }
        self.assertEqual(
            _prioritize_authorized_surahs(
                [5, 4, 3], catalog, ("4:1",), ((5, 1, 1),), random.Random(3),
                **source_kwargs,
            ),
            [3],
        )
        self.assertEqual(
            _prioritize_authorized_surahs(
                [4, 5], catalog, ("4:1",), ((5, 1, 1),), random.Random(3),
                **source_kwargs,
            ),
            [],
        )

        # Text bounds and missing authentic source data are hard filters.
        quran.get_ayah.return_value = SimpleNamespace(arabic_text="ا" * 221)
        self.assertEqual(_prioritize_authorized_surahs(
            [3], catalog, (), (), random.Random(3), **source_kwargs,
        ), [])
        quran.get_ayah.return_value = SimpleNamespace(arabic_text=ARABIC)
        translation.get_ayah.return_value = SimpleNamespace(translation_text="a" * 321)
        self.assertEqual(_prioritize_authorized_surahs(
            [3], catalog, (), (), random.Random(3), **source_kwargs,
        ), [])
        translation.get_ayah.side_effect = KeyError("missing verified translation")
        self.assertEqual(_prioritize_authorized_surahs(
            [3], catalog, (), (), random.Random(3), **source_kwargs,
        ), [])

        # A server-authorized two-ayah range cannot bypass a one-ayah cap.
        translation.get_ayah.side_effect = None
        translation.get_ayah.return_value = SimpleNamespace(translation_text=ENGLISH)
        pair = authorized_catalog(surah=3, first=8, last=9, candidates=["3:8"])
        self.assertEqual(_prioritize_authorized_surahs(
            [3], pair, (), (), random.Random(3),
            **{**source_kwargs, "limits": VerseSelectionLimits(maximum_ayahs=1)},
        ), [])
        # The fixture repeats a 112-character Arabic ayah twice (224
        # characters in total), exceeding the default 220-character cap.
        pair_arabic_characters = 2 * len(ARABIC.strip())
        self.assertGreater(
            pair_arabic_characters, VerseSelectionLimits().maximum_arabic_characters,
        )
        self.assertEqual(_prioritize_authorized_surahs(
            [3], pair, (), (), random.Random(3), **source_kwargs,
        ), [])
        self.assertEqual(_prioritize_authorized_surahs(
            [3], pair, (), (), random.Random(3),
            **{
                **source_kwargs,
                "limits": VerseSelectionLimits(
                    maximum_arabic_characters=pair_arabic_characters,
                ),
            },
        ), [3])

    def test_malformed_or_noncanonical_authorization_payload_fails_closed(self):
        bad_row = {
            "policy_version": "calm-inviting-v1",
            "surah_number": 3,
            "passage_first_ayah": 8,
            "passage_last_ayah": 8,
            "authorization_ids": [101],
            "approval_bases": ["SCREENING_ELIGIBLE"],
            "candidate_ayah_keys": ["3:08"],
            "authorization_fingerprint": "a" * 32,
        }
        with self.assertRaisesRegex(ProductionAuthorizationError, "CANDIDATE_INVALID"):
            ProductionAuthorizationCatalog.from_rpc_rows("calm-inviting-v1", [bad_row])

        with self.assertRaisesRegex(ProductionAuthorizationError, "RESPONSE_INVALID"):
            ProductionAuthorizationCatalog.from_rpc_rows("calm-inviting-v1", [bad_row, "not a row"])  # type: ignore[list-item]

    def test_publication_requires_the_exact_server_snapshot(self):
        catalog = authorized_catalog(surah=2, first=21, last=21, candidates=["2:21"])
        record = eligible_record()
        record.run_kind = "manual_production"
        record.provenance["production_authorization"] = catalog.require_range(2, 21, 21).snapshot()

        require_record_eligible(record, production_authorization=catalog)

        stale = copy.deepcopy(record)
        stale.provenance["production_authorization"]["authorization_fingerprint"] = "b" * 32
        with self.assertRaisesRegex(EditorialPolicyError, "PRODUCTION_AUTHORIZATION_PROVENANCE_STALE"):
            require_record_eligible(stale, production_authorization=catalog)

    def test_importing_unittest_never_bypasses_missing_live_authorization(self):
        with patch("engine.supabase_state.SupabaseConfig.from_env", side_effect=ValueError("unavailable")):
            with self.assertRaisesRegex(EditorialPolicyError, "PRODUCTION_AUTHORIZATION_UNAVAILABLE"):
                require_record_eligible(eligible_record())

    def test_transport_rejects_missing_test_and_unknown_run_purposes(self):
        for kind in (None, "test", "unknown"):
            record = eligible_record()
            record.run_kind = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(EditorialPolicyError, "production run kind required"):
                require_publication_request(record, "caption", backend="buffer")


class ProductionAuthorizationMigrationContractTests(unittest.TestCase):
    def test_automatic_authorization_requires_final_dual_pass_evidence(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / "supabase/migrations/20261015090000_automatic_editorial_production_authorization.sql").read_text(encoding="utf-8")
        hardening = (root / "supabase/migrations/20261015091500_production_authorization_audit_immutability.sql").read_text(encoding="utf-8")

        self.assertIn("new.screening_status <> 'SCREENED'", migration)
        self.assertIn("new.semantic_assessment <> 'ELIGIBLE_CANDIDATE'", migration)
        self.assertIn("assessment.pass_count = 2", migration)
        self.assertIn("assessment.distinct_prompt_count = 2", migration)
        self.assertIn("after insert or update", hardening)
        self.assertIn("Production authorization events are immutable", hardening)
