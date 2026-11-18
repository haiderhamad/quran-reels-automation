"""A new installation has no previous review decisions or completed cycles."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from engine import reel_theme_policy
from engine.storage import CoverageState


class PublicEditorialBaselineTests(unittest.TestCase):
    def test_fresh_distribution_has_no_legacy_approvals(self):
        self.assertEqual(reel_theme_policy.eligible_verse_keys(), frozenset())
        self.assertFalse(reel_theme_policy.evaluate_range(2, 21, 21).eligible)
        self.assertFalse(CoverageState().is_cycle_completed)

    def test_empty_coverage_cannot_finish_a_cycle_with_no_approved_keys(self):
        with patch("engine.storage.eligible_verse_keys", return_value=frozenset()):
            self.assertFalse(CoverageState().is_cycle_completed)

    def test_operator_catalog_retains_review_and_context_validation(self):
        with TemporaryDirectory() as local:
            path = Path(local) / "catalog.json"
            config = {"schema_version": 1, "policy_version": "calm-inviting-v1",
                      "content_profile": "calm_inviting",
                      "ranges": [{"surah": 11, "first_ayah": 11, "last_ayah": 12,
                                  "theme": "unit-test", "review_note": "Synthetic test",
                                  "source_url": "https://example.test/source"}],
                      "context_requirements": [{"required_when_contains": "11:12",
                                                "require_any_of": ["11:11"],
                                                "review_note": "Synthetic contextual pair"}]}
            path.write_text(json.dumps(config), encoding="utf-8")
            self.addCleanup(reel_theme_policy.eligible_verse_keys.cache_clear)
            with patch.object(reel_theme_policy, "CATALOG_PATH", path):
                reel_theme_policy.eligible_verse_keys.cache_clear()
                self.assertTrue(reel_theme_policy.evaluate_range(11, 11, 12).eligible)
                self.assertFalse(reel_theme_policy.evaluate_range(11, 12, 12).eligible)
                self.assertFalse(reel_theme_policy.evaluate_range(11, 13, 13).eligible)
