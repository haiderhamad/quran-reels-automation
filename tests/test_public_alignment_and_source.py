"""Offline contracts for self-hosters using invented labels and local temporary files."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.alignments import AlignmentValidationError, load_validated_alignment, normalized_sha256
from engine.translation import TranslationSourceError, fetch_translation_data_for_language


class PublicAlignmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.record_file = self.directory / "002" / "021.json"
        self.record_file.parent.mkdir()
        self.inputs = {
            "verse_key": "2:21",
            "arabic_text": "صندوق أزرق",
            "english_text": "blue box",
            "english_translation_key": "english_rwwad",
            "english_translation_version": "1.0.19",
        }
        self.data = {
            "schema_version": 1,
            "data_version": "english_rwwad-v1",
            "verse_key": self.inputs["verse_key"],
            "arabic": {"source": "quran_foundation_text_uthmani",
                       "normalized_sha256": normalized_sha256(self.inputs["arabic_text"])},
            "english": {"translation_key": self.inputs["english_translation_key"],
                        "translation_version": self.inputs["english_translation_version"],
                        "normalized_sha256": normalized_sha256(self.inputs["english_text"])},
            "tokenization_version": "whitespace_v1",
            "status": "verified",
            "reviewed_at": "2000-01-01",
            "review_note": "Synthetic unit-test fixture",
            "timing_strategy": "arabic_base_letter_weight_v1",
            "panels": [
                {"panel_index": i, "arabic_word_start": i, "arabic_word_end": i + 1,
                 "english_token_start": i, "english_token_end": i + 1,
                 "arabic_text": ar, "english_text": en}
                for i, (ar, en) in enumerate(zip(self.inputs["arabic_text"].split(),
                                                 self.inputs["english_text"].split(), strict=True))
            ],
        }

    def load(self):
        self.record_file.write_text(json.dumps(self.data), encoding="utf-8")
        return load_validated_alignment(root=self.directory, **self.inputs)

    def test_two_reviewed_panels_round_trip(self):
        plan = self.load()
        self.assertEqual(" ".join(p.arabic_text for p in plan.panels), self.inputs["arabic_text"])
        self.assertEqual(" ".join(p.english_text for p in plan.panels), self.inputs["english_text"])

    def test_absent_record_grants_no_phrase_alignment(self):
        self.assertIsNone(load_validated_alignment(root=self.directory, **self.inputs))

    def test_unreviewed_record_is_refused(self):
        self.data["status"] = "candidate"
        with self.assertRaisesRegex(AlignmentValidationError, "not verified"):
            self.load()

    def test_source_hash_is_bound(self):
        self.inputs["arabic_text"] += " sample"
        with self.assertRaisesRegex(AlignmentValidationError, "Arabic source hash"):
            self.load()

    def test_translation_version_is_bound(self):
        self.inputs["english_translation_version"] = "0.0.0"
        with self.assertRaisesRegex(AlignmentValidationError, "version mismatch"):
            self.load()

    def test_noncontiguous_source_range_is_refused(self):
        self.data["panels"][1]["english_token_start"] = 0
        with self.assertRaisesRegex(AlignmentValidationError, "contiguous"):
            self.load()

    def test_tampered_panel_is_refused_even_when_source_hash_matches(self):
        self.data["panels"][0]["english_text"] = "red"
        with self.assertRaisesRegex(AlignmentValidationError, "does not match its source range"):
            self.load()


class ExplicitReferenceTests(unittest.TestCase):
    def test_missing_or_corrupt_reference_never_falls_back_to_network_or_populates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            damaged = root / "damaged.json.gz"
            damaged.write_bytes(b"invalid compressed data")
            for reference in (root / "absent.json", damaged):
                with self.subTest(reference=reference.name):
                    with patch("engine.translation.fetch_complete_translation_data") as remote:
                        with self.assertRaises(TranslationSourceError):
                            fetch_translation_data_for_language("en", cache_dir=cache, reference_path=reference)
                        remote.assert_not_called()
                        self.assertEqual(list(cache.glob("*")), [])
