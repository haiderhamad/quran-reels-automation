"""Pin the reviewed display boundaries without redistributing any source text."""
import hashlib
import json
import unittest

from engine.alignments import PACKAGED_ALIGNMENT_ROOT, load_alignment_record


class PackagedEnglishPanelTests(unittest.TestCase):
    def test_approved_english_panels_are_available_in_a_clean_installation(self):
        # Fingerprints cover the ordered Arabic/English boundaries and slice
        # digests from the reviewed records, independently of JSON formatting.
        cases = (
            ("2:20", "2026-11-07", 5, "a36dc046ea7de378ae4fd8fcfd334cd1342ac6400208890ffbdfc66501fb61d8"),
            ("2:25", "2026-11-16", 7, "19140d672f18fd43321f77e947bc2ff035469b08a431533a5c4b269c3e996fa3"),
        )
        for verse, date, count, fingerprint in cases:
            with self.subTest(verse=verse):
                record = load_alignment_record(verse, root=PACKAGED_ALIGNMENT_ROOT)
                self.assertIsNotNone(record, f"Missing reviewed panels for {verse}")
                self.assertEqual(record.schema_version, 2)
                self.assertEqual((record.status, record.reviewed_at), ("verified", date))
                self.assertEqual(record.english_translation_version, "1.0.19")
                self.assertEqual(record.timing_strategy, "arabic_base_letter_weight_v1")
                self.assertEqual([p.panel_index for p in record.panels], list(range(count)))
                proof = [(p.arabic_word_start, p.arabic_word_end, p.english_token_start,
                          p.english_token_end, p.arabic_sha256, p.english_sha256) for p in record.panels]
                encoded = json.dumps(proof, separators=(",", ":")).encode("ascii")
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), fingerprint)
