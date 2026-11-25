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

    def test_four_lines_fit_in_each_language_at_the_legacy_default(self):
        for counts in ({"en": 4}, {"de": 4}, {"en": 4, "de": 4}):
            with self.subTest(counts=counts):
                panels = self.measure_panel(counts)
                self.assertEqual(len(panels), 1)
                self.assertEqual([b.line_count for b in panels[0].translation_bounds], list(counts.values()))

    def test_errors_report_the_offending_language_and_its_effective_limit(self):
        for counts, setting, language, limit in (
            ({"en": 5, "de": 4}, 5, "en", 4), ({"en": 3, "de": 5}, 3, "de", 4),
            ({"en": 2, "de": 4}, 1, "en", 1), ({"en": 3, "de": 4}, 2, "en", 2),
        ):
            with self.subTest(counts=counts, setting=setting):
                with self.assertRaises(SubtitlePreflightError) as ctx:
                    self.measure_panel(counts, setting)
                self.assertEqual((ctx.exception.language, ctx.exception.allowed_lines, ctx.exception.required_lines),
                                 (language, limit, counts[language]))

    def test_arabic_shrinks_to_fit_the_combined_safe_zone_even_with_two_lines(self):
        config = build_render_config_from_stored_config({"arabic_font_size": 160, "translation_max_lines": 4})
        translations = (RenderTranslation("en", "en"), RenderTranslation("de", "de"))
        ayah = RenderAyah(2, 1, "arabic", "", "", 0, 4000, translations)

        def measure(text, *, font_size, **kwargs):
            if text == "arabic":
                return TextBlockBounds(-50, 0, 50, font_size * 4, 2)
            return TextBlockBounds(-50, 0, 50, 470, 4)

        with patch("engine.renderer.measure_ass_text_block", side_effect=measure):
            _, (panel,) = _select_measured_phrase_plan(ayah, config, fonts_dir="", ffmpeg_bin="ffmpeg")
        self.assertEqual(panel.arabic_font_size, 152)
        self.assertEqual(panel.arabic_bounds.line_count, 2)
        self.assertEqual([bounds.line_count for bounds in panel.translation_bounds], [4, 4])
        self.assertLessEqual(panel.layout.group_bottom_y, 1800)

    def test_safe_area_remains_enforced_at_allowed_line_counts(self):
        with self.assertRaisesRegex(SubtitlePreflightError, "exceed the fixed text Safe Zone"):
            self.measure_panel({"en": 3, "de": 4}, height=800)

    def test_missing_configuration_defaults_english_to_four(self):
        self.assertEqual(SubtitleConfig().translation_max_lines, 4)
        for saved in ({}, StoredConfig(), stored_config_from_dict({})):
            self.assertEqual(build_render_config_from_stored_config(saved).subtitle_config.translation_max_lines, 4)
