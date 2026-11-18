"""Final fixed margins and native optical Arabic layout acceptance."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from engine.layout_config import LAYOUT, clamp_header_offset, safe_rectangle, sura_header_top
from engine.arabic_layout import measure_arabic_geometry
from engine.renderer import build_ass_text_probe, measure_ass_text_block, resolve_ffmpeg_binary
from engine.storage import StoredConfig, stored_config_from_dict, stored_config_to_dict
from engine.subtitles import SubtitleConfig, TextBlockBounds, calculate_multilingual_subtitle_group_layout, SubtitleError


ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / 'assets/fonts'
# Typesetting samples only. The labels deliberately contain no verse text.
SAMPLES = ("مِثَال", " ".join(["تِجَارِب"] * 8))


def render_png(ffmpeg, ass, destination, temp):
    script = temp / "layout.ass"
    script.write_text(ass, encoding="utf-8")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=1080x1920:r=1:d=1", "-vf",
                    f"ass=filename='{script}':fontsdir='{FONTS}'", "-frames:v", "1",
                    str(destination)], check=True)


class TestFinalReelLayout(unittest.TestCase):
    def test_fixed_safe_rectangle_and_header_setting(self):
        self.assertEqual(LAYOUT['safeZone'], dict(top=80,left=80,right=80,bottom=120))
        self.assertEqual(safe_rectangle(), dict(x=80,y=80,width=920,height=1720,right=1000,bottom=1800))
        self.assertEqual(LAYOUT['suraHeaderOffset'], dict(min=0,max=60,step=5,default=50))
        self.assertEqual(sura_header_top(), 130)
        for value, expected in [(-10,0),(0,0),(10,10),(12,10),(13,15),(50,50),(65,60)]:
            self.assertEqual(clamp_header_offset(value), expected)
            self.assertEqual(sura_header_top(value),80+expected)
            config=stored_config_from_dict(dict(sura_header_top_offset=value))
            self.assertEqual(config.sura_header_top_offset,expected)
            self.assertEqual(stored_config_from_dict(stored_config_to_dict(config)),config)
        self.assertEqual(StoredConfig().sura_header_top_offset,50)
        for value in [None,True,'10',float('nan')]:
            with self.assertRaises(ValueError):clamp_header_offset(value)

    def test_complete_group_moves_inside_margins_or_rejects_impossible_fit(self):
        ar=TextBlockBounds(-400,0,400,900,4)
        tr=TextBlockBounds(-400,0,400,400,4)
        layout=calculate_multilingual_subtitle_group_layout(ar,[tr],SubtitleConfig(arabic_translation_gap_px=60))
        self.assertEqual(layout.group_bottom_y,1800)
        self.assertEqual(layout.translation_visible_ranges[0][0]-layout.arabic_visible_bottom_y,60)
        self.assertGreaterEqual(layout.group_top_y,80)
        shifted=calculate_multilingual_subtitle_group_layout(TextBlockBounds(-461,0,459,100,1),[tr],SubtitleConfig())
        self.assertEqual(shifted.anchor_x,541)
        with self.assertRaises(SubtitleError):
            calculate_multilingual_subtitle_group_layout(ar,[tr,tr,tr],SubtitleConfig())
        with self.assertRaises(SubtitleError):
            calculate_multilingual_subtitle_group_layout(TextBlockBounds(-461,0,460,100,1),[tr],SubtitleConfig())

    def test_natural_line_pixels_are_preserved_and_gaps_scale(self):
        ffmpeg=resolve_ffmpeg_binary()
        with tempfile.TemporaryDirectory() as directory:
            temp=Path(directory)
            for size in (120,160,180):
                for sample in SAMPLES:
                    params=dict(font_name='Amiri Quran',font_size=size,video_width=1080,video_height=1920,margin_sides=80,fonts_dir=str(FONTS))
                    raw=build_ass_text_probe(sample,**params,normalize_arabic=False)
                    fixed=build_ass_text_probe(sample,**params)
                    geom=measure_arabic_geometry(raw,size,1080,1920,str(FONTS))
                    self.assertEqual(geom.gap,round(size*.15))
                    render_png(ffmpeg,raw,temp/'raw.png',temp)
                    render_png(ffmpeg,fixed,temp/'fixed.png',temp)
                    before=Image.open(temp/'raw.png').convert('L')
                    after=Image.open(temp/'fixed.png').convert('L')
                    # Full pixel histogram proves no marks/strokes/antialiasing disappear.
                    self.assertEqual(before.histogram(),after.histogram(),(sample, size))
                    for a,b in zip(geom.lines,geom.lines[1:]):
                        self.assertEqual(b.top+b.shift-a.bottom-a.shift,geom.gap)
                    measured=measure_ass_text_block(sample,**params,ffmpeg_bin=ffmpeg)
                    self.assertEqual(measured.line_count,len(geom.lines))
                    self.assertEqual(measured.height,geom.lines[-1].bottom+geom.lines[-1].shift-geom.lines[0].top)
                    if size == 180:
                        if sample == SAMPLES[0]:
                            self.assertEqual(measured.line_count, 1)
                        else:
                            self.assertGreater(measured.line_count, 1)
