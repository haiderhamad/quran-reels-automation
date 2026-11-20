"""Check the approved 2:35 Worker subtitles from public sources and a fresh cache."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from engine import audio, translation
from engine.alignments import SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE
from engine.config import DurationConfig, VerseSelectionLimits
from engine.main import RunRequest, _build_subtitle_preflight_request
from engine.renderer import RenderConfig, _build_resolved_phrase_panels, preflight_subtitle_layout
from engine.subtitles import SubtitleConfig
from engine.verses import select_owner_v1_ayah_candidate
from scripts.fetch_fonts import install_bundled_fonts
from scripts.review_sources import fetch_public_quran


def verify() -> dict:
    if os.environ.get(translation.ENGLISH_REFERENCE_PATH_ENV):
        raise ValueError(f"Unset {translation.ENGLISH_REFERENCE_PATH_ENV} for this public-source check.")
    with tempfile.TemporaryDirectory(prefix="public-235-") as directory:
        cache = Path(directory)
        sources = {lang: translation.fetch_translation_data_for_language(lang, cache_dir=cache)
                   for lang in ("en", "de")}
        if tuple(s.source.version for s in sources.values()) != ("1.0.19", "1.0.15"):
            raise ValueError("Unexpected Rowwad edition.")

        corpus = fetch_public_quran()
        timing = audio.fetch_surah_timing(2).get_ayah_timing(35)
        if (timing.start_ms, timing.end_ms) != (707420, 725980):
            raise ValueError("The read-92 interval differs from the reviewed recitation.")
        selection = audio.to_engine_verse_timing(
            timing, corpus.get_ayah(2, 35).arabic_text, sources["en"].get_ayah(2, 35).translation_text)
        duration = DurationConfig(10, 60)
        candidate = select_owner_v1_ayah_candidate(
            (selection,), "2:35", duration_config=duration, limits=VerseSelectionLimits())

        fonts = cache / "fonts"
        install_bundled_fonts(str(fonts))
        config = RenderConfig(fonts_dir=str(fonts), subtitle_config=SubtitleConfig(
            arabic_font_size=160, translation_font_size=40, arabic_max_lines=2,
            translation_max_lines=3, arabic_translation_gap_px=120, translation_stack_gap_px=120))
        render, (ayah,) = _build_subtitle_preflight_request(
            candidate=candidate, selected_surah=2, quran_data=corpus, translations_by_language=sources,
            request=RunRequest(translation_languages=("en", "de"), render_config=config, duration_config=duration))
        if ayah.subtitle_mode != SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE:
            raise ValueError("The default Worker did not select the verified bilingual record.")
        measured = preflight_subtitle_layout(render, config)
        windows = [(p.start_ms - timing.start_ms, p.end_ms - timing.start_ms)
                   for p in _build_resolved_phrase_panels(ayah, config)]
        if windows != [(0, 6338), (6338, 10864), (10864, 18560)]:
            raise ValueError(f"Panel timing differs from the review: {windows}")
        if len(measured.panels) != 3 or any(p.visible_translation_stack_gaps != (120,)
                                             for p in measured.panels):
            raise ValueError("Unexpected bilingual layout.")
        return {
            "verse": "2:35", "languages": {lang: source.source.version for lang, source in sources.items()},
            "subtitle_mode": ayah.subtitle_mode, "read_92_interval_ms": [timing.start_ms, timing.end_ms],
            "panel_windows_ms": windows,
            "lines": [{"arabic": p.arabic_lines, **dict(p.translation_lines)} for p in measured.panels],
        }


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
