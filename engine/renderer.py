"""Deterministic FFmpeg Reel rendering module.

Assembles verified moving-nature background video segments, authentic Yasser Al-Dosari
recitation audio, canonical Arabic Uthmani Quran text, and verified translation subtitles
into a fixed 1080x1920 (9:16) 30 FPS vertical Reel.

Local and deterministic only. No network calls.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from engine.layout_config import REEL_WIDTH, REEL_HEIGHT, safe_rectangle, sura_header_top
from engine.arabic_layout import ArabicLayoutError, measure_arabic_geometry

from engine.alignments import (
    AlignmentValidationError,
    DEFAULT_ALIGNMENT_ROOT, PACKAGED_ALIGNMENT_ROOT, resolve_alignment_root,
    SUBTITLE_MODE_FULL_AYAH_FALLBACK,
    SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE,
    load_validated_alignment,
    normalized_sha256,
)

from engine.bilingual_alignments import (
    DEFAULT_BILINGUAL_ALIGNMENT_ROOT, PACKAGED_BILINGUAL_ALIGNMENT_ROOT, load_bilingual_alignment,
)

from engine.subtitles import (
    MultilingualPhrasePanel,
    MultilingualSubtitleGroupLayout,
    SubtitleConfig,
    SubtitleError,
    TextBlockBounds,
    TranslationPhrase,
    build_multilingual_panels_from_segments,
    build_multilingual_phrase_panels,
    calculate_multilingual_subtitle_group_layout,
    verify_multilingual_reconstruction,
    format_header_text,
    BACKING_GRADIENT,
    BACKING_SHADOW,
    BACKING_TRANSLUCENT,
    BACKING_LOCALIZED,
    HEADER_MINIMAL_CAPSULE,
    HEADER_HIDDEN,
    HIGHLIGHT_PHRASE_FADE,
    MODE_STATIC_FULL_AYAH,
    MIN_ARABIC_FONT_SIZE,
    SAFE_MARGIN_TOP,
    SAFE_MARGIN_BOTTOM,
    SAFE_MARGIN_SIDES,
    SUBTITLE_CENTER_Y,
)
from scripts.fetch_fonts import DEFAULT_CACHE_DIR as DEFAULT_FONTS_CACHE_DIR, verify_font_file


DEFAULT_WIDTH = REEL_WIDTH
DEFAULT_HEIGHT = REEL_HEIGHT
DEFAULT_FPS = 30
DEFAULT_VIDEO_CODEC = "libx264"
DEFAULT_PRESET = "medium"
DEFAULT_CRF = 20
DEFAULT_PIXEL_FORMAT = "yuv420p"
DEFAULT_AUDIO_CODEC = "aac"
DEFAULT_AUDIO_BITRATE = "128k"
DEFAULT_AUDIO_SAMPLE_RATE = 48000
DEFAULT_CROSSFADE_SECONDS = 0.75
DEFAULT_MIN_BACKGROUNDS = 1
DEFAULT_MAX_BACKGROUNDS = 3
DEFAULT_BACKGROUNDS_COUNT = 1
DEFAULT_DURATION_TOLERANCE_SECONDS = 0.05
DEFAULT_OVERLAY_DARKNESS = 0.20

# Typography defaults — Premium Quran Reel preset (D-005 updated)
DEFAULT_ARABIC_FONT_NAME = "Amiri Quran"
DEFAULT_ARABIC_TITLE_FONT_NAME = "Amiri"
DEFAULT_ENGLISH_FONT_NAME = "Noto Sans"
DEFAULT_ARABIC_FONT_SIZE = 180
ARABIC_FIT_STEP_PX = 4
DEFAULT_ENGLISH_FONT_SIZE = 60
DEFAULT_ARABIC_TITLE_FONT_SIZE = 72
DEFAULT_ENGLISH_TITLE_FONT_SIZE = 42
DEFAULT_AYAH_NUMBER_FONT_SIZE = 24
DEFAULT_FOOTNOTE_FONT_SIZE = 36
DEFAULT_MAX_FOOTNOTE_PANEL_CHARS = 100
DEFAULT_ARABIC_LINE_SPACING = 1.15
DEFAULT_TRANSLATION_LINE_SPACING = 1.25
DEFAULT_ARABIC_TRANSLATION_GAP_PX = 20
DEFAULT_TRANSLATION_STACK_GAP_PX = 10
DEFAULT_HEADER_GAP_PX = 4

REQUIRED_FONTS = (
    "AmiriQuran.ttf",
    "Amiri-Regular.ttf",
    "Amiri-Bold.ttf",
    "NotoSans-Regular.ttf",
)

_ASS_TEXT_BOUNDS_CACHE: dict[tuple[object, ...], TextBlockBounds] = {}


@dataclass(frozen=True)
class _MeasuredPhrasePanel:
    panel: MultilingualPhrasePanel
    arabic_font_size: int
    arabic_bounds: TextBlockBounds
    translation_bounds: tuple[TextBlockBounds, ...]
    layout: MultilingualSubtitleGroupLayout


SUBTITLE_MODE_UNVERIFIED_ALIGNMENT_PREVIEW = "UNVERIFIED_ALIGNMENT_PREVIEW"


class RenderError(Exception):
    """Raised when Reel rendering, validation, or asset verification fails."""


class SubtitlePreflightError(RenderError):
    """Structured fail-closed result for an impossible subtitle layout."""

    code = "SUBTITLE_PREFLIGHT_FAILED"

    def __init__(
        self,
        *,
        verse_key: str,
        reason: str,
        translation_stack_gap_px: Optional[int],
        attempted_boundaries: Sequence[str],
        language: Optional[str] = None,
        font_size: Optional[int] = None,
        allowed_lines: Optional[int] = None,
        required_lines: Optional[int] = None,
        panel_index: Optional[int] = None,
    ) -> None:
        self.verse_key = verse_key
        self.reason = reason
        self.translation_stack_gap_px = translation_stack_gap_px
        self.attempted_boundaries = tuple(attempted_boundaries)
        self.language = language
        self.font_size = font_size
        self.allowed_lines = allowed_lines
        self.required_lines = required_lines
        self.panel_index = panel_index
        details = [
            self.code,
            f"verse_key={verse_key}",
            "translation_stack_gap_px="
            + ("N/A" if translation_stack_gap_px is None else str(translation_stack_gap_px)),
        ]
        if language is not None:
            details.append(f"language={language}")
        if font_size is not None:
            details.append(f"font_size={font_size}")
        if allowed_lines is not None:
            details.append(f"allowed_lines={allowed_lines}")
        if required_lines is not None:
            details.append(f"required_lines={required_lines}")
        if panel_index is not None:
            details.append(f"panel_index={panel_index}")
        details.append(f"reason={reason}")
        details.append(
            "attempted_boundaries=" + ",".join(self.attempted_boundaries)
        )
        super().__init__("; ".join(details))


class AyahRequiresVerifiedAlignmentError(SubtitlePreflightError):
    """Full source ayah cannot fit without a reviewed phrase alignment."""

    code = "AYAH_REQUIRES_VERIFIED_ALIGNMENT"


@dataclass(frozen=True)
class RenderTranslation:
    """Exact source translation and footnotes for one enabled language."""

    language: str
    text: str
    footnotes: str = ""
    translation_key: str = ""
    translation_version: str = ""

    def __post_init__(self) -> None:
        if self.language not in {"en", "de"}:
            raise RenderError(f"Unsupported render translation language: {self.language}")
        if not self.text or not self.text.strip():
            raise RenderError(f"Translation text for '{self.language}' must be non-empty.")
        if not isinstance(self.footnotes, str):
            raise RenderError(f"Translation footnotes for '{self.language}' must be a string.")


@dataclass(frozen=True)
class RenderAyah:
    """One immutable ayah with Arabic, ordered translations, and timing."""

    surah_number: int
    ayah_number: int
    arabic_text: str
    english_text: str
    footnotes: str
    start_ms: int
    end_ms: int
    translations: tuple[RenderTranslation, ...] = ()
    subtitle_mode: Optional[str] = None
    subtitle_segments: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    subtitle_provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.translations:
            languages = tuple(item.language for item in self.translations)
            if len(set(languages)) != len(languages):
                raise RenderError("RenderAyah translation languages must be unique.")
        if self.subtitle_mode is None:
            if self.subtitle_segments or self.subtitle_provenance:
                raise RenderError("Unresolved RenderAyah cannot carry subtitle plan data.")
            return
        if self.subtitle_mode not in {
            SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE,
            SUBTITLE_MODE_FULL_AYAH_FALLBACK,
            SUBTITLE_MODE_UNVERIFIED_ALIGNMENT_PREVIEW,
        }:
            raise RenderError(f"Unsupported resolved subtitle mode: {self.subtitle_mode}")
        if not self.subtitle_segments:
            raise RenderError("Resolved RenderAyah must contain subtitle segments.")
        expected_languages = tuple(item.language for item in self.ordered_translations)
        reconstructed_arabic: list[str] = []
        reconstructed_translations = {language: [] for language in expected_languages}
        for arabic, translated in self.subtitle_segments:
            if tuple(language for language, _ in translated) != expected_languages:
                raise RenderError("Resolved subtitle segment languages/order mismatch.")
            reconstructed_arabic.extend(arabic.split())
            for language, text in translated:
                reconstructed_translations[language].extend(text.split())
        if reconstructed_arabic != self.arabic_text.split():
            raise RenderError("Resolved subtitle Arabic reconstruction is not exact.")
        for item in self.ordered_translations:
            if reconstructed_translations[item.language] != item.text.split():
                raise RenderError(
                    f"Resolved subtitle {item.language} reconstruction is not exact."
                )
        if self.subtitle_provenance.get("subtitle_mode") != self.subtitle_mode:
            raise RenderError("Resolved subtitle provenance mode mismatch.")

    @property
    def ordered_translations(self) -> tuple[RenderTranslation, ...]:
        if self.translations:
            return self.translations
        return (RenderTranslation("en", self.english_text, self.footnotes),)

    @property
    def start_time_seconds(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_time_seconds(self) -> float:
        return self.end_ms / 1000.0

    @property
    def duration_seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


@dataclass(frozen=True)
class RenderBackground:
    """A local moving-nature background video clip pre-approved by visual safety scanning."""

    local_path: str
    source_duration_seconds: float
    pexels_video_id: int
    requires_center_crop: bool = False


@dataclass(frozen=True)
class RenderRequest:
    """Complete specification for rendering a single Quran Reel."""

    surah_number: int
    arabic_surah_name: str
    english_surah_name: str
    ayahs: tuple[RenderAyah, ...]
    surah_audio_path: str
    backgrounds: tuple[RenderBackground, ...]
    output_path: str
    seed: Optional[int] = None
    source_lead_ms: int = 0  # Real recorded audio before the annotated first word, not padding.

    @property
    def clip_start_ms(self) -> int:
        first = self.ayahs[0]
        if type(self.source_lead_ms) is not int or not 0 <= self.source_lead_ms <= first.start_ms:
            raise RenderError('Invalid source audio lead.')
        if self.source_lead_ms and not (self.source_lead_ms == 200 and len(self.ayahs) == 1
                and (self.surah_number, first.surah_number, first.ayah_number, first.start_ms, first.end_ms)
                == (1, 1, 1, 300, 3240)):
            raise RenderError('Unreviewed source audio window.')
        return first.start_ms - self.source_lead_ms


@dataclass(frozen=True)
class RenderResult:
    """Metadata and verified properties of a successfully rendered Quran Reel."""

    output_path: str
    duration_seconds: float
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: float = float(DEFAULT_FPS)
    video_codec: str = "h264"
    audio_codec: str = "aac"
    ayah_range: tuple[int, int] = (1, 1)
    pexels_video_ids: tuple[int, ...] = ()
    plans: tuple[BackgroundSegmentPlan, ...] = ()


@dataclass(frozen=True)
class SubtitlePanelPreflight:
    """Authoritative libass fit result for one shared multilingual panel."""

    verse_key: str
    panel_index: int
    arabic_lines: int
    arabic_font_size: int
    translation_lines: tuple[tuple[str, int], ...]
    visible_translation_stack_gaps: tuple[int, ...]


@dataclass(frozen=True)
class SubtitlePreflightResult:
    """All subtitle panels validated before expensive media acquisition."""

    panels: tuple[SubtitlePanelPreflight, ...]
    ayahs: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RenderConfig:
    """Deterministic configuration and typography rules for the FFmpeg renderer."""

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    video_codec: str = DEFAULT_VIDEO_CODEC
    preset: str = DEFAULT_PRESET
    crf: int = DEFAULT_CRF
    pixel_format: str = DEFAULT_PIXEL_FORMAT
    audio_codec: str = DEFAULT_AUDIO_CODEC
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE
    audio_sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE
    crossfade_seconds: float = DEFAULT_CROSSFADE_SECONDS
    min_backgrounds: int = DEFAULT_MIN_BACKGROUNDS
    max_backgrounds: int = DEFAULT_MAX_BACKGROUNDS
    default_backgrounds: int = DEFAULT_BACKGROUNDS_COUNT
    duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS
    overlay_darkness: float = DEFAULT_OVERLAY_DARKNESS
    fonts_dir: Optional[str] = None
    arabic_font_name: str = DEFAULT_ARABIC_FONT_NAME
    arabic_title_font_name: str = DEFAULT_ARABIC_TITLE_FONT_NAME
    english_font_name: str = DEFAULT_ENGLISH_FONT_NAME
    arabic_font_size: int = DEFAULT_ARABIC_FONT_SIZE
    english_font_size: int = DEFAULT_ENGLISH_FONT_SIZE
    arabic_title_font_size: int = DEFAULT_ARABIC_TITLE_FONT_SIZE
    english_title_font_size: int = DEFAULT_ENGLISH_TITLE_FONT_SIZE
    ayah_number_font_size: int = DEFAULT_AYAH_NUMBER_FONT_SIZE
    footnote_font_size: int = DEFAULT_FOOTNOTE_FONT_SIZE
    max_footnote_panel_chars: int = DEFAULT_MAX_FOOTNOTE_PANEL_CHARS
    render_footnotes: bool = True
    subtitle_config: SubtitleConfig = field(default_factory=SubtitleConfig)
    allow_unverified_alignment_preview: bool = False  # Offline human-review tool only.


@dataclass(frozen=True)
class BackgroundSegmentPlan:
    """Computed deterministic time slice for a single background clip."""

    clip_index: int
    local_path: str
    start_offset_seconds: float
    segment_duration_seconds: float
    transition_offset_seconds: Optional[float] = None
    playback_speed: float = 1.0
    trimmed_source_duration_seconds: Optional[float] = None


def resolve_ffmpeg_binary(custom_path: Optional[str] = None) -> str:
    """Resolve executable FFmpeg binary supporting libass."""
    if custom_path and os.path.isfile(custom_path) and os.access(custom_path, os.X_OK):
        return custom_path

    env_path = os.getenv("FFMPEG_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        return env_path

    # Homebrew ffmpeg-full on macOS
    homebrew_full = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    if os.path.isfile(homebrew_full) and os.access(homebrew_full, os.X_OK):
        return homebrew_full

    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path

    raise RenderError("FFmpeg binary not found. Please install ffmpeg with libass.")


def resolve_ffprobe_binary(custom_path: Optional[str] = None) -> str:
    """Resolve executable ffprobe binary."""
    if custom_path and os.path.isfile(custom_path) and os.access(custom_path, os.X_OK):
        return custom_path

    env_path = os.getenv("FFPROBE_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        return env_path

    # Homebrew ffmpeg-full ffprobe on macOS
    homebrew_full = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
    if os.path.isfile(homebrew_full) and os.access(homebrew_full, os.X_OK):
        return homebrew_full

    which_path = shutil.which("ffprobe")
    if which_path:
        return which_path

    raise RenderError("ffprobe binary not found on system.")


def verify_ffmpeg_capabilities(ffmpeg_path: str) -> None:
    """Verify that the resolved FFmpeg binary supports libass and h264/aac codecs."""
    try:
        res = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RenderError(f"Failed to execute FFmpeg at {ffmpeg_path}: returncode {res.returncode}")
        output = (res.stdout + " " + res.stderr).lower()
        if "--enable-libass" not in output:
            filters_res = subprocess.run([ffmpeg_path, "-filters"], capture_output=True, text=True, check=False)
            if "ass" not in filters_res.stdout:
                raise RenderError("FFmpeg does not have libass / ass filter enabled.")
    except Exception as err:
        if isinstance(err, RenderError):
            raise
        raise RenderError(f"Error checking FFmpeg capabilities: {err}") from err


def verify_fonts_available(fonts_dir: str) -> None:
    """Verify that all required typography fonts exist in fonts_dir and match checksums."""
    if not os.path.isdir(fonts_dir):
        raise RenderError(f"Fonts directory not found at: {fonts_dir}")

    for font_file in REQUIRED_FONTS:
        font_path = os.path.join(fonts_dir, font_file)
        if not os.path.isfile(font_path):
            raise RenderError(f"Required font '{font_file}' missing from {fonts_dir}.")
        if not verify_font_file(font_file, fonts_dir):
            raise RenderError(f"Font file '{font_file}' in {fonts_dir} failed SHA-256 checksum verification.")


def format_ass_time(seconds: float) -> str:
    """Format seconds into standard ASS timestamp H:MM:SS.cs (centiseconds)."""
    total_cs = int(round(max(0.0, seconds) * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    """Escape text for safe inclusion in an ASS Dialogue line without mutating source."""
    if not text:
        return ""
    # In ASS, curly braces contain override tags; escape or clean newlines
    escaped = text.replace("{", "\\{").replace("}", "\\}")
    escaped = escaped.replace("\r\n", "\\N").replace("\n", "\\N").replace("\r", "\\N")
    return escaped


def build_ass_text_probe(
    text: str, *, font_name: str, font_size: int, video_width: int,
    video_height: int, margin_sides: int, bold: bool = False,
    fonts_dir: str = str(DEFAULT_FONTS_CACHE_DIR), normalize_arabic: bool = True,
) -> str:
    """Shared native libass probe for production measurements and static preview glyphs."""
    anchor_x = video_width // 2
    anchor_y = max(180, min(320, video_height // 6))
    bold_value = -1 if bold else 0
    escaped_text = escape_ass_text(text)
    probe = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {video_width}",
        f"PlayResY: {video_height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Measure,{font_name},{font_size},&H00FFFFFF,&H000000FF,&HFF000000,&HFF000000,{bold_value},0,0,0,100,100,0,0,1,0,0,8,{margin_sides},{margin_sides},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,0:00:00.00,0:00:01.00,Measure,,0,0,0,,{{\\pos({anchor_x},{anchor_y})}}{escaped_text}",
        "",
    ])

    if normalize_arabic and font_name == DEFAULT_ARABIC_FONT_NAME and not bold:
        geometry = measure_arabic_geometry(probe, font_size, video_width, video_height, fonts_dir)
        original_event = probe.splitlines()[-1]
        events = [
            f"Dialogue: 0,0:00:00.00,0:00:01.00,Measure,,0,0,0,,"
            f"{{\\pos({anchor_x},{anchor_y + line.shift})"
            f"\\clip(0,{line.clip_top + line.shift},{video_width},{line.clip_bottom + line.shift})}}{escaped_text}"
            for line in geometry.lines
        ]
        probe = probe.replace(original_event, "\n".join(events))
    return probe


def arabic_line_position_tags(text: str, *, font_size: int, video_width: int,
                              video_height: int, margin_sides: int, fonts_dir: str,
                              anchor_y: int, anchor_x: int | None = None) -> tuple[str, ...]:
    """Position intact naturally wrapped lines using shared optical geometry."""
    probe = build_ass_text_probe(text, font_name=DEFAULT_ARABIC_FONT_NAME,
        font_size=font_size, video_width=video_width, video_height=video_height,
        margin_sides=margin_sides, fonts_dir=fonts_dir, normalize_arabic=False)
    geometry = measure_arabic_geometry(probe, font_size, video_width, video_height, fonts_dir)
    probe_y = max(180, min(320, video_height // 6))
    delta = anchor_y - probe_y
    return tuple(
        f"\\pos({video_width // 2 if anchor_x is None else anchor_x},{anchor_y + line.shift})"
        f"\\clip(0,{line.clip_top + delta + line.shift},{video_width},{line.clip_bottom + delta + line.shift})"
        for line in geometry.lines
    )


def measure_ass_text_block(
    text: str,
    *,
    font_name: str,
    font_size: int,
    video_width: int,
    video_height: int,
    margin_sides: int,
    fonts_dir: str,
    ffmpeg_bin: str,
    bold: bool = False,
) -> TextBlockBounds:
    """Measure the exact pixels painted by the final libass/font stack.

    The probe uses the same PlayRes, alignment, wrapping margins, font assets,
    and libass implementation as the Reel.  Its result is cached per unique
    text/style tuple so a render never relies on font-specific magic offsets.
    """
    if not text or not text.strip():
        raise RenderError("Cannot measure an empty ASS text block.")

    cache_key = (
        text,
        font_name,
        font_size,
        video_width,
        video_height,
        margin_sides,
        os.path.abspath(fonts_dir),
        os.path.abspath(ffmpeg_bin),
        bold,
    )
    cached = _ASS_TEXT_BOUNDS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    anchor_x = video_width // 2
    anchor_y = max(180, min(320, video_height // 6))
    ass_content = build_ass_text_probe(
        text, font_name=font_name, font_size=font_size, video_width=video_width,
        video_height=video_height, margin_sides=margin_sides, bold=bold, fonts_dir=fonts_dir,
    )

    with tempfile.TemporaryDirectory(prefix="quran_ass_measure_") as measure_dir:
        ass_path = os.path.join(measure_dir, "measure.ass")
        image_path = os.path.join(measure_dir, "measure.png")
        with open(ass_path, "w", encoding="utf-8") as ass_file:
            ass_file.write(ass_content)

        escaped_ass = ass_path.replace(":", "\\:").replace("\\", "/")
        escaped_fonts = fonts_dir.replace(":", "\\:").replace("\\", "/")
        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"color=c=black:s={video_width}x{video_height}:r=1:d=1",
            "-vf", f"ass=filename={escaped_ass}:fontsdir={escaped_fonts}",
            "-frames:v", "1",
            "-y", image_path,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not os.path.isfile(image_path):
            raise RenderError(
                f"Failed to measure ASS text with libass (code {result.returncode}): {result.stderr}"
            )

        try:
            from PIL import Image

            with Image.open(image_path) as image:
                gray = image.convert("L")
                mask = gray.point(lambda value: 255 if value > 16 else 0)
                bbox = mask.getbbox()
                if bbox is None:
                    raise RenderError("libass measurement produced no painted text pixels.")

                left, top, right, bottom = bbox
                occupied_rows = []
                for y in range(top, bottom):
                    if mask.crop((left, y, right, y + 1)).getbbox() is not None:
                        occupied_rows.append(y)
        except RenderError:
            raise
        except Exception as err:
            raise RenderError(f"Failed to inspect libass measurement frame: {err}") from err

    row_runs: list[list[int]] = []
    for y in occupied_rows:
        if not row_runs or y > row_runs[-1][1] + 1:
            row_runs.append([y, y])
        else:
            row_runs[-1][1] = y

    merge_gap = max(4, int(round(font_size * 0.15)))
    merged_runs: list[list[int]] = []
    for run in row_runs:
        if not merged_runs or run[0] - merged_runs[-1][1] - 1 > merge_gap:
            merged_runs.append(run)
        else:
            merged_runs[-1][1] = run[1]

    natural_line_count = len(merged_runs)
    if font_name == DEFAULT_ARABIC_FONT_NAME and not bold:
        raw_probe = build_ass_text_probe(text, font_name=font_name, font_size=font_size,
            video_width=video_width, video_height=video_height, margin_sides=margin_sides,
            fonts_dir=fonts_dir, normalize_arabic=False)
        natural_line_count = len(measure_arabic_geometry(raw_probe, font_size, video_width,
            video_height, fonts_dir).lines)
    bounds = TextBlockBounds(
        left_offset=left - anchor_x,
        top_offset=top - anchor_y,
        right_offset=right - anchor_x,
        bottom_offset=bottom - anchor_y,
        line_count=natural_line_count,
    )
    _ASS_TEXT_BOUNDS_CACHE[cache_key] = bounds
    return bounds


def paginate_footnote_text(
    text: str,
    max_panel_chars: int = DEFAULT_MAX_FOOTNOTE_PANEL_CHARS,
) -> list[str]:
    """Deterministically split footnote text into sequential panels on word boundaries.

    Guarantees:
    - Words are split strictly on whitespace boundaries without truncating words.
    - Reconstructed word sequence matches original words 100% identically.
    - Returns empty list for empty/whitespace input.
    """
    text = text.strip()
    if not text:
        return []

    words = text.split()
    if not words:
        return []

    panels: list[str] = []
    current_words: list[str] = []
    current_len = 0

    for word in words:
        added_len = len(word) if not current_words else len(word) + 1
        if current_words and (current_len + added_len > max_panel_chars):
            panels.append(" ".join(current_words))
            current_words = [word]
            current_len = len(word)
        else:
            current_words.append(word)
            current_len += added_len

    if current_words:
        panels.append(" ".join(current_words))

    return panels


def _resolve_bilingual_ayah(
    ayah: RenderAyah, *, root: Path, english_root: Path, preview: bool,
) -> RenderAyah:
    english, german = ayah.ordered_translations
    alignment = load_bilingual_alignment(
        f"{ayah.surah_number}:{ayah.ayah_number}", ayah.arabic_text, english, german,
        root=root, english_root=english_root, preview=preview,
    )
    if alignment is None:
        if preview:
            raise RenderError("Bilingual preview candidate is missing.")
        return ayah
    mode = (SUBTITLE_MODE_UNVERIFIED_ALIGNMENT_PREVIEW if preview
            else SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE)
    segments = tuple(
        (panel.arabic_text, (("en", panel.english_text), ("de", german_text)))
        for panel, german_text in zip(alignment.english.panels, alignment.german_panels)
    )
    return replace(ayah, subtitle_mode=mode, subtitle_segments=segments,
                   subtitle_provenance={**alignment.provenance, "subtitle_mode": mode})


def resolve_bilingual_alignment_preview(
    ayah: RenderAyah, config: RenderConfig, *, candidate_root: Path, english_root: Optional[Path] = None,
) -> RenderAyah:
    """Resolve a source-validated proposal only for an explicit offline preview."""
    if config.allow_unverified_alignment_preview is not True:
        raise RenderError("UNVERIFIED_ALIGNMENT_PREVIEW_FORBIDDEN")
    if ayah.subtitle_mode is not None or tuple(t.language for t in ayah.ordered_translations) != ("en", "de"):
        raise RenderError("Bilingual preview requires an unresolved English/German ayah.")
    if english_root is None:
        verse = f"{ayah.surah_number}:{ayah.ayah_number}"
        english_root = resolve_alignment_root(verse, DEFAULT_ALIGNMENT_ROOT, PACKAGED_ALIGNMENT_ROOT)
    return _resolve_bilingual_ayah(ayah, root=candidate_root, english_root=english_root, preview=True)


def resolve_render_ayah_subtitle_plan(
    ayah: RenderAyah,
    config: RenderConfig,
    *,
    alignment_root: Optional[Path] = None,
    bilingual_alignment_root: Optional[Path] = None,
) -> RenderAyah:
    """Resolve reviewed subtitles before layout, preserving language-specific fallback."""
    if ayah.subtitle_mode is not None:
        return ayah

    ordered = ayah.ordered_translations
    languages = tuple(item.language for item in ordered)
    if languages == ("en", "de") and config.subtitle_config.subtitle_mode != MODE_STATIC_FULL_AYAH:
        verse = f"{ayah.surah_number}:{ayah.ayah_number}"
        if bilingual_alignment_root is None:
            bilingual_alignment_root = resolve_alignment_root(
                verse, DEFAULT_BILINGUAL_ALIGNMENT_ROOT, PACKAGED_BILINGUAL_ALIGNMENT_ROOT,
            )
        if alignment_root is None:
            alignment_root = (PACKAGED_ALIGNMENT_ROOT if bilingual_alignment_root == PACKAGED_BILINGUAL_ALIGNMENT_ROOT
                              else DEFAULT_ALIGNMENT_ROOT)
        return _resolve_bilingual_ayah(ayah, root=bilingual_alignment_root, english_root=alignment_root, preview=False)
    if languages != ("en",):
        return ayah

    english = ordered[0]
    verse_key = f"{ayah.surah_number}:{ayah.ayah_number}"
    alignment = None
    alignment_state = "missing"
    alignment_error = ""
    if config.subtitle_config.subtitle_mode != MODE_STATIC_FULL_AYAH:
        try:
            kwargs: dict[str, Any] = {}
            if alignment_root is not None:
                kwargs["root"] = alignment_root
            alignment = load_validated_alignment(
                verse_key=verse_key,
                arabic_text=ayah.arabic_text,
                english_text=english.text,
                english_translation_key=english.translation_key,
                english_translation_version=english.translation_version,
                **kwargs,
            )
        except AlignmentValidationError as err:
            alignment_state = "invalid"
            alignment_error = str(err)

    if alignment is not None:
        segments = tuple(
            (
                panel.arabic_text,
                (("en", panel.english_text),),
            )
            for panel in alignment.panels
        )
        return replace(
            ayah,
            subtitle_mode=SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE,
            subtitle_segments=segments,
            subtitle_provenance=alignment.provenance(),
        )

    provenance: dict[str, Any] = {
        "verse_key": verse_key,
        "subtitle_mode": SUBTITLE_MODE_FULL_AYAH_FALLBACK,
        "alignment_status": alignment_state,
        "arabic_source_hash": normalized_sha256(ayah.arabic_text),
        "english_source_hash": normalized_sha256(english.text),
        "english_translation_key": english.translation_key,
        "english_translation_version": english.translation_version,
        "timing_strategy": "authentic_ayah_window",
    }
    if alignment_error:
        provenance["alignment_validation_error"] = alignment_error
    return replace(
        ayah,
        subtitle_mode=SUBTITLE_MODE_FULL_AYAH_FALLBACK,
        subtitle_segments=((ayah.arabic_text, (("en", english.text),)),),
        subtitle_provenance=provenance,
    )


def _build_resolved_phrase_panels(
    ayah: RenderAyah,
    config: RenderConfig,
) -> list[MultilingualPhrasePanel]:
    segments = tuple(
        (
            arabic,
            tuple(TranslationPhrase(language, text) for language, text in translated),
        )
        for arabic, translated in ayah.subtitle_segments
    )
    return build_multilingual_panels_from_segments(
        surah_number=ayah.surah_number,
        ayah_number=ayah.ayah_number,
        segments=segments,
        start_ms=ayah.start_ms,
        end_ms=ayah.end_ms,
        config=config.subtitle_config,
    )


def _select_measured_phrase_plan(
    ayah: RenderAyah,
    config: RenderConfig,
    *,
    fonts_dir: str,
    ffmpeg_bin: str,
) -> tuple[RenderAyah, list[_MeasuredPhrasePanel]]:
    """Measure one authorized plan, preserving its text and timing."""
    sub_cfg = config.subtitle_config
    ayah = resolve_render_ayah_subtitle_plan(ayah, config)
    if (ayah.subtitle_mode == SUBTITLE_MODE_UNVERIFIED_ALIGNMENT_PREVIEW
            and config.allow_unverified_alignment_preview is not True):
        raise RenderError("UNVERIFIED_ALIGNMENT_PREVIEW_FORBIDDEN")
    translation_texts = {
        item.language: item.text
        for item in ayah.ordered_translations
    }
    if ayah.subtitle_mode is not None:
        resolved_panels = _build_resolved_phrase_panels(ayah, config)
        attempts: tuple[tuple[str, Optional[list[MultilingualPhrasePanel]], Optional[int], Optional[int]], ...] = ((
            ayah.subtitle_mode.lower(),
            resolved_panels,
            None,
            None,
        ),)
    else:
        # Existing German development path. English production never reaches it.
        boundaries = ((4, 7), (4, 6), (4, 5), (3, 4), (2, 3), (2, 2))
        attempts = tuple(
            (
                f"target{target_words}-max{maximum_words}",
                None,
                target_words,
                maximum_words,
            )
            for target_words, maximum_words in boundaries
        )
    last_fit_error = "does not fit the mobile-safe subtitle area"
    attempted_boundaries: list[str] = []
    last_language: Optional[str] = None
    last_font_size: Optional[int] = None
    last_allowed_lines: Optional[int] = None
    last_required_lines: Optional[int] = None
    last_panel_index: Optional[int] = None

    for attempt_label, phrase_panels, target_words, maximum_words in attempts:
        if phrase_panels is None:
            phrase_panels = build_multilingual_phrase_panels(
                surah_number=ayah.surah_number,
                ayah_number=ayah.ayah_number,
                arabic_text=ayah.arabic_text,
                translations=translation_texts,
                start_ms=ayah.start_ms,
                end_ms=ayah.end_ms,
                config=sub_cfg,
                target_arabic_words=target_words,
                maximum_arabic_words=maximum_words,
            )
        reconstructed_arabic = [
            word for panel in phrase_panels for word in panel.arabic_text.split()
        ]
        if reconstructed_arabic != ayah.arabic_text.split():
            raise RenderError(
                f"Arabic phrase panels failed exact reconstruction for "
                f"{ayah.surah_number}:{ayah.ayah_number}."
            )
        if not verify_multilingual_reconstruction(phrase_panels, translation_texts):
            raise RenderError(
                f"Translation phrase panels failed exact reconstruction for "
                f"{ayah.surah_number}:{ayah.ayah_number}."
            )

        measured: list[_MeasuredPhrasePanel] = []
        plan_fits = True
        for panel in phrase_panels:
            measure_arabic = partial(
                measure_ass_text_block,
                panel.arabic_text,
                font_name=config.arabic_font_name,
                video_width=config.width,
                video_height=config.height,
                margin_sides=SAFE_MARGIN_SIDES,
                fonts_dir=fonts_dir,
                ffmpeg_bin=ffmpeg_bin,
            )
            preferred_size = sub_cfg.arabic_font_size
            arabic_bounds = measure_arabic(font_size=preferred_size)
            needs_arabic_fallback = sub_cfg.arabic_max_lines == 2 and arabic_bounds.line_count > 2
            translation_bounds = tuple(
                measure_ass_text_block(
                    segment.text,
                    font_name=config.english_font_name,
                    font_size=sub_cfg.translation_font_size,
                    video_width=config.width,
                    video_height=config.height,
                    margin_sides=SAFE_MARGIN_SIDES,
                    fonts_dir=fonts_dir,
                    ffmpeg_bin=ffmpeg_bin,
                )
                for segment in panel.translations
            )
            if arabic_bounds.line_count > sub_cfg.arabic_max_lines and not needs_arabic_fallback:
                last_fit_error = f"Arabic renders as {arabic_bounds.line_count} lines"
                last_language = "ar"
                last_font_size = sub_cfg.arabic_font_size
                last_allowed_lines = sub_cfg.arabic_max_lines
                last_required_lines = arabic_bounds.line_count
                last_panel_index = panel.panel_index
                plan_fits = False
                break
            overflowing_translation = next(
                (
                    (segment.language, bounds.line_count)
                    for segment, bounds in zip(panel.translations, translation_bounds)
                    if bounds.line_count > sub_cfg.line_limit_for_translation(segment.language)
                ),
                None,
            )
            if overflowing_translation is not None:
                language, line_count = overflowing_translation
                last_fit_error = f"{language} translation renders as {line_count} lines"
                last_language = language
                last_font_size = sub_cfg.translation_font_size
                last_allowed_lines = sub_cfg.line_limit_for_translation(language)
                last_required_lines = line_count
                last_panel_index = panel.panel_index
                plan_fits = False
                break

            # If the whole subtitle stack overflows the Safe Zone, retry with
            # smaller Arabic text even when its original line count fits.
            line_limit = 3 if needs_arabic_fallback else sub_cfg.arabic_max_lines
            first_size = (
                max(MIN_ARABIC_FONT_SIZE, preferred_size - ARABIC_FIT_STEP_PX)
                if needs_arabic_fallback else preferred_size
            )
            for candidate_size in range(first_size, MIN_ARABIC_FONT_SIZE - ARABIC_FIT_STEP_PX, -ARABIC_FIT_STEP_PX):
                font_size = max(MIN_ARABIC_FONT_SIZE, candidate_size)
                if font_size != preferred_size:
                    arabic_bounds = measure_arabic(font_size=font_size)
                if arabic_bounds.line_count > line_limit:
                    last_fit_error = f"Arabic renders as {arabic_bounds.line_count} lines"
                    last_language = "ar"
                    last_font_size = font_size
                    last_allowed_lines = line_limit
                    last_required_lines = arabic_bounds.line_count
                    last_panel_index = panel.panel_index
                    continue

                last_language = None
                last_font_size = None
                last_allowed_lines = None
                last_required_lines = None
                last_panel_index = panel.panel_index
                try:
                    layout = calculate_multilingual_subtitle_group_layout(
                        arabic_bounds=arabic_bounds,
                        translation_bounds=translation_bounds,
                        config=sub_cfg,
                        center_y=SUBTITLE_CENTER_Y,
                        video_width=config.width,
                        video_height=config.height,
                    )
                except SubtitleError as error:
                    last_fit_error = str(error)
                    continue
                if (
                    layout.group_top_y < SAFE_MARGIN_TOP
                    or layout.group_bottom_y > config.height - SAFE_MARGIN_BOTTOM
                ):
                    last_fit_error = "exceeds the mobile-safe vertical subtitle area"
                    continue

                measured.append(_MeasuredPhrasePanel(
                    panel=panel,
                    arabic_font_size=font_size,
                    arabic_bounds=arabic_bounds,
                    translation_bounds=translation_bounds,
                    layout=layout,
                ))
                break
            else:
                plan_fits = False
                break

        if plan_fits:
            return ayah, measured
        attempted_boundaries.append(
            f"{attempt_label}:panels{len(phrase_panels)}:{last_fit_error}"
        )

    error_type = (
        AyahRequiresVerifiedAlignmentError
        if ayah.subtitle_mode == SUBTITLE_MODE_FULL_AYAH_FALLBACK
        else SubtitlePreflightError
    )
    raise error_type(
        verse_key=f"{ayah.surah_number}:{ayah.ayah_number}",
        reason=last_fit_error,
        translation_stack_gap_px=(
            None if len(translation_texts) == 1 else sub_cfg.translation_stack_gap_px
        ),
        attempted_boundaries=attempted_boundaries,
        language=last_language,
        font_size=last_font_size,
        allowed_lines=last_allowed_lines,
        required_lines=last_required_lines,
        panel_index=last_panel_index,
    )


def preflight_subtitle_layout(
    request: RenderRequest,
    config: RenderConfig,
    *,
    ffmpeg_bin: Optional[str] = None,
) -> SubtitlePreflightResult:
    """Validate every shared panel with the final libass geometry.

    This is intentionally the same planner and measurement path used by final
    ASS generation.  It performs no audio, background, storage, or publishing
    work and is safe to run immediately after canonical text/timing selection.
    """
    if not request.ayahs:
        raise SubtitlePreflightError(
            verse_key="unknown",
            reason="no ayahs were supplied",
            translation_stack_gap_px=config.subtitle_config.translation_stack_gap_px,
            attempted_boundaries=(),
        )

    fonts_dir = config.fonts_dir or DEFAULT_FONTS_CACHE_DIR
    resolved_ffmpeg = resolve_ffmpeg_binary(ffmpeg_bin)
    verify_fonts_available(fonts_dir)
    results: list[SubtitlePanelPreflight] = []
    ayah_results: list[Mapping[str, Any]] = []
    resolved_ayahs: list[RenderAyah] = []
    for ayah in request.ayahs:
        resolved_ayah, measured_panels = _select_measured_phrase_plan(
            ayah,
            config,
            fonts_dir=fonts_dir,
            ffmpeg_bin=resolved_ffmpeg,
        )
        resolved_ayahs.append(resolved_ayah)
        ayah_results.append(dict(resolved_ayah.subtitle_provenance))
        for measured in measured_panels:
            visible_ranges = measured.layout.translation_visible_ranges
            visible_gaps = tuple(
                visible_ranges[index + 1][0] - visible_ranges[index][1]
                for index in range(len(visible_ranges) - 1)
            )
            results.append(SubtitlePanelPreflight(
                verse_key=measured.panel.verse_key,
                panel_index=measured.panel.panel_index,
                arabic_lines=measured.arabic_bounds.line_count,
                arabic_font_size=measured.arabic_font_size,
                translation_lines=tuple(
                    (segment.language, bounds.line_count)
                    for segment, bounds in zip(
                        measured.panel.translations,
                        measured.translation_bounds,
                    )
                ),
                visible_translation_stack_gaps=visible_gaps,
            ))

    # Exercise the complete final ASS path now as well, including header and
    # footnote construction. The same cached libass block measurements are
    # reused later by the final renderer, so no new layout failure is deferred
    # until after media acquisition.
    generate_ass_subtitles(
        replace(request, ayahs=tuple(resolved_ayahs)),
        config,
        ffmpeg_bin=resolved_ffmpeg,
    )
    return SubtitlePreflightResult(
        panels=tuple(results),
        ayahs=tuple(ayah_results),
    )


def generate_ass_subtitles(
    request: RenderRequest,
    config: RenderConfig,
    *,
    ffmpeg_bin: Optional[str] = None,
) -> str:
    """Generate complete ASS subtitle content for the requested Reel.

    Supports two modes via config.subtitle_config.subtitle_mode:
    - progressive_phrase_sync: Short phrase panels with fade transitions
    - static_full_ayah: Legacy full-ayah blocks
    """
    if not request.ayahs:
        raise RenderError("Cannot generate ASS subtitles with empty ayahs.")

    sub_cfg = config.subtitle_config
    first_ayah = request.ayahs[0]
    last_ayah = request.ayahs[-1]
    reel_duration = (last_ayah.end_ms - request.clip_start_ms) / 1000.0
    end_ts_str = format_ass_time(reel_duration)

    fade_ms = sub_cfg.transition_duration_ms
    fade_tag = f"\\fad({fade_ms},{fade_ms})"

    # Outline configuration (minimal / thin / medium / none) — clean, non-cartoon look
    from engine.subtitles import (
        OUTLINE_NONE, OUTLINE_MINIMAL, OUTLINE_THIN, OUTLINE_MEDIUM,
        SHADOW_OFF, SHADOW_VERY_SOFT, SHADOW_SOFT, SHADOW_MEDIUM,
        INTENSITY_SUBTLE, INTENSITY_NORMAL, INTENSITY_STRONG,
        HEADER_BACKING_NONE, HEADER_BACKING_LOW, HEADER_BACKING_MEDIUM, HEADER_BACKING_HIGH,
        HEADER_MINIMAL_CAPSULE, HEADER_SOFT_GLASS, HEADER_PLAIN_TEXT,
        BACKING_TRANSLUCENT, BACKING_LOCALIZED,
        HIGHLIGHT_COLORS,
        calculate_subtitle_group_layout,
    )

    fonts_dir = config.fonts_dir or DEFAULT_FONTS_CACHE_DIR
    resolved_measure_ffmpeg = resolve_ffmpeg_binary(ffmpeg_bin)
    verify_fonts_available(fonts_dir)

    # Amiri Quran has no genuine bold face.  Its outline is therefore a tiny
    # same-colour expansion for strength, while English retains a restrained
    # semi-transparent dark separation outline.
    if sub_cfg.text_outline == OUTLINE_NONE:
        ar_border = 0.0
        en_border = 0.0
    elif sub_cfg.text_outline == OUTLINE_MINIMAL:
        ar_border = 0.55
        en_border = 0.45
    elif sub_cfg.text_outline == OUTLINE_THIN:
        ar_border = 0.75
        en_border = 0.70
    else:  # OUTLINE_MEDIUM
        ar_border = 1.0
        en_border = 1.0

    # ASS alpha is transparency (00 opaque, FF transparent).
    if sub_cfg.text_shadow == SHADOW_OFF:
        ar_shadow = 0
        en_shadow = 0
        shadow_alpha = "FF"
    elif sub_cfg.text_shadow == SHADOW_VERY_SOFT:
        ar_shadow = 0.7
        en_shadow = 0.7
        shadow_alpha = "D0"  # about 18% opacity
    elif sub_cfg.text_shadow == SHADOW_SOFT:
        ar_shadow = 1
        en_shadow = 1
        shadow_alpha = "B8"
    else:  # SHADOW_MEDIUM
        ar_shadow = 1.5
        en_shadow = 1
        shadow_alpha = "90"

    # Backing alphas are intentionally light; they must never become dark cards.
    if sub_cfg.header_backing_intensity == HEADER_BACKING_LOW:
        header_back_alpha = "E0"
    elif sub_cfg.header_backing_intensity == HEADER_BACKING_MEDIUM:
        header_back_alpha = "CC"
    else:
        header_back_alpha = "B8"

    border_style = 1
    back_col = f"&H{shadow_alpha}000000"
    english_outline_col = "&HC0000000"
    header_outline_col = "&HC8000000"

    # Highlight intensity and active color
    active_color = HIGHLIGHT_COLORS.get(sub_cfg.highlight_color, "&H00F8FDFF")
    if sub_cfg.highlight_intensity == INTENSITY_SUBTLE:
        ar_primary = active_color
        en_primary = "&H00D8DFE4"
    elif sub_cfg.highlight_intensity == INTENSITY_STRONG:
        ar_primary = active_color
        en_primary = "&H00F4F4F4"
    else:  # INTENSITY_NORMAL
        ar_primary = active_color
        en_primary = "&H00E0E6EC"

    header_ar_primary = "&H00FFFFFF"
    header_en_primary = "&H00E0E6EC"

    # Header text
    header_ar, header_en = format_header_text(
        arabic_surah_name=request.arabic_surah_name,
        english_surah_name=request.english_surah_name,
        first_ayah=first_ayah.ayah_number,
        last_ayah=last_ayah.ayah_number,
        surah_number=request.surah_number,
        config=sub_cfg,
    )

    center_x = config.width // 2
    header_ar_bounds = (
        measure_ass_text_block(
            header_ar,
            font_name=config.arabic_title_font_name,
            font_size=config.arabic_title_font_size,
            video_width=config.width,
            video_height=config.height,
            margin_sides=SAFE_MARGIN_SIDES,
            fonts_dir=fonts_dir,
            ffmpeg_bin=resolved_measure_ffmpeg,
            bold=True,
        )
        if header_ar
        else None
    )
    header_en_bounds = (
        measure_ass_text_block(
            header_en,
            font_name=config.english_font_name,
            font_size=config.english_title_font_size,
            video_width=config.width,
            video_height=config.height,
            margin_sides=SAFE_MARGIN_SIDES,
            fonts_dir=fonts_dir,
            ffmpeg_bin=resolved_measure_ffmpeg,
        )
        if header_en
        else None
    )

    header_visible_top = sura_header_top(sub_cfg.sura_header_top_offset)
    header_ar_visible_top = header_visible_top
    header_ar_anchor_y = (
        header_ar_visible_top - header_ar_bounds.top_offset
        if header_ar_bounds is not None
        else header_visible_top
    )
    header_ar_visible_bottom = (
        header_ar_visible_top + header_ar_bounds.height
        if header_ar_bounds is not None
        else header_ar_visible_top
    )
    header_en_visible_top = (
        header_ar_visible_bottom + DEFAULT_HEADER_GAP_PX
        if header_ar_bounds is not None and header_en_bounds is not None
        else header_visible_top
    )
    header_en_anchor_y = (
        header_en_visible_top - header_en_bounds.top_offset
        if header_en_bounds is not None
        else header_en_visible_top
    )
    header_en_visible_bottom = (
        header_en_visible_top + header_en_bounds.height
        if header_en_bounds is not None
        else header_en_visible_top
    )

    safe = safe_rectangle(config.width, config.height)
    for bound in (header_ar_bounds, header_en_bounds):
        if bound and (center_x + bound.left_offset < safe['x'] or center_x + bound.right_offset > safe['right']):
            raise RenderError("Sura header exceeds the fixed horizontal text Safe Zone.")
    if max(header_ar_visible_bottom, header_en_visible_bottom) > safe['bottom']:
        raise RenderError("Sura header exceeds the fixed vertical text Safe Zone.")

    lines = [
        "[Script Info]",
        "Title: Quran Reel Subtitles",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        f"PlayResX: {config.width}",
        f"PlayResY: {config.height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Drawing styles for translucent backing shapes
        "Style: BackingCard,Arial,10,&H0014181E,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        # Header styles — positioned via unified \\pos
        f"Style: SurahHeaderAr,{config.arabic_title_font_name},{config.arabic_title_font_size},{header_ar_primary},&H000000FF,{header_outline_col},{back_col},-1,0,0,0,100,100,0,0,1,0.55,{ar_shadow},8,{SAFE_MARGIN_SIDES},{SAFE_MARGIN_SIDES},0,1",
        f"Style: SurahHeaderEn,{config.english_font_name},{config.english_title_font_size},{header_en_primary},&H000000FF,{header_outline_col},{back_col},0,0,0,0,100,100,0,0,1,0.45,{en_shadow},8,{SAFE_MARGIN_SIDES},{SAFE_MARGIN_SIDES},0,1",
        # Amiri Quran has no bold face; a sub-pixel same-colour edge expansion
        # strengthens the canonical glyph without synthetic-bold distortion.
        f"Style: QuranPhraseAr,{config.arabic_font_name},{sub_cfg.arabic_font_size},{ar_primary},&H000000FF,{ar_primary},{back_col},0,0,0,0,100,100,0,0,{border_style},{ar_border},{ar_shadow},8,{SAFE_MARGIN_SIDES},{SAFE_MARGIN_SIDES},0,1",
        # Translation — top-center aligned with dynamic \\pos
        f"Style: QuranPhraseTr,{config.english_font_name},{sub_cfg.translation_font_size},{en_primary},&H000000FF,{english_outline_col},{back_col},0,0,0,0,100,100,0,0,{border_style},{en_border},{en_shadow},8,{SAFE_MARGIN_SIDES},{SAFE_MARGIN_SIDES},0,1",
        # Footnote — below translation
        f"Style: FootnoteEnglish,{config.english_font_name},{config.footnote_font_size},&H00D0D0D0,&H000000FF,{english_outline_col},{back_col},0,0,0,0,100,100,0,0,{border_style},{en_border},{en_shadow},2,120,120,240,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # Header events (visible for entire Reel duration)
    if (sub_cfg.header_style in (HEADER_MINIMAL_CAPSULE, HEADER_SOFT_GLASS)
            and sub_cfg.header_backing_intensity != HEADER_BACKING_NONE
            and (header_ar or header_en)):
        header_bounds = [bound for bound in (header_ar_bounds, header_en_bounds) if bound is not None]
        header_content_left = center_x + min(bound.left_offset for bound in header_bounds)
        header_content_right = center_x + max(bound.right_offset for bound in header_bounds)
        header_content_bottom = max(
            header_ar_visible_bottom if header_ar_bounds is not None else header_visible_top,
            header_en_visible_bottom if header_en_bounds is not None else header_visible_top,
        )
        capsule_x = max(SAFE_MARGIN_SIDES, header_content_left - 24)
        capsule_y = max(0, header_visible_top - 14)
        capsule_right = min(config.width - SAFE_MARGIN_SIDES, header_content_right + 24)
        capsule_bottom = header_content_bottom + 14
        capsule_w = capsule_right - capsule_x
        capsule_h = capsule_bottom - capsule_y
        lines.append(
            f"Dialogue: 5,0:00:00.00,{end_ts_str},BackingCard,,0,0,0,,{{\\pos({capsule_x},{capsule_y})\\1a&H{header_back_alpha}&\\blur4\\p1}}m 0 0 l {capsule_w} 0 l {capsule_w} {capsule_h} l 0 {capsule_h}{{\\p0}}"
        )

    if header_ar:
        escaped_header_ar = escape_ass_text(header_ar)
        lines.append(f"Dialogue: 10,0:00:00.00,{end_ts_str},SurahHeaderAr,,0,0,0,,{{\\pos({center_x},{header_ar_anchor_y})}}{escaped_header_ar}")
    if header_en:
        escaped_header_en = escape_ass_text(header_en)
        lines.append(f"Dialogue: 10,0:00:00.00,{end_ts_str},SurahHeaderEn,,0,0,0,,{{\\pos({center_x},{header_en_anchor_y})}}{escaped_header_en}")

    max_panel_chars = config.max_footnote_panel_chars

    for ayah in request.ayahs:
        rel_start_s = (ayah.start_ms - request.clip_start_ms) / 1000.0
        rel_end_s = (ayah.end_ms - request.clip_start_ms) / 1000.0
        ayah_duration_s = max(0.01, rel_end_s - rel_start_s)

        ordered_translations = ayah.ordered_translations
        _, measured_panels = _select_measured_phrase_plan(
            ayah,
            config,
            fonts_dir=fonts_dir,
            ffmpeg_bin=resolved_measure_ffmpeg,
        )

        for measured_panel in measured_panels:
            panel = measured_panel.panel
            arabic_bounds = measured_panel.arabic_bounds
            translation_bounds = measured_panel.translation_bounds
            layout = measured_panel.layout
            p_start_rel = max(0.0, (panel.visual_start_ms - request.clip_start_ms) / 1000.0)
            p_end_rel = max(p_start_rel + 0.1, (panel.visual_end_ms - request.clip_start_ms) / 1000.0)
            p_start_ts = format_ass_time(p_start_rel)
            p_end_ts = format_ass_time(p_end_rel)

            # Center the measured bilingual unit once. The translation is not
            # independently recentered or offset after this calculation.
            # Localized soft backing card behind subtitle block
            if sub_cfg.subtitle_backing in (BACKING_LOCALIZED, BACKING_TRANSLUCENT):
                bk_left, bk_top, bk_right, bk_bottom = layout.backing_rect
                bk_w = bk_right - bk_left
                bk_h = bk_bottom - bk_top
                backing_alpha = "D8" if sub_cfg.subtitle_backing == BACKING_LOCALIZED else "C0"
                lines.append(
                    f"Dialogue: 1,{p_start_ts},{p_end_ts},BackingCard,,0,0,0,,{{\\pos({bk_left},{bk_top})\\1a&H{backing_alpha}&\\blur6{fade_tag}\\p1}}m 0 0 l {bk_w} 0 l {bk_w} {bk_h} l 0 {bk_h}{{\\p0}}"
                )

            escaped_ar = escape_ass_text(panel.arabic_text)
            ar_font_size = measured_panel.arabic_font_size
            ar_size_tag = f"\\fs{ar_font_size}" if ar_font_size != sub_cfg.arabic_font_size else ""
            ar_positions = arabic_line_position_tags(panel.arabic_text,
                font_size=ar_font_size, video_width=config.width,
                video_height=config.height, margin_sides=SAFE_MARGIN_SIDES,
                fonts_dir=fonts_dir, anchor_y=layout.arabic_anchor_y, anchor_x=layout.anchor_x)
            for ar_pos_tag in ar_positions:
                ar_fade = fade_tag if sub_cfg.effective_highlight_style == HIGHLIGHT_PHRASE_FADE else ""
                lines.append(f"Dialogue: 10,{p_start_ts},{p_end_ts},QuranPhraseAr,,0,0,0,,{{{ar_size_tag}{ar_pos_tag}{ar_fade}}}{escaped_ar}")
            for segment, anchor_y in zip(panel.translations, layout.translation_anchor_ys):
                escaped_translation = escape_ass_text(segment.text)
                translation_pos = f"\\pos({layout.anchor_x},{anchor_y})"
                if sub_cfg.effective_highlight_style == HIGHLIGHT_PHRASE_FADE:
                    lines.append(
                        f"Dialogue: 10,{p_start_ts},{p_end_ts},QuranPhraseTr,,0,0,0,,"
                        f"{{{translation_pos}{fade_tag}}}{escaped_translation}"
                    )
                else:
                    lines.append(
                        f"Dialogue: 10,{p_start_ts},{p_end_ts},QuranPhraseTr,,0,0,0,,"
                        f"{{{translation_pos}}}{escaped_translation}"
                    )

        # Preserve each enabled Rowwad source's exact footnotes.
        footnote_items = [
            item for item in ordered_translations
            if item.footnotes and item.footnotes.strip()
        ]
        footnote_pages: list[tuple[str, int, int, str]] = []
        for footnote_item in footnote_items if config.render_footnotes else ():
            fn_panels = paginate_footnote_text(
                footnote_item.footnotes.strip(),
                max_panel_chars=max_panel_chars,
            )
            language_label = (
                ""
                if len(footnote_items) == 1 and not ayah.translations
                else f"{footnote_item.language.upper()} "
            )
            footnote_pages.extend(
                (language_label, page_index, len(fn_panels), page)
                for page_index, page in enumerate(fn_panels, start=1)
            )

        # Multiple language notes are time-multiplexed instead of painted on
        # top of each other. Every exact source page remains visible once.
        if footnote_pages:
            page_duration_s = ayah_duration_s / float(len(footnote_pages))
            for global_index, (language_label, page_index, language_total, page) in enumerate(footnote_pages):
                page_start = rel_start_s + (global_index * page_duration_s)
                page_end = (
                    rel_start_s + ((global_index + 1) * page_duration_s)
                    if global_index < len(footnote_pages) - 1
                    else rel_end_s
                )
                page_label = (
                    f"{language_label}Note"
                    if language_total == 1
                    else f"{language_label}Note ({page_index}/{language_total})"
                )
                lines.append(
                    f"Dialogue: 0,{format_ass_time(page_start)},{format_ass_time(page_end)},"
                    f"FootnoteEnglish,,0,0,0,,[{page_label}: {escape_ass_text(page)}]"
                )

    return "\n".join(lines) + "\n"


def calculate_background_segments(
    request: RenderRequest,
    config: RenderConfig,
    rng: Optional[random.Random] = None,
) -> list[BackgroundSegmentPlan]:
    """Compute deterministic segment allocations, start offsets, and xfade transition points."""
    num_clips = len(request.backgrounds)
    if num_clips < config.min_backgrounds or num_clips > config.max_backgrounds:
        raise RenderError(
            f"Number of background clips ({num_clips}) must be between "
            f"{config.min_backgrounds} and {config.max_backgrounds}."
        )

    first_ayah = request.ayahs[0]
    last_ayah = request.ayahs[-1]
    target_duration = (last_ayah.end_ms - request.clip_start_ms) / 1000.0

    if target_duration <= 0:
        raise RenderError(f"Invalid non-positive target duration: {target_duration}s.")

    generator = rng if rng is not None else (
        random.Random(request.seed) if request.seed is not None else random.Random()
    )

    num_crossfades = num_clips - 1
    total_overlap = num_crossfades * config.crossfade_seconds
    total_source_needed = target_duration + total_overlap
    segment_duration = total_source_needed / float(num_clips)

    plans: list[BackgroundSegmentPlan] = []
    for idx, bg in enumerate(request.backgrounds):
        if bg.source_duration_seconds >= segment_duration:
            playback_speed = 1.0
            trimmed_source = segment_duration
            slack = bg.source_duration_seconds - segment_duration
            start_offset = generator.uniform(0.0, slack) if slack > 0.0 else 0.0
        else:
            required_speed = bg.source_duration_seconds / segment_duration
            if required_speed < 0.5:
                raise RenderError(
                    f"Background clip {idx} ({bg.local_path}) duration ({bg.source_duration_seconds:.2f}s) "
                    f"cannot cover required segment duration ({segment_duration:.2f}s) even at minimum "
                    f"0.5x playback speed (required {required_speed:.3f}x < 0.5x)."
                )
            playback_speed = required_speed
            trimmed_source = bg.source_duration_seconds
            start_offset = 0.0

        transition_offset: Optional[float] = None
        if idx > 0:
            # offset_k = k * (segment_duration - crossfade_seconds)
            transition_offset = idx * (segment_duration - config.crossfade_seconds)

        plans.append(
            BackgroundSegmentPlan(
                clip_index=idx,
                local_path=bg.local_path,
                start_offset_seconds=start_offset,
                segment_duration_seconds=segment_duration,
                transition_offset_seconds=transition_offset,
                playback_speed=playback_speed,
                trimmed_source_duration_seconds=trimmed_source,
            )
        )

    return plans


def validate_render_request(
    request: RenderRequest,
    config: RenderConfig,
    fonts_dir: str,
) -> None:
    """Validate all aspects of the render request before launching FFmpeg."""
    if not isinstance(request, RenderRequest):
        raise RenderError("request must be a RenderRequest instance.")

    if not isinstance(request.surah_number, int) or request.surah_number < 1 or request.surah_number > 114:
        raise RenderError(f"Invalid Surah number {request.surah_number} (must be 1..114).")

    if not request.arabic_surah_name or not request.arabic_surah_name.strip():
        raise RenderError("arabic_surah_name must be a non-empty string.")

    if not request.english_surah_name or not request.english_surah_name.strip():
        raise RenderError("english_surah_name must be a non-empty string.")

    if not request.ayahs or not isinstance(request.ayahs, (tuple, list)):
        raise RenderError("ayahs must be a non-empty sequence of RenderAyah objects.")

    # Validate ayah sequence, timings, and footnote policy
    previous_ayah: Optional[RenderAyah] = None
    for idx, ayah in enumerate(request.ayahs):
        if not isinstance(ayah, RenderAyah):
            raise RenderError(f"Ayah at index {idx} must be a RenderAyah instance.")
        if ayah.surah_number != request.surah_number:
            raise RenderError(
                f"Ayah at index {idx} has surah_number {ayah.surah_number}, expected {request.surah_number}."
            )
        if not isinstance(ayah.arabic_text, str) or not ayah.arabic_text.strip():
            raise RenderError(f"Ayah {ayah.surah_number}:{ayah.ayah_number} has empty arabic_text.")
        if not ayah.translations and (
            not isinstance(ayah.english_text, str) or not ayah.english_text.strip()
        ):
            raise RenderError(f"Ayah {ayah.surah_number}:{ayah.ayah_number} has no translation text.")
        translations = ayah.ordered_translations
        if not translations:
            raise RenderError(f"Ayah {ayah.surah_number}:{ayah.ayah_number} has no translations.")
        if ayah.start_ms < 0 or ayah.end_ms <= ayah.start_ms:
            raise RenderError(
                f"Ayah {ayah.surah_number}:{ayah.ayah_number} has invalid timing: {ayah.start_ms}..{ayah.end_ms} ms."
            )

        # Rowwad footnotes policy (Decision D-011): footnotes are rendered faithfully in ASS subtitles
        if not isinstance(ayah.footnotes, str):
            raise RenderError(f"Ayah {ayah.surah_number}:{ayah.ayah_number} footnotes must be a string.")

        if previous_ayah is not None:
            if ayah.ayah_number != previous_ayah.ayah_number + 1:
                raise RenderError(
                    f"Non-consecutive ayah numbering: expected {previous_ayah.ayah_number + 1}, got {ayah.ayah_number}."
                )
            if ayah.start_ms < previous_ayah.end_ms:
                raise RenderError(
                    f"Ayah {ayah.surah_number}:{ayah.ayah_number} start_time ({ayah.start_ms}ms) overlaps "
                    f"previous ayah end_time ({previous_ayah.end_ms}ms)."
                )

        previous_ayah = ayah

    if not request.surah_audio_path or not os.path.isfile(request.surah_audio_path):
        raise RenderError(f"Surah audio file not found at: {request.surah_audio_path}")

    if os.path.getsize(request.surah_audio_path) <= 0:
        raise RenderError(f"Surah audio file is empty (0 bytes): {request.surah_audio_path}")

    if not request.backgrounds or not isinstance(request.backgrounds, (tuple, list)):
        raise RenderError("backgrounds must be a non-empty sequence of RenderBackground objects.")

    if len(request.backgrounds) < config.min_backgrounds or len(request.backgrounds) > config.max_backgrounds:
        raise RenderError(
            f"Backgrounds count ({len(request.backgrounds)}) must be between "
            f"{config.min_backgrounds} and {config.max_backgrounds}."
        )

    for idx, bg in enumerate(request.backgrounds):
        if not isinstance(bg, RenderBackground):
            raise RenderError(f"Background at index {idx} must be a RenderBackground instance.")
        if not os.path.isfile(bg.local_path):
            raise RenderError(f"Background file not found at: {bg.local_path}")
        if os.path.getsize(bg.local_path) <= 0:
            raise RenderError(f"Background file is empty (0 bytes): {bg.local_path}")

    verify_fonts_available(fonts_dir)


def build_ffmpeg_command(
    request: RenderRequest,
    segment_plans: Sequence[BackgroundSegmentPlan],
    ass_path: str,
    fonts_dir: str,
    output_path: str,
    config: RenderConfig,
    ffmpeg_bin: str,
) -> list[str]:
    """Build exact FFmpeg CLI command with complex filter graph."""
    first_ayah = request.ayahs[0]
    last_ayah = request.ayahs[-1]
    audio_start_sec = request.clip_start_ms / 1000.0
    audio_end_sec = last_ayah.end_ms / 1000.0
    target_duration = audio_end_sec - audio_start_sec

    num_clips = len(segment_plans)
    cmd = [ffmpeg_bin]

    # Add background video inputs (inputs 0..N-1)
    for plan in segment_plans:
        cmd.extend(["-i", plan.local_path])

    # Add Quran audio input (input N)
    cmd.extend(["-i", request.surah_audio_path])

    # Construct complex filter graph
    filter_chains = []

    # 1. Video trimming, scaling, and cropping for each clip
    for idx, plan in enumerate(segment_plans):
        pts_expr = (
            "setpts=PTS-STARTPTS,"
            if abs(plan.playback_speed - 1.0) < 1e-4
            else f"setpts=(PTS-STARTPTS)/{plan.playback_speed:.4f},"
        )
        trimmed_dur = (
            plan.trimmed_source_duration_seconds
            if plan.trimmed_source_duration_seconds is not None
            else plan.segment_duration_seconds * plan.playback_speed
        )
        filter_chains.append(
            f"[{idx}:v]trim=start={plan.start_offset_seconds:.4f}:duration={trimmed_dur:.4f},"
            f"{pts_expr}"
            f"scale={config.width}:{config.height}:force_original_aspect_ratio=increase:out_range=limited,"
            f"crop={config.width}:{config.height},"
            f"setsar=1,fps={config.fps},format={config.pixel_format}[v{idx}]"
        )

    # 2. Crossfade chaining
    current_label = "[v0]"
    for idx in range(1, num_clips):
        plan = segment_plans[idx]
        next_label = f"[v{idx}]"
        out_label = "[vxf]" if idx == num_clips - 1 else f"[vx{idx}]"
        filter_chains.append(
            f"{current_label}{next_label}xfade=transition=fade:duration={config.crossfade_seconds:.2f}:"
            f"offset={plan.transition_offset_seconds:.4f}{out_label}"
        )
        current_label = out_label

    # 3. Readability overlay and ASS subtitles burn-in
    escaped_ass = ass_path.replace(":", "\\:").replace("\\", "/")
    escaped_fonts = fonts_dir.replace(":", "\\:").replace("\\", "/")

    filter_chains.append(
        f"{current_label}drawbox=x=0:y=0:w={config.width}:h={config.height}:"
        f"color=black@{config.overlay_darkness:.2f}:t=fill,"
        f"ass=filename={escaped_ass}:fontsdir={escaped_fonts},"
        f"format={config.pixel_format}[vout]"
    )

    # 4. Audio trimming (Quran recitation ONLY; background audio completely discarded)
    audio_input_idx = num_clips
    filter_chains.append(
        f"[{audio_input_idx}:a]atrim=start={audio_start_sec:.4f}:end={audio_end_sec:.4f},"
        f"asetpts=PTS-STARTPTS,"
        f"aformat=sample_rates={config.audio_sample_rate}:channel_layouts=stereo[aout]"
    )

    filter_complex_str = ";".join(filter_chains)

    cmd.extend([
        "-filter_complex", filter_complex_str,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", config.video_codec,
        "-preset", config.preset,
        "-crf", str(config.crf),
        "-pix_fmt", config.pixel_format,
        "-c:a", config.audio_codec,
        "-b:a", config.audio_bitrate,
        "-ar", str(config.audio_sample_rate),
        "-movflags", "+faststart",
        "-t", f"{target_duration:.4f}",
        "-y", output_path,
    ])

    return cmd


def validate_rendered_output(
    output_path: str,
    expected_duration_seconds: float,
    config: RenderConfig,
    ffprobe_bin: Optional[str] = None,
) -> dict:
    """Validate the output Reel file using ffprobe against all strict quality rules."""
    if not os.path.isfile(output_path):
        raise RenderError(f"Rendered output file does not exist: {output_path}")

    file_size = os.path.getsize(output_path)
    if file_size <= 0:
        raise RenderError(f"Rendered output file is empty (0 bytes): {output_path}")

    probe_path = ffprobe_bin or resolve_ffprobe_binary()
    probe_cmd = [
        probe_path,
        "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,pix_fmt,duration",
        "-show_entries", "format=duration,size,format_name",
        "-of", "json",
        output_path,
    ]

    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RenderError(f"ffprobe failed with returncode {res.returncode}: {res.stderr}")
        data = json.loads(res.stdout)
    except Exception as err:
        if isinstance(err, RenderError):
            raise
        raise RenderError(f"Failed to inspect output with ffprobe: {err}") from err

    format_info = data.get("format", {})
    format_name = format_info.get("format_name", "")
    if "mp4" not in format_name and "mov" not in format_name:
        raise RenderError(f"Invalid output container format: '{format_name}'. Expected mp4.")

    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if len(video_streams) != 1:
        raise RenderError(f"Expected exactly 1 video stream, found {len(video_streams)}.")

    if len(audio_streams) != 1:
        raise RenderError(f"Expected exactly 1 audio stream (Quran audio only), found {len(audio_streams)}.")

    v_stream = video_streams[0]
    a_stream = audio_streams[0]

    # Validate video properties
    v_codec = str(v_stream.get("codec_name", "")).lower()
    if v_codec != "h264":
        raise RenderError(f"Invalid video codec '{v_codec}'. Expected h264.")

    width = int(v_stream.get("width", 0))
    height = int(v_stream.get("height", 0))
    if width != config.width or height != config.height:
        raise RenderError(f"Invalid video resolution {width}x{height}. Expected {config.width}x{config.height}.")

    pix_fmt = str(v_stream.get("pix_fmt", "")).lower()
    if pix_fmt != config.pixel_format:
        raise RenderError(f"Invalid pixel format '{pix_fmt}'. Expected {config.pixel_format}.")

    # Validate audio properties
    a_codec = str(a_stream.get("codec_name", "")).lower()
    if a_codec != "aac":
        raise RenderError(f"Invalid audio codec '{a_codec}'. Expected aac.")

    sample_rate = int(a_stream.get("sample_rate", 0))
    if sample_rate != config.audio_sample_rate:
        raise RenderError(f"Invalid audio sample rate {sample_rate}. Expected {config.audio_sample_rate}.")

    # Validate duration against authentic audio duration
    reported_duration = float(format_info.get("duration", 0.0))
    diff = abs(reported_duration - expected_duration_seconds)
    if diff > config.duration_tolerance_seconds:
        raise RenderError(
            f"Rendered duration {reported_duration:.3f}s differs from expected {expected_duration_seconds:.3f}s "
            f"by {diff:.3f}s (tolerance: {config.duration_tolerance_seconds:.3f}s)."
        )

    return data


def render_reel(
    request: RenderRequest,
    config: Optional[RenderConfig] = None,
    rng: Optional[random.Random] = None,
    ffmpeg_bin: Optional[str] = None,
    ffprobe_bin: Optional[str] = None,
) -> RenderResult:
    """Render a deterministic 1080x1920 Quran Reel video from validated local assets.

    Fail-closed: Cleans up any partial output file if rendering or validation fails.
    """
    resolved_config = config or RenderConfig()
    fonts_dir = resolved_config.fonts_dir or DEFAULT_FONTS_CACHE_DIR

    resolved_ffmpeg = resolve_ffmpeg_binary(ffmpeg_bin)
    resolved_ffprobe = resolve_ffprobe_binary(ffprobe_bin)

    verify_ffmpeg_capabilities(resolved_ffmpeg)
    validate_render_request(request, resolved_config, fonts_dir)

    first_ayah = request.ayahs[0]
    last_ayah = request.ayahs[-1]
    expected_duration = (last_ayah.end_ms - request.clip_start_ms) / 1000.0

    segment_plans = calculate_background_segments(request, resolved_config, rng=rng)

    # Generate temporary ASS subtitle file
    temp_dir = tempfile.gettempdir()
    ass_fd, ass_path = tempfile.mkstemp(dir=temp_dir, prefix="quran_reel_", suffix=".ass")
    try:
        ass_content = generate_ass_subtitles(request, resolved_config, ffmpeg_bin=resolved_ffmpeg)
        with os.fdopen(ass_fd, "w", encoding="utf-8") as f_ass:
            f_ass.write(ass_content)

        output_dir = os.path.dirname(os.path.abspath(request.output_path))
        os.makedirs(output_dir, exist_ok=True)

        cmd = build_ffmpeg_command(
            request=request,
            segment_plans=segment_plans,
            ass_path=ass_path,
            fonts_dir=fonts_dir,
            output_path=request.output_path,
            config=resolved_config,
            ffmpeg_bin=resolved_ffmpeg,
        )

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            if os.path.exists(request.output_path):
                try:
                    os.remove(request.output_path)
                except OSError:
                    pass
            raise RenderError(f"FFmpeg rendering failed with code {res.returncode}: {res.stderr}")

        probe = validate_rendered_output(
            output_path=request.output_path,
            expected_duration_seconds=expected_duration,
            config=resolved_config,
            ffprobe_bin=resolved_ffprobe,
        )
        if request.source_lead_ms:
            # The normal tolerance alone is insufficient for a near-minimum Reel.
            lengths = [float(s.get('duration', 0)) for s in probe['streams']
                       if s.get('codec_type') in ('audio', 'video')]
            if len(lengths) != 2 or not all(3.0 <= length < float('inf') for length in lengths):
                raise RenderError('Reviewed opening: both encoded tracks must meet the 3-second floor.')

    except Exception:
        # Clean up partial output on failure
        if os.path.exists(request.output_path):
            try:
                os.remove(request.output_path)
            except OSError:
                pass
        raise
    finally:
        # Always remove temporary ASS subtitle file
        if os.path.exists(ass_path):
            try:
                os.remove(ass_path)
            except OSError:
                pass

    return RenderResult(
        output_path=request.output_path,
        duration_seconds=expected_duration,
        width=resolved_config.width,
        height=resolved_config.height,
        fps=float(resolved_config.fps),
        video_codec="h264",
        audio_codec="aac",
        ayah_range=(first_ayah.ayah_number, last_ayah.ayah_number),
        pexels_video_ids=tuple(bg.pexels_video_id for bg in request.backgrounds),
        plans=tuple(segment_plans),
    )
