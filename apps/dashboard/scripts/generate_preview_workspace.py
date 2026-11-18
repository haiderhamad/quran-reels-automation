#!/usr/bin/env python3
"""Offline static workspace assets. Never reads runtime state or dispatches work.

Glyphs use the production libass probe. Every selectable stack position is exported
from the production layout helper, so the browser only composes/scales static data.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.fetch_fonts import install_bundled_fonts
from engine.layout_config import safe_rectangle
from engine.arabic_layout import measure_arabic_geometry
from engine.renderer import (build_ass_text_probe, measure_ass_text_block,
    resolve_ffmpeg_binary, verify_fonts_available, generate_ass_subtitles,
    RenderConfig, RenderRequest, RenderAyah, DEFAULT_OVERLAY_DARKNESS)
from engine.subtitles import (SubtitleConfig, TextBlockBounds,
    calculate_multilingual_subtitle_group_layout, SAFE_MARGIN_SIDES,
    SAFE_MARGIN_BOTTOM, HEADER_VISIBLE_TOP, SUBTITLE_CENTER_Y,
    VALID_HEADER_CONTENTS, VALID_REF_POSITIONS, VALID_HEADER_STYLES,
    HIGHLIGHT_COLORS)

OUTPUT = ROOT / "apps/dashboard/public/previews/v3"
FONTS = ROOT / ".cache/fonts"
SAMPLES_PATH = Path(__file__).parent / "assets/preview-samples.json"
SAMPLES = json.loads(SAMPLES_PATH.read_text())
ARABIC_SIZES = range(72, 181)  # Include odd persisted values as well as slider steps.
TR_SIZES = (50, 40, 30)
GAPS = (20, 40, 60)
LANGUAGES = (("en",), ("de",), ("en", "de"))


def render_png(ffmpeg, ass, destination, temp, transparent=False):
    path = temp / "probe.ass"
    path.write_text(ass)
    source = "color=c=black@0:s=1080x1920:r=1:d=1,format=rgba" if transparent else "color=c=black:s=1080x1920:r=1:d=1"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", source,
        "-vf", f"ass=filename='{path}':fontsdir='{FONTS}':alpha=1", "-frames:v", "1", str(destination)], check=True)



def preview_line_ranges(alpha, font_size):
    """Return painted row bands relative to the top edge, excluding the 2 px bleed."""
    mask = alpha.point(lambda value: 255 if value > 16 else 0)
    bands = []
    merge_gap = max(3, round(font_size * .10))
    for y in range(mask.height):
        if mask.crop((0, y, mask.width, y + 1)).getbbox() is None:
            continue
        if not bands or y - bands[-1][1] > merge_gap:
            bands.append([y, y + 1])
        else:
            bands[-1][1] = y + 1
    return [[top - 2, bottom - 2] for top, bottom in bands]


def generate():
    from PIL import Image, ImageOps
    install_bundled_fonts(str(FONTS))
    ffmpeg = resolve_ffmpeg_binary()
    verify_fonts_available(str(FONTS))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 3, "canvas": [1080, 1920], "header_top": HEADER_VISIBLE_TOP,
        "overlay_darkness": DEFAULT_OVERLAY_DARKNESS, "subtitle_center": SUBTITLE_CENTER_Y, "safe_bottom": 1920 - SAFE_MARGIN_BOTTOM,
        "safe_zone": safe_rectangle(), "samples": SAMPLES, "glyphs": {}, "layouts": {}, "headers": {},
        "colors": {}}
    # ASS colors are &H00BBGGRR; remove prefix/alpha before reversing channels.
    manifest["colors"] = {k: '#' + v[-2:] + v[-4:-2] + v[-6:-4] for k, v in HIGHLIGHT_COLORS.items()}
    with tempfile.TemporaryDirectory(prefix="reel-preview-") as tmp:
        temp = Path(tmp)
        for sample_number, sample in enumerate(SAMPLES, start=1):
            for language in ("arabic", "en", "de"):
                for size in ARABIC_SIZES if language == "arabic" else TR_SIZES:
                    key = f'{sample["id"]}-{language}-{size}'
                    params = dict(font_name="Amiri Quran" if language == "arabic" else "Noto Sans",
                        font_size=size, video_width=1080, video_height=1920, margin_sides=SAFE_MARGIN_SIDES)
                    bounds = measure_ass_text_block(sample[language], **params, fonts_dir=str(FONTS), ffmpeg_bin=ffmpeg)
                    render_png(ffmpeg, build_ass_text_probe(sample[language], **params, fonts_dir=str(FONTS)), temp / "glyph.png", temp)
                    # Retain low-alpha antialiasing around the >16 production measurement.
                    box = (540 + bounds.left_offset - 2, 320 + bounds.top_offset - 2,
                           540 + bounds.right_offset + 2, 320 + bounds.bottom_offset + 2)
                    alpha = Image.open(temp / "glyph.png").convert("L").crop(box)
                    glyph = Image.new("RGBA", alpha.size, "white")
                    glyph.putalpha(alpha)
                    glyph.save(OUTPUT / f"{key}.webp", lossless=True)
                    visual_ranges = preview_line_ranges(alpha, size)
                    if language == 'arabic':
                        raw = build_ass_text_probe(sample[language], **params, fonts_dir=str(FONTS), normalize_arabic=False)
                        geometry = measure_arabic_geometry(raw, size, 1080, 1920, str(FONTS))
                        visual_ranges = [[line.top + line.shift - 320 - bounds.top_offset,
                                          line.bottom + line.shift - 320 - bounds.top_offset] for line in geometry.lines]
                    manifest["glyphs"][key] = {**asdict(bounds), "image": f"/previews/v3/{key}.webp",
                        "visual_line_ranges": visual_ranges}
            print(f'Glyphs: {sample["id"]}', flush=True)
            for ar_size, tr_size, gap, languages in itertools.product(ARABIC_SIZES, TR_SIZES, GAPS, LANGUAGES):
                for stack in (10, 20, 30) if len(languages) == 2 else (10,):
                    keys = [f'{sample["id"]}-arabic-{ar_size}', *[f'{sample["id"]}-{lang}-{tr_size}' for lang in languages]]
                    bounds = [TextBlockBounds(**{k: v for k, v in manifest['glyphs'][key].items() if k in TextBlockBounds.__dataclass_fields__}) for key in keys]
                    config = SubtitleConfig(arabic_font_size=ar_size, translation_font_size=tr_size,
                        arabic_translation_gap_px=gap, translation_stack_gap_px=stack)
                    layout = calculate_multilingual_subtitle_group_layout(bounds[0], bounds[1:], config)
                    key = f'{sample["id"]}/{ar_size}/{tr_size}/{gap}/{"-".join(languages)}/{stack if len(languages) == 2 else 0}'
                    manifest['layouts'][key] = {"glyphs": keys, "anchor_x": layout.anchor_x,
                        "tops": [layout.arabic_visible_top_y, *[r[0] for r in layout.translation_visible_ranges]],
                        "backing": layout.backing_rect, "bottom": layout.group_bottom_y}

            # Header pixels and placement come from the real ASS generator, including
            # the capsule. No browser-specific header positioning calculations.
            request = RenderRequest(surah_number=1, arabic_surah_name="دراسة الخط", english_surah_name="Type study",
                ayahs=(RenderAyah(surah_number=1, ayah_number=sample_number, arabic_text=SAMPLES[0]['arabic'],
                    english_text=SAMPLES[0]['en'], footnotes="", start_ms=0, end_ms=1000),),
                surah_audio_path="", backgrounds=(), output_path="")
            for content, reference, style, intensity in itertools.product(VALID_HEADER_CONTENTS, VALID_REF_POSITIONS,
                    VALID_HEADER_STYLES, ('low', 'medium', 'high')):
                key = f'{sample["id"]}/{content}/{reference}/{style}/{intensity}'
                config = SubtitleConfig(header_content=content, ayah_reference_position=reference,
                    header_style=style, header_backing_intensity=intensity)
                arabic_title = "" if style == "hidden" else "دراسة الخط"
                english_title = ""
                if arabic_title and content != "surah_name_only":
                    english_title = "Type study"
                    if content == "arabic_translation_ayah_range" and reference == "top_header":
                        english_title += f" • Sample {sample_number}"
                # Retain native layout/measurement, substituting the neutral display labels
                # before measurement so capsule geometry also matches the sample title.
                with patch("engine.renderer.format_header_text", return_value=(arabic_title, english_title)):
                    ass = generate_ass_subtitles(request, RenderConfig(fonts_dir=str(FONTS), render_footnotes=False, subtitle_config=config))
                ass = '\n'.join(line for line in ass.splitlines() if not line.startswith('Dialogue:') or
                    ',SurahHeader' in line or line.startswith('Dialogue: 5,'))
                digest = hashlib.sha256(ass.encode()).hexdigest()[:20]
                filename = f'header-{digest}.webp'
                if not (OUTPUT / filename).exists():
                    render_png(ffmpeg, ass, temp / 'header.png', temp, transparent=True)
                    Image.open(temp / 'header.png').crop((0, 0, 1080, 300)).save(OUTPUT / filename, lossless=True)
                manifest['headers'][key] = f'/previews/v3/{filename}'
            print(f'Layouts and headers: {sample["id"]}', flush=True)
    # These deterministic neutral gradients are generated from pixel values.
    gradient = Image.linear_gradient("L").resize((1080, 1920))
    for name, top, bottom in (("slate", "#65717d", "#33404b"), ("charcoal", "#42474b", "#1e2327")):
        ImageOps.colorize(gradient, black=top, white=bottom).save(OUTPUT / f"{name}.webp", lossless=True)
    urls = set(manifest['headers'].values()) | {g['image'] for g in manifest['glyphs'].values()}
    filenames = {Path(url).name for url in urls} | {'slate.webp', 'charcoal.webp'}
    manifest['files'] = {name: hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest() for name in sorted(filenames)}
    manifest['source_hashes'] = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
        'engine/renderer.py', 'engine/subtitles.py', 'engine/arabic_layout.py', 'engine/layout_config.py', 'apps/dashboard/reel-layout.json', 'apps/dashboard/scripts/assets/preview-samples.json',
        'apps/dashboard/scripts/generate_preview_workspace.py')}
    (OUTPUT / 'workspace.json').write_text(json.dumps(manifest, ensure_ascii=False, separators=(',', ':')) + '\n')
    verify()


def verify():
    manifest = json.loads((OUTPUT / 'workspace.json').read_text())
    assert manifest['version'] == 3 and manifest['canvas'] == [1080, 1920]
    assert manifest['header_top'] == HEADER_VISIBLE_TOP
    assert manifest['safe_zone'] == safe_rectangle()
    assert len(manifest['layouts']) == len(SAMPLES) * len(ARABIC_SIZES) * 45
    assert manifest['samples'] == SAMPLES
    assert set(manifest['files']) == {p.name for p in OUTPUT.glob('*.webp')}, 'Unexpected or missing preview assets'
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest() == digest, name
    for name, digest in manifest['source_hashes'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, f'Regenerate preview after changing {name}'
    for sample, lines in zip(SAMPLES, (1, 2, 3, 2)):
        assert len(manifest['glyphs'][f'{sample["id"]}-arabic-180']['visual_line_ranges']) == lines
    print(f"Verified {len(manifest['layouts'])} layouts, {len(manifest['files'])} assets.", flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    verify() if args.verify_only else generate()
