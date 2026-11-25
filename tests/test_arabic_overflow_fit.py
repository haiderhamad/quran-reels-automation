"""Regression coverage for adaptive Arabic layout in the final ASS renderer."""
from dataclasses import replace
from pathlib import Path
import unittest

from engine.renderer import (
    RenderAyah, RenderConfig, RenderRequest, RenderTranslation, SubtitlePreflightError,
    generate_ass_subtitles, preflight_subtitle_layout,
)
from engine.subtitles import MODE_STATIC_FULL_AYAH, SubtitleConfig


ARABIC_SENTENCE = "عِنْدَمَا تَحْضُرُ الْفُرْصَةُ يُمْكِنُنَا أَنْ نَبْدَأَ الْعَمَلَ الْجَدِيدَ"
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"


class ArabicOverflowFitTests(unittest.TestCase):
    def setUp(self):
        self.config = RenderConfig(
            fonts_dir=str(FONTS),
            subtitle_config=SubtitleConfig(
                subtitle_mode=MODE_STATIC_FULL_AYAH,
                arabic_font_size=160,
                arabic_max_lines=2,
                translation_font_size=40,
                translation_stack_gap_px=120,
            ),
        )
        self.translations = (
            RenderTranslation("en", "We may start the new work."),
            RenderTranslation("de", "Wir können mit der neuen Arbeit anfangen."),
        )

    def ayah(self, number, repeats, translations=None):
        start_ms = (number - 56) * 5000
        return RenderAyah(
            surah_number=2,
            ayah_number=number,
            arabic_text=" ".join([ARABIC_SENTENCE] * repeats),
            english_text="",
            start_ms=start_ms,
            end_ms=start_ms + 5000,
            footnotes="",
            translations=self.translations if translations is None else translations,
        )

    def request(self, *ayahs):
        return RenderRequest(2, "", "", ayahs, "", (), "")

    def test_only_overflowing_panel_shrinks_and_keeps_all_three_lines(self):
        normal, overflowing = self.ayah(56, 2), self.ayah(57, 3)
        request = self.request(normal, overflowing)
        result = preflight_subtitle_layout(request, self.config)
        self.assertEqual(
            [(panel.verse_key, panel.arabic_lines, panel.arabic_font_size) for panel in result.panels],
            [("2:56", 2, 160), ("2:57", 3, 156)],
        )
        self.assertEqual(result.panels[1].visible_translation_stack_gaps, (120,))
        ass = generate_ass_subtitles(request, self.config)
        arabic_events = [line for line in ass.splitlines() if ",QuranPhraseAr," in line]
        self.assertEqual(len(arabic_events), 5)
        self.assertTrue(all(line.endswith(normal.arabic_text) and r"\fs" not in line for line in arabic_events[:2]))
        self.assertTrue(all(line.endswith(overflowing.arabic_text) and r"\fs156" in line for line in arabic_events[2:]))
        self.assertTrue(all(",0:00:05.00,0:00:10.00," in line for line in arabic_events[2:]))
        self.assertTrue(all(item.text in ass for item in self.translations))

    def test_larger_overflow_tries_progressively_smaller_sizes(self):
        result = preflight_subtitle_layout(self.request(self.ayah(57, 4)), self.config)
        (panel,) = result.panels
        self.assertEqual(panel.arabic_lines, 3)
        self.assertLess(panel.arabic_font_size, 156)
        self.assertGreaterEqual(panel.arabic_font_size, 72)

    def test_an_explicit_one_line_limit_is_respected(self):
        config = replace(self.config, subtitle_config=replace(self.config.subtitle_config, arabic_max_lines=1))
        with self.assertRaises(SubtitlePreflightError) as caught:
            preflight_subtitle_layout(self.request(self.ayah(56, 2)), config)
        self.assertEqual((caught.exception.language, caught.exception.allowed_lines), ("ar", 1))

    def test_english_overflow_still_fails(self):
        translations = (replace(self.translations[0], text=" ".join([self.translations[0].text] * 14)), self.translations[1])
        with self.assertRaises(SubtitlePreflightError) as caught:
            preflight_subtitle_layout(self.request(self.ayah(57, 3, translations)), self.config)
        self.assertEqual((caught.exception.language, caught.exception.allowed_lines), ("en", 4))

    def test_minimum_size_can_fit_three_lines_without_further_shrinking(self):
        config = replace(self.config, subtitle_config=replace(self.config.subtitle_config, arabic_font_size=72))
        result = preflight_subtitle_layout(self.request(self.ayah(57, 5)), config)
        (panel,) = result.panels
        self.assertEqual((panel.arabic_lines, panel.arabic_font_size), (3, 72))

    def test_overflow_cannot_shrink_below_supported_size(self):
        config = replace(self.config, subtitle_config=replace(self.config.subtitle_config, arabic_font_size=72))
        with self.assertRaises(SubtitlePreflightError) as caught:
            preflight_subtitle_layout(self.request(self.ayah(57, 12)), config)
        self.assertEqual((caught.exception.language, caught.exception.font_size), ("ar", 72))
