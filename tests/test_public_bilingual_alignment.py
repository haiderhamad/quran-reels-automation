"""Public reviewed panel metadata reconstructs operator-fetched text without bundling it."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.alignments import AlignmentValidationError, load_alignment_record, normalized_sha256
from engine.renderer import (
    RenderAyah, RenderConfig, RenderError, RenderTranslation, resolve_bilingual_alignment_preview,
    resolve_render_ayah_subtitle_plan,
)
from engine.reel_publication_policy import require_record_eligible
from engine.reel_theme_policy import EditorialPolicyError
from tests.editorial_fixtures import eligible_record, mock_live_authorization

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "data/alignments"
REL = Path("002/035.json")


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


class PublicBilingualTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.local = Path(temp.name)
        self.english_path = self.local / "en" / REL
        self.german_path = self.local / "de" / REL
        self.english_path.parent.mkdir(parents=True)
        self.german_path.parent.mkdir(parents=True)
        self.arabic = ("صندوق", "أزرق", "كبير")
        self.english = ("a", "blue", "box")
        self.german = ("eine", "blaue", "Kiste")
        self.ayah = RenderAyah(2, 35, " ".join(self.arabic), " ".join(self.english), "", 0, 18560,
            (RenderTranslation("en", " ".join(self.english), "", "english_rwwad", "1.0.19"),
             RenderTranslation("de", " ".join(self.german), "", "german_rwwad", "1.0.15")))
        self.manifest = {
            "schema_version": 2, "data_version": "english_rwwad-v1", "verse_key": "2:35",
            "arabic": {"source": "quran_foundation_text_uthmani", "normalized_sha256": digest(self.ayah.arabic_text)},
            "english": {"translation_key": "english_rwwad", "translation_version": "1.0.19",
                        "normalized_sha256": digest(self.ayah.english_text)},
            "tokenization_version": "whitespace_v1", "timing_strategy": "arabic_base_letter_weight_v1",
            "status": "verified", "reviewed_at": "2026-11-19", "review_note": "Synthetic fixture",
            "panels": [
                {"panel_index": i, "arabic_word_start": i, "arabic_word_end": i + 1,
                 "english_token_start": i, "english_token_end": i + 1,
                 "arabic_sha256": digest(ar), "english_sha256": digest(en)}
                for i, (ar, en) in enumerate(zip(self.arabic, self.english, strict=True))],
        }
        self.english_path.write_text(json.dumps(self.manifest))
        self.companion = {**self.manifest, "data_version": "english_german_rwwad-v1",
            "english_alignment_sha256": sha256(self.english_path.read_bytes()).hexdigest(),
            "german": {"translation_key": "german_rwwad", "translation_version": "1.0.15",
                       "source_url": "https://quranenc.com/downloads/csv/german_rwwad_v1.0.15-csv.1.csv",
                       "source_file_sha256": "d34e08397e93b39e2441c2e41c96201ad0aefa88a1ac9fdbade37d329bb44f9f",
                       "footnotes_sha256": digest(""), "normalized_sha256": digest(" ".join(self.german))},
            "panels": [{"panel_index": i, "german_token_start": i, "german_token_end": i + 1,
                        "german_sha256": digest(de)} for i, de in enumerate(self.german)]}

    def resolve(self, record):
        self.german_path.write_text(json.dumps(record))
        return resolve_render_ayah_subtitle_plan(self.ayah, RenderConfig(), alignment_root=self.local / "en",
                                                 bilingual_alignment_root=self.local / "de")

    def test_rebuilds_reviewed_segments_and_provenance_from_sources(self):
        plan = self.resolve(self.companion)
        self.assertEqual(plan.subtitle_mode, "VERIFIED_PHRASE_PROGRESSIVE")
        self.assertEqual(plan.subtitle_segments, tuple((ar, (("en", en), ("de", de))) for ar, en, de
            in zip(self.arabic, self.english, self.german, strict=True)))
        self.assertEqual(plan.subtitle_provenance["german_source_hash"], normalized_sha256(" ".join(self.german)))
        self.assertEqual(plan.subtitle_provenance["alignment_schema_version"], 2)

    def test_rejects_unreviewed_changed_or_incomplete_companions(self):
        for mutate in (
            lambda r: r.update(status="unverified"), lambda r: r.update(english_alignment_sha256="0"*64),
            lambda r: r["german"].update(translation_version="1.0.16"),
            lambda r: r["german"].update(footnotes_sha256=digest("new")),
            lambda r: r["panels"][1].update(german_token_start=0),
            lambda r: r["panels"][0].update(german_sha256=digest("other")),
            lambda r: r["panels"].pop(),
        ):
            record = deepcopy(self.companion)
            mutate(record)
            with self.subTest(record=record), self.assertRaises(AlignmentValidationError):
                self.resolve(record)

    def test_candidate_root_is_only_accepted_by_explicit_preview(self):
        self.companion.update(status="unverified", data_version="english_german_rwwad-candidate-v1")
        del self.companion["reviewed_at"]
        with self.assertRaises(AlignmentValidationError):
            self.resolve(self.companion)
        kwargs = {"candidate_root": self.local / "de", "english_root": self.local / "en"}
        with self.assertRaisesRegex(RenderError, "UNVERIFIED_ALIGNMENT_PREVIEW_FORBIDDEN"):
            resolve_bilingual_alignment_preview(self.ayah, RenderConfig(), **kwargs)
        config = replace(RenderConfig(), allow_unverified_alignment_preview=True)
        plan = resolve_bilingual_alignment_preview(self.ayah, config, **kwargs)
        self.assertEqual(plan.subtitle_mode, "UNVERIFIED_ALIGNMENT_PREVIEW")
        self.assertEqual(plan.subtitle_provenance["alignment_status"], "unverified")

    def test_rejects_wrong_slices_in_english_parent_even_when_parent_digest_matches(self):
        self.manifest["panels"][0]["english_sha256"] = digest("wrong")
        self.english_path.write_text(json.dumps(self.manifest))
        self.companion["english_alignment_sha256"] = sha256(self.english_path.read_bytes()).hexdigest()
        with self.assertRaises(AlignmentValidationError):
            self.resolve(self.companion)

    def test_public_approval_ranges_and_sources_bind_only_the_reviewed_split(self):
        parent = PUBLIC / "english_rwwad/v1" / REL
        companion = PUBLIC / "english_german_rwwad/v1" / REL
        en, de = [json.loads(path.read_bytes()) for path in (parent, companion)]
        self.assertEqual(de["english_alignment_sha256"], sha256(parent.read_bytes()).hexdigest())
        self.assertEqual(de["german"]["translation_version"], "1.0.15")
        self.assertEqual(en["english"]["translation_version"], "1.0.19")
        self.assertEqual([p["arabic_word_end"] for p in en["panels"]], [6, 11, 18])
        self.assertEqual([p["english_token_end"] for p in en["panels"]], [11, 18, 32])
        self.assertEqual([p["german_token_end"] for p in de["panels"]], [12, 21, 34])
        self.assertEqual(load_alignment_record("2:35").status, "verified")
        self.assertIsNone(load_alignment_record("2:35", root=self.local / "other"))
        with self.assertRaisesRegex(AlignmentValidationError, "Arabic source hash"):
            resolve_render_ayah_subtitle_plan(self.ayah, RenderConfig())

    def test_saved_preview_fails_final_publishing_gate(self):
        mock_live_authorization(self)
        for marker in ({"subtitle_mode": "UNVERIFIED_ALIGNMENT_PREVIEW"}, {"alignment_status": "unverified"},
                       {"alignment_data_version": "english_german_rwwad-candidate-v1"}):
            record = eligible_record()
            record.provenance["subtitle_ayahs"][0].update(marker)
            with self.subTest(marker=marker), self.assertRaisesRegex(EditorialPolicyError, "UNVERIFIED_ALIGNMENT"):
                require_record_eligible(record)
