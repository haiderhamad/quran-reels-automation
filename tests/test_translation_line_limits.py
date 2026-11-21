"""Per-language line limits apply to the same measured plans used for final ASS."""
import unittest
from unittest.mock import patch

from engine.job import build_render_config_from_stored_config
from engine.renderer import (
    RenderAyah, RenderTranslation, SubtitlePreflightError, _select_measured_phrase_plan,
)
from engine.storage import StoredConfig, stored_config_from_dict
from engine.subtitles import SubtitleConfig, TextBlockBounds


class TranslationLineLimitTests(unittest.TestCase):
    def measure_panel(self, counts, saved_limit=3, height=40):
        config = build_render_config_from_stored_config({"translation_max_lines": saved_limit})
        ayah = RenderAyah(2, 1, "arabic", "", "", 0, 4000,
                          tuple(RenderTranslation(lang, lang) for lang in counts))

        def measure(text, **kwargs):
            return TextBlockBounds(-50, 0, 50, height, counts.get(text, 1))

        with patch("engine.renderer.measure_ass_text_block", side_effect=measure):
            _, panels = _select_measured_phrase_plan(ayah, config, fonts_dir="", ffmpeg_bin="ffmpeg")
        return panels

    def test_german_four_lines_fit_alone_and_beside_three_english_lines(self):
        for counts in ({"de": 4}, {"en": 3, "de": 4}):
            with self.subTest(counts=counts):
                panels = self.measure_panel(counts)
                self.assertEqual(len(panels), 1)
                self.assertEqual([b.line_count for b in panels[0].translation_bounds], list(counts.values()))

    def test_errors_report_the_offending_language_and_its_effective_limit(self):
        for counts, setting, language, limit in (
            ({"en": 4, "de": 4}, 5, "en", 3), ({"en": 3, "de": 5}, 3, "de", 4),
            ({"en": 2, "de": 4}, 1, "en", 1), ({"en": 3, "de": 4}, 2, "en", 2),
        ):
            with self.subTest(counts=counts, setting=setting):
                with self.assertRaises(SubtitlePreflightError) as ctx:
                    self.measure_panel(counts, setting)
                self.assertEqual((ctx.exception.language, ctx.exception.allowed_lines, ctx.exception.required_lines),
                                 (language, limit, counts[language]))

    def test_safe_area_remains_enforced_at_allowed_line_counts(self):
        with self.assertRaisesRegex(SubtitlePreflightError, "exceed the fixed text Safe Zone"):
            self.measure_panel({"en": 3, "de": 4}, height=800)

    def test_missing_configuration_defaults_english_to_three(self):
        self.assertEqual(SubtitleConfig().translation_max_lines, 3)
        for saved in ({}, StoredConfig(), stored_config_from_dict({})):
            self.assertEqual(build_render_config_from_stored_config(saved).subtitle_config.translation_max_lines, 3)
