"""Progressive phrase segmentation and subtitle configuration for Quran Reels.

Splits long ayah text into short, readable phrase panels synchronized with
authentic recitation timing. Guarantees complete text reconstruction
(no missing, duplicated, or reordered words) for Arabic Quran and every
enabled verified translation.

Timing resolution: ayah-level only (MP3Quran). No word-level timestamps
are available, so phrases are proportionally timed within each ayah.
"""

from __future__ import annotations

from engine.layout_config import REEL_WIDTH, REEL_HEIGHT, TEXT_SAFE_ZONE, SURA_HEADER_OFFSET, clamp_header_offset, safe_rectangle, sura_header_top

import re
import unicodedata
from dataclasses import dataclass
from itertools import product
from typing import FrozenSet, Mapping, Optional, Sequence


# --- Subtitle Mode ---
MODE_PROGRESSIVE_PHRASE = "progressive_phrase_sync"
MODE_STATIC_FULL_AYAH = "static_full_ayah"
MODE_PROGRESSIVE_WORD = "progressive_word_highlight"

VALID_SUBTITLE_MODES = (MODE_PROGRESSIVE_PHRASE, MODE_STATIC_FULL_AYAH, MODE_PROGRESSIVE_WORD)

# --- Text Outline ---
OUTLINE_NONE = "none"
OUTLINE_MINIMAL = "minimal"
OUTLINE_THIN = "thin"
OUTLINE_MEDIUM = "medium"

VALID_TEXT_OUTLINES = (OUTLINE_NONE, OUTLINE_MINIMAL, OUTLINE_THIN, OUTLINE_MEDIUM)

# --- Text Shadow ---
SHADOW_OFF = "off"
SHADOW_VERY_SOFT = "very_soft"
SHADOW_SOFT = "soft"
SHADOW_MEDIUM = "medium"

VALID_TEXT_SHADOWS = (SHADOW_OFF, SHADOW_VERY_SOFT, SHADOW_SOFT, SHADOW_MEDIUM)

# --- Highlight Intensity ---
INTENSITY_SUBTLE = "subtle"
INTENSITY_NORMAL = "normal"
INTENSITY_STRONG = "strong"

VALID_HIGHLIGHT_INTENSITIES = (INTENSITY_SUBTLE, INTENSITY_NORMAL, INTENSITY_STRONG)

# --- Highlight Mode ---
MODE_HIGHLIGHT_NONE = "none"
MODE_HIGHLIGHT_PHRASE = "phrase_highlight"
MODE_HIGHLIGHT_WORD = "word_highlight"
MODE_HIGHLIGHT_PROGRESSIVE_REVEAL = "progressive_reveal_highlight"

VALID_HIGHLIGHT_MODES = (
    MODE_HIGHLIGHT_NONE,
    MODE_HIGHLIGHT_PHRASE,
    MODE_HIGHLIGHT_WORD,
    MODE_HIGHLIGHT_PROGRESSIVE_REVEAL,
)

# --- Highlight styles (legacy alias support) ---
HIGHLIGHT_NONE = "none"
HIGHLIGHT_PHRASE_FADE = "phrase_fade"
HIGHLIGHT_WORD = "word_highlight"
HIGHLIGHT_PHRASE_WORD = "phrase_word_highlight"

VALID_HIGHLIGHT_STYLES = (HIGHLIGHT_NONE, HIGHLIGHT_PHRASE_FADE, HIGHLIGHT_WORD, HIGHLIGHT_PHRASE_WORD)

# Word-level highlighting requires authentic word timestamps which do not exist in MP3Quran.
WORD_LEVEL_TIMING_AVAILABLE = False

# --- Highlight colours (ASS BGR hex without alpha) ---
HIGHLIGHT_COLORS = {
    "warm_white": "&H00F0F0FF",   # slightly warm
    "ivory":      "&H00E0EFFF",   # ivory tint
    "soft_gold":  "&H00C0DDFF",   # soft gold
    "neutral_white": "&H00FFFFFF",
}

VALID_HIGHLIGHT_COLORS = tuple(HIGHLIGHT_COLORS.keys())

# --- Header styles ---
HEADER_MINIMAL_CAPSULE = "minimal_capsule"
HEADER_SOFT_GLASS = "soft_glass"
HEADER_PLAIN_TEXT = "plain_text"
HEADER_HIDDEN = "hidden"

VALID_HEADER_STYLES = (HEADER_MINIMAL_CAPSULE, HEADER_SOFT_GLASS, HEADER_PLAIN_TEXT, HEADER_HIDDEN)

# --- Header content ---
HEADER_SURAH_ONLY = "surah_name_only"
HEADER_AR_TR_NAME = "arabic_translation_name"
HEADER_AR_TR_AYAH = "arabic_translation_ayah_range"

VALID_HEADER_CONTENTS = (HEADER_SURAH_ONLY, HEADER_AR_TR_NAME, HEADER_AR_TR_AYAH)

# --- Ayah reference position ---
REF_TOP_HEADER = "top_header"
REF_BOTTOM = "bottom"
REF_HIDDEN = "hidden"

VALID_REF_POSITIONS = (REF_TOP_HEADER, REF_BOTTOM, REF_HIDDEN)

# --- Subtitle backing ---
BACKING_NONE = "none"
BACKING_SHADOW = "shadow_only"
BACKING_GRADIENT = "soft_gradient"
BACKING_TRANSLUCENT = "soft_translucent"
BACKING_LOCALIZED = "localized_subtle"

VALID_BACKINGS = (
    BACKING_NONE,
    BACKING_SHADOW,
    BACKING_GRADIENT,
    BACKING_TRANSLUCENT,
    BACKING_LOCALIZED,
)

# --- Header backing intensity ---
HEADER_BACKING_NONE = "none"
HEADER_BACKING_LOW = "low"
HEADER_BACKING_MEDIUM = "medium"
HEADER_BACKING_HIGH = "high"

VALID_HEADER_BACKING_INTENSITIES = (
    HEADER_BACKING_NONE,
    HEADER_BACKING_LOW,
    HEADER_BACKING_MEDIUM,
    HEADER_BACKING_HIGH,
)

# --- Safe-zone constants for 1080×1920 ---
SAFE_MARGIN_TOP = TEXT_SAFE_ZONE["top"]
SAFE_MARGIN_BOTTOM = TEXT_SAFE_ZONE["bottom"]
SAFE_MARGIN_SIDES = TEXT_SAFE_ZONE["left"]
HEADER_Y_POSITION = 160
# Visible top of the complete Sura block, in final Reel pixels.
HEADER_TOP_OFFSET_PX = SURA_HEADER_OFFSET["default"]
HEADER_VISIBLE_TOP = sura_header_top()
SUBTITLE_CENTER_Y = 1150

# Arabic waqf / pause markers (Unicode) used as preferred split points
WAQF_MARKERS = set("ۖۗۘۙۚۛۜ")

# Arabic comma and semicolon equivalents
ARABIC_PUNCTUATION = {"،", "؛", "۞"}


class SubtitleError(ValueError):
    """Raised when subtitle segmentation or validation fails."""


@dataclass(frozen=True)
class SubtitleConfig:
    """Complete subtitle and visual style configuration with Premium defaults."""

    subtitle_mode: str = MODE_PROGRESSIVE_PHRASE
    arabic_font_size: int = 180
    translation_font_size: int = 50
    arabic_max_lines: int = 2
    translation_max_lines: int = 2
    arabic_translation_gap_px: int = 80
    translation_stack_gap_px: int = 80
    arabic_line_spacing: float = 1.15
    translation_line_spacing: float = 1.25
    text_outline: str = OUTLINE_MINIMAL
    text_shadow: str = SHADOW_VERY_SOFT
    highlight_mode: str = MODE_HIGHLIGHT_PHRASE
    highlight_intensity: str = INTENSITY_NORMAL
    highlight_style: str = HIGHLIGHT_PHRASE_FADE
    highlight_color: str = "warm_white"
    transition_duration_ms: int = 100
    subtitle_lead_ms: int = 0
    sura_header_top_offset: int = SURA_HEADER_OFFSET["default"]
    header_style: str = HEADER_MINIMAL_CAPSULE
    header_content: str = HEADER_AR_TR_AYAH
    header_backing_intensity: str = HEADER_BACKING_LOW
    ayah_reference_position: str = REF_TOP_HEADER
    subtitle_backing: str = BACKING_SHADOW

    def __post_init__(self) -> None:
        object.__setattr__(self, "sura_header_top_offset", clamp_header_offset(self.sura_header_top_offset))
        if self.subtitle_mode not in VALID_SUBTITLE_MODES:
            raise SubtitleError(f"Invalid subtitle_mode: {self.subtitle_mode}")
        if not (72 <= self.arabic_font_size <= 180):
            raise SubtitleError(f"arabic_font_size must be 72–180, got {self.arabic_font_size}")
        if not (30 <= self.translation_font_size <= 80):
            raise SubtitleError(f"translation_font_size must be 30–80, got {self.translation_font_size}")
        if not (1 <= self.arabic_max_lines <= 5):
            raise SubtitleError(f"arabic_max_lines must be 1–5, got {self.arabic_max_lines}")
        if not (1 <= self.translation_max_lines <= 5):
            raise SubtitleError(f"translation_max_lines must be 1–5, got {self.translation_max_lines}")
        if not (2 <= self.arabic_translation_gap_px <= 120):
            raise SubtitleError(f"arabic_translation_gap_px must be 2–120, got {self.arabic_translation_gap_px}")
        # Raw render configurations may replay a historical layout at its original gap.
        if self.translation_stack_gap_px not in (10, 20, 30, 80, 100, 120):
            raise SubtitleError(
                f"Unsupported translation_stack_gap_px: {self.translation_stack_gap_px}"
            )
        if not (0.95 <= self.arabic_line_spacing <= 1.60):
            raise SubtitleError(f"arabic_line_spacing must be 0.95–1.60, got {self.arabic_line_spacing}")
        if not (1.0 <= self.translation_line_spacing <= 1.6):
            raise SubtitleError(f"translation_line_spacing must be 1.0–1.6, got {self.translation_line_spacing}")
        if self.text_outline not in VALID_TEXT_OUTLINES:
            raise SubtitleError(f"Invalid text_outline: {self.text_outline}")
        if self.text_shadow not in VALID_TEXT_SHADOWS:
            raise SubtitleError(f"Invalid text_shadow: {self.text_shadow}")
        if self.highlight_mode not in VALID_HIGHLIGHT_MODES:
            raise SubtitleError(f"Invalid highlight_mode: {self.highlight_mode}")
        if self.highlight_intensity not in VALID_HIGHLIGHT_INTENSITIES:
            raise SubtitleError(f"Invalid highlight_intensity: {self.highlight_intensity}")
        if self.highlight_style not in VALID_HIGHLIGHT_STYLES:
            raise SubtitleError(f"Invalid highlight_style: {self.highlight_style}")
        if self.highlight_color not in VALID_HIGHLIGHT_COLORS:
            raise SubtitleError(f"Invalid highlight_color: {self.highlight_color}")
        if not (100 <= self.transition_duration_ms <= 400):
            raise SubtitleError(f"transition_duration_ms must be 100–400, got {self.transition_duration_ms}")
        if not (0 <= self.subtitle_lead_ms <= 1000):
            raise SubtitleError(f"subtitle_lead_ms must be 0–1000, got {self.subtitle_lead_ms}")
        if self.header_style not in VALID_HEADER_STYLES:
            raise SubtitleError(f"Invalid header_style: {self.header_style}")
        if self.header_content not in VALID_HEADER_CONTENTS:
            raise SubtitleError(f"Invalid header_content: {self.header_content}")
        if self.header_backing_intensity not in VALID_HEADER_BACKING_INTENSITIES:
            raise SubtitleError(f"Invalid header_backing_intensity: {self.header_backing_intensity}")
        if self.ayah_reference_position not in VALID_REF_POSITIONS:
            raise SubtitleError(f"Invalid ayah_reference_position: {self.ayah_reference_position}")
        if self.subtitle_backing not in VALID_BACKINGS:
            raise SubtitleError(f"Invalid subtitle_backing: {self.subtitle_backing}")

    @property
    def effective_highlight_style(self) -> str:
        """Return the highlight style, falling back to phrase_fade if word timing unavailable."""
        if self.highlight_mode == MODE_HIGHLIGHT_NONE:
            return HIGHLIGHT_NONE
        if not WORD_LEVEL_TIMING_AVAILABLE and self.highlight_style in (HIGHLIGHT_WORD, HIGHLIGHT_PHRASE_WORD):
            return HIGHLIGHT_PHRASE_FADE
        return self.highlight_style


@dataclass(frozen=True)
class BilingualPhrasePanel:
    """A single unified progressive bilingual subtitle panel (Arabic phrase + matching Translation)."""

    arabic_text: str
    translation_text: str
    start_ms: int
    end_ms: int
    surah_number: int
    ayah_number: int
    panel_index: int
    total_panels: int
    visual_start_ms: int = 0
    visual_end_ms: int = 0
    verse_key: str = ""

    def __post_init__(self) -> None:
        if not self.verse_key and self.surah_number > 0 and self.ayah_number > 0:
            object.__setattr__(self, "verse_key", f"{self.surah_number}:{self.ayah_number}")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def visual_duration_ms(self) -> int:
        return self.visual_end_ms - self.visual_start_ms


# Alias for backward compatibility
PhrasePanel = BilingualPhrasePanel


@dataclass(frozen=True)
class TranslationPhrase:
    """One exact source-language segment within a multilingual panel."""

    language: str
    text: str

    def __post_init__(self) -> None:
        if self.language not in {"en", "de"}:
            raise SubtitleError(f"Unsupported translation language: {self.language}")
        if not self.text or not self.text.strip():
            raise SubtitleError("Translation phrase text must be non-empty.")


@dataclass(frozen=True)
class MultilingualPhrasePanel:
    """Arabic plus ordered exact translation segments sharing one content window."""

    arabic_text: str
    translations: tuple[TranslationPhrase, ...]
    start_ms: int
    end_ms: int
    surah_number: int
    ayah_number: int
    panel_index: int
    total_panels: int
    visual_start_ms: int = 0
    visual_end_ms: int = 0
    verse_key: str = ""

    def __post_init__(self) -> None:
        if not self.arabic_text or not self.arabic_text.strip():
            raise SubtitleError("Arabic phrase text must be non-empty.")
        if not self.translations:
            raise SubtitleError("A multilingual panel requires at least one translation.")
        languages = tuple(item.language for item in self.translations)
        if len(set(languages)) != len(languages):
            raise SubtitleError("Translation languages must be unique within a panel.")
        if not self.verse_key and self.surah_number > 0 and self.ayah_number > 0:
            object.__setattr__(self, "verse_key", f"{self.surah_number}:{self.ayah_number}")

    @property
    def translation_text(self) -> str:
        """Backward-compatible first-translation access."""
        return self.translations[0].text

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def visual_duration_ms(self) -> int:
        return self.visual_end_ms - self.visual_start_ms


@dataclass(frozen=True)
class SubtitleGroupLayout:
    """Painted-pixel geometry for one centered bilingual subtitle group."""

    arabic_lines_count: int
    translation_lines_count: int
    arabic_block_height: int
    translation_block_height: int
    total_group_height: int
    group_top_y: int
    group_bottom_y: int
    arabic_anchor_y: int
    translation_anchor_y: int
    arabic_visible_top_y: int
    arabic_visible_bottom_y: int
    translation_visible_top_y: int
    translation_visible_bottom_y: int
    effective_gap_px: int
    backing_rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class TextBlockBounds:
    """Actual libass-painted bounds relative to an ASS ``\\pos`` anchor."""

    left_offset: int
    top_offset: int
    right_offset: int
    bottom_offset: int
    line_count: int

    def __post_init__(self) -> None:
        if self.right_offset <= self.left_offset or self.bottom_offset <= self.top_offset:
            raise SubtitleError("TextBlockBounds must have positive width and height.")
        if self.line_count < 1:
            raise SubtitleError("TextBlockBounds.line_count must be positive.")

    @property
    def width(self) -> int:
        return self.right_offset - self.left_offset

    @property
    def height(self) -> int:
        return self.bottom_offset - self.top_offset


@dataclass(frozen=True)
class MultilingualSubtitleGroupLayout:
    """Painted-pixel geometry for one centered multilingual subtitle stack."""

    anchor_x: int
    arabic_anchor_y: int
    translation_anchor_ys: tuple[int, ...]
    arabic_visible_top_y: int
    arabic_visible_bottom_y: int
    translation_visible_ranges: tuple[tuple[int, int], ...]
    total_group_height: int
    group_top_y: int
    group_bottom_y: int
    effective_gap_px: int
    translation_stack_gap_px: int
    backing_rect: tuple[int, int, int, int]


def calculate_multilingual_subtitle_group_layout(
    arabic_bounds: TextBlockBounds,
    translation_bounds: Sequence[TextBlockBounds],
    config: SubtitleConfig,
    center_y: int = SUBTITLE_CENTER_Y,
    video_width: int = 1080,
    video_height: int = 1920,
    safe_margin_sides: int = SAFE_MARGIN_SIDES,
) -> MultilingualSubtitleGroupLayout:
    """Measure the complete Arabic/translation stack and center it exactly once."""
    if not translation_bounds:
        raise SubtitleError("At least one measured translation block is required.")

    gap_px = config.arabic_translation_gap_px
    stack_gap_px = config.translation_stack_gap_px
    total_vis_h = (
        arabic_bounds.height
        + gap_px
        + sum(bound.height for bound in translation_bounds)
        + stack_gap_px * (len(translation_bounds) - 1)
    )
    safe = safe_rectangle(video_width, video_height)
    if total_vis_h > safe["height"]:
        raise SubtitleError("Arabic and translation blocks exceed the fixed text Safe Zone.")
    all_bounds = (arabic_bounds, *translation_bounds)
    left = min(b.left_offset for b in all_bounds)
    right = max(b.right_offset for b in all_bounds)
    if right - left > safe["width"]:
        raise SubtitleError("Text exceeds the fixed left/right Safe Zone.")
    center_x = min(max(video_width // 2, safe["x"] - left), safe["right"] - right)
    group_top = min(max(center_y - (total_vis_h // 2), safe["y"]), safe["bottom"] - total_vis_h)
    group_bottom = group_top + total_vis_h
    arabic_anchor_y = group_top - arabic_bounds.top_offset
    arabic_bottom = group_top + arabic_bounds.height

    anchors: list[int] = []
    visible_ranges: list[tuple[int, int]] = []
    next_top = arabic_bottom + gap_px
    for index, bound in enumerate(translation_bounds):
        visible_top = next_top
        visible_bottom = visible_top + bound.height
        anchors.append(visible_top - bound.top_offset)
        visible_ranges.append((visible_top, visible_bottom))
        next_top = visible_bottom + (stack_gap_px if index < len(translation_bounds) - 1 else 0)

    all_bounds = (arabic_bounds, *translation_bounds)
    content_left = center_x + min(bound.left_offset for bound in all_bounds)
    content_right = center_x + max(bound.right_offset for bound in all_bounds)
    pad_h = 36
    pad_v = 22
    backing_left = max(safe_margin_sides, content_left - pad_h)
    backing_right = min(video_width - safe_margin_sides, content_right + pad_h)
    backing_top = max(0, group_top - pad_v)
    backing_bottom = min(video_height, group_bottom + pad_v)

    return MultilingualSubtitleGroupLayout(
        anchor_x=center_x,
        arabic_anchor_y=arabic_anchor_y,
        translation_anchor_ys=tuple(anchors),
        arabic_visible_top_y=group_top,
        arabic_visible_bottom_y=arabic_bottom,
        translation_visible_ranges=tuple(visible_ranges),
        total_group_height=total_vis_h,
        group_top_y=group_top,
        group_bottom_y=group_bottom,
        effective_gap_px=gap_px,
        translation_stack_gap_px=stack_gap_px,
        backing_rect=(backing_left, backing_top, backing_right, backing_bottom),
    )


def calculate_subtitle_group_layout(
    arabic_bounds: TextBlockBounds,
    translation_bounds: TextBlockBounds,
    config: SubtitleConfig,
    center_y: int = SUBTITLE_CENTER_Y,
    video_width: int = 1080,
    video_height: int = 1920,
    safe_margin_sides: int = SAFE_MARGIN_SIDES,
) -> SubtitleGroupLayout:
    """Place Arabic and translation from their real painted bounds, then center once.

    The configured gap is the empty pixel distance between the two painted
    blocks.  No font-specific ascent guesses, hidden collision buffers, or
    second translation recentering are applied.
    """
    multilingual = calculate_multilingual_subtitle_group_layout(
        arabic_bounds,
        (translation_bounds,),
        config,
        center_y=center_y,
        video_width=video_width,
        video_height=video_height,
        safe_margin_sides=safe_margin_sides,
    )
    translation_vis_top, translation_vis_bottom = multilingual.translation_visible_ranges[0]

    return SubtitleGroupLayout(
        arabic_lines_count=arabic_bounds.line_count,
        translation_lines_count=translation_bounds.line_count,
        arabic_block_height=arabic_bounds.height,
        translation_block_height=translation_bounds.height,
        total_group_height=multilingual.total_group_height,
        group_top_y=multilingual.group_top_y,
        group_bottom_y=multilingual.group_bottom_y,
        arabic_anchor_y=multilingual.arabic_anchor_y,
        translation_anchor_y=multilingual.translation_anchor_ys[0],
        arabic_visible_top_y=multilingual.arabic_visible_top_y,
        arabic_visible_bottom_y=multilingual.arabic_visible_bottom_y,
        translation_visible_top_y=translation_vis_top,
        translation_visible_bottom_y=translation_vis_bottom,
        effective_gap_px=multilingual.effective_gap_px,
        backing_rect=multilingual.backing_rect,
    )


@dataclass(frozen=True)
class _BoundaryCandidate:
    """Potential cumulative bilingual clause boundary."""

    index: int
    kinds: FrozenSet[str]


def _strip_arabic_marks(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(char) and char not in WAQF_MARKERS
    )


def _arabic_boundary_candidates(words: Sequence[str]) -> list[_BoundaryCandidate]:
    candidates: dict[int, set[str]] = {}
    total = len(words)

    def add(index: int, kind: str) -> None:
        if 2 <= index <= total - 2:
            candidates.setdefault(index, set()).add(kind)

    for index, word in enumerate(words, start=1):
        normalized = _strip_arabic_marks(word).strip("،؛۞")
        if any(marker in word for marker in WAQF_MARKERS):
            add(index, "pause")
        if normalized.startswith("قال") or normalized in {"قل", "قيل"}:
            add(index, "speech")

        if index < total:
            next_word = _strip_arabic_marks(words[index])
            if len(next_word) > 1 and next_word[0] in {"و", "ف"}:
                add(index, "connector")

    return [
        _BoundaryCandidate(index=index, kinds=frozenset(kinds))
        for index, kinds in sorted(candidates.items())
    ]


def _translation_boundary_candidates(
    words: Sequence[str],
    language: str,
) -> list[_BoundaryCandidate]:
    candidates: dict[int, set[str]] = {}
    total = len(words)

    def add(index: int, kind: str) -> None:
        if 2 <= index <= total - 2:
            candidates.setdefault(index, set()).add(kind)

    for index, word in enumerate(words, start=1):
        plain = word.rstrip("'\"”’)]}")
        lower = plain.lower()
        if re.search(r"[.!?;:]$", plain):
            add(index, "pause")
        elif plain.endswith(","):
            add(index, "comma")
        speech_words = (
            {"said", "say", "says", "asked", "replied"}
            if language == "en"
            else {"sagte", "sagen", "sagt", "sprach", "sprich", "fragte", "antwortete"}
        )
        if re.sub(r"[^a-zäöüß]", "", lower) in speech_words:
            add(index, "speech")

        if index < total:
            next_word = words[index].lstrip("'\"“‘([{ ").lower().rstrip(",;:")
            connectors = (
                {"and", "but", "so", "then", "yet", "nor", "indeed"}
                if language == "en"
                else {"und", "aber", "doch", "so", "dann", "denn", "wahrlich"}
            )
            if next_word in connectors:
                add(index, "connector")

    return [
        _BoundaryCandidate(index=index, kinds=frozenset(kinds))
        for index, kinds in sorted(candidates.items())
    ]


def _english_boundary_candidates(words: Sequence[str]) -> list[_BoundaryCandidate]:
    """Backward-compatible English candidate wrapper."""
    return _translation_boundary_candidates(words, "en")


def _boundary_pair_score(
    arabic: _BoundaryCandidate,
    translation: _BoundaryCandidate,
    arabic_total: int,
    translation_total: int,
    *,
    conservative: bool = False,
    previous_arabic: int = 0,
    previous_translation: int = 0,
) -> Optional[float]:
    compatible = False
    bonus = 0.0
    conservative_connector_match = False

    if "speech" in arabic.kinds and "speech" in translation.kinds:
        compatible = True
        bonus = 0.12
    elif "pause" in arabic.kinds and "pause" in translation.kinds:
        compatible = True
        bonus = 0.15
    elif "pause" in arabic.kinds and "comma" in translation.kinds:
        # A canonical Quran pause marker and a punctuation boundary in every
        # enabled source translation are shared clause evidence.  The
        # multilingual caller still requires a close proportional match in
        # every language before accepting the Arabic boundary.
        compatible = True
        bonus = 0.08
    elif (
        conservative
        and "connector" in arabic.kinds
        and {"comma", "connector"}.issubset(translation.kinds)
    ):
        # In a multilingual plan a conjunction alone is not enough evidence.
        # Require punctuation plus a connector independently in every enabled
        # translation before accepting the shared Arabic clause boundary.
        compatible = True
        conservative_connector_match = True
        bonus = 0.10
    elif (
        not conservative
        and "connector" in arabic.kinds
        and translation.kinds.intersection({"connector", "comma", "pause"})
    ):
        compatible = True
        bonus = 0.05

    if not compatible:
        return None

    ratio_distance = abs(
        (arabic.index / arabic_total) - (translation.index / translation_total)
    )
    # With two translations there is no authoritative word-level alignment in
    # QuranEnc's ayah payload.  Require much closer, punctuation-backed clause
    # boundaries and never infer a shared boundary from connectors alone. If
    # this cannot be proven, the caller deliberately keeps the complete ayah so
    # the renderer can fail closed on fit instead of displaying future words.
    maximum_ratio_distance = (
        0.08
        if conservative_connector_match
        else (0.12 if conservative else 0.20)
    )
    if ratio_distance > maximum_ratio_distance:
        return None
    if conservative:
        segment_ratio_distance = abs(
            ((arabic.index - previous_arabic) / arabic_total)
            - ((translation.index - previous_translation) / translation_total)
        )
        if segment_ratio_distance > maximum_ratio_distance:
            return None
    return ratio_distance - bonus


def segment_bilingual_phrases(
    arabic_text: str,
    translation_text: str,
    *,
    target_arabic_words: int = 6,
    maximum_arabic_words: int = 10,
) -> list[tuple[str, str]]:
    """Jointly select conservative Arabic/Rowwad clause boundaries.

    Only compatible pause, speech, or conjunction boundaries in both languages
    are accepted.  If no safe joint boundary is available, the complete ayah is
    returned as one bilingual unit instead of claiming a semantic alignment that
    the source data cannot prove.
    """
    multilingual = segment_multilingual_phrases(
        arabic_text,
        {"en": translation_text},
        target_arabic_words=target_arabic_words,
        maximum_arabic_words=maximum_arabic_words,
    )
    return [(arabic, translations[0].text) for arabic, translations in multilingual]


def segment_multilingual_phrases(
    arabic_text: str,
    translations: Mapping[str, str],
    *,
    target_arabic_words: int = 6,
    maximum_arabic_words: int = 10,
) -> list[tuple[str, tuple[TranslationPhrase, ...]]]:
    """Choose one conservative Arabic boundary plan shared by all languages."""
    if not arabic_text or not arabic_text.strip():
        raise SubtitleError("arabic_text must be non-empty.")
    if not translations:
        raise SubtitleError("At least one translation is required.")
    ordered = tuple(translations.items())
    if len({language for language, _ in ordered}) != len(ordered):
        raise SubtitleError("Translation languages must be unique.")
    for language, text in ordered:
        if language not in {"en", "de"}:
            raise SubtitleError(f"Unsupported translation language: {language}")
        if not text or not text.strip():
            raise SubtitleError(f"Translation text for '{language}' must be non-empty.")

    arabic_words = arabic_text.split()
    translation_words = {language: text.split() for language, text in ordered}
    if len(arabic_words) <= maximum_arabic_words:
        return [(
            arabic_text.strip(),
            tuple(TranslationPhrase(language, text.strip()) for language, text in ordered),
        )]

    arabic_candidates = _arabic_boundary_candidates(arabic_words)
    language_candidates = {
        language: _translation_boundary_candidates(words, language)
        for language, words in translation_words.items()
    }
    if not arabic_candidates or any(not values for values in language_candidates.values()):
        return [(
            arabic_text.strip(),
            tuple(TranslationPhrase(language, text.strip()) for language, text in ordered),
        )]

    language_order = tuple(language for language, _ in ordered)
    failed_states: set[tuple[int, tuple[int, ...]]] = set()

    def find_complete_boundaries(
        previous_arabic: int,
        previous_by_language: dict[str, int],
    ) -> Optional[list[tuple[int, dict[str, int]]]]:
        """Backtrack over safe candidates so a greedy dead end cannot hide a valid plan."""
        if len(arabic_words) - previous_arabic <= maximum_arabic_words:
            return []
        state = (
            previous_arabic,
            tuple(previous_by_language[language] for language in language_order),
        )
        if state in failed_states:
            return None

        options: list[tuple[float, int, dict[str, int]]] = []
        for arabic_candidate in arabic_candidates:
            if arabic_candidate.index <= previous_arabic:
                continue
            segment_words = arabic_candidate.index - previous_arabic
            if segment_words < 2 or segment_words > maximum_arabic_words:
                continue
            choices_by_language: list[tuple[str, list[tuple[float, int]]]] = []
            valid = True
            for language, words in translation_words.items():
                candidates: list[tuple[float, int]] = []
                for candidate in language_candidates[language]:
                    if candidate.index <= previous_by_language[language]:
                        continue
                    if len(words) - candidate.index < 2:
                        continue
                    score = _boundary_pair_score(
                        arabic_candidate,
                        candidate,
                        len(arabic_words),
                        len(words),
                        conservative=len(ordered) > 1,
                        previous_arabic=previous_arabic,
                        previous_translation=previous_by_language[language],
                    )
                    if score is not None:
                        candidates.append((score, candidate.index))
                if not candidates:
                    valid = False
                    break
                choices_by_language.append((language, sorted(candidates)))
            if not valid:
                continue

            # Explore the full shared state. Choosing only the locally best
            # translation index can dead-end later even when another safe
            # English/German boundary combination completes the Arabic plan.
            for combination in product(
                *(candidates for _, candidates in choices_by_language)
            ):
                chosen = {
                    language: candidate[1]
                    for (language, _), candidate in zip(
                        choices_by_language,
                        combination,
                    )
                }
                total_score = (
                    abs(segment_words - target_arabic_words) * 0.01
                    + sum(candidate[0] for candidate in combination)
                )
                options.append((total_score, arabic_candidate.index, chosen))

        for _, arabic_index, chosen in sorted(
            options,
            key=lambda option: (option[0], option[1]),
        ):
            next_positions = dict(previous_by_language)
            next_positions.update(chosen)
            remainder = find_complete_boundaries(arabic_index, next_positions)
            if remainder is not None:
                return [(arabic_index, chosen), *remainder]

        failed_states.add(state)
        return None

    boundaries = find_complete_boundaries(
        0,
        {language: 0 for language, _ in ordered},
    )
    if not boundaries:
        return [(
            arabic_text.strip(),
            tuple(TranslationPhrase(language, text.strip()) for language, text in ordered),
        )]

    results: list[tuple[str, tuple[TranslationPhrase, ...]]] = []
    arabic_start = 0
    starts = {language: 0 for language, _ in ordered}
    for arabic_end, ends in boundaries:
        segments = tuple(
            TranslationPhrase(language, " ".join(translation_words[language][starts[language]:ends[language]]))
            for language, _ in ordered
        )
        results.append((" ".join(arabic_words[arabic_start:arabic_end]), segments))
        arabic_start = arabic_end
        starts.update(ends)
    results.append((
        " ".join(arabic_words[arabic_start:]),
        tuple(
            TranslationPhrase(language, " ".join(translation_words[language][starts[language]:]))
            for language, _ in ordered
        ),
    ))

    if " ".join(arabic for arabic, _ in results).split() != arabic_words:
        raise SubtitleError("Joint Arabic segmentation failed exact reconstruction.")
    for language, words in translation_words.items():
        reconstructed = [
            word
            for _, segments in results
            for segment in segments
            if segment.language == language
            for word in segment.text.split()
        ]
        if reconstructed != words:
            raise SubtitleError(f"Joint {language} segmentation failed exact reconstruction.")
    return results


def _find_best_split(words: list[str], target_idx: int, pos: int = 0) -> int:
    """Find the best split point near target_idx, preferring waqf/punctuation/clause boundaries."""
    if target_idx <= 0:
        return 1
    if target_idx >= len(words):
        return len(words)

    search_start = max(pos + 2, target_idx - 2)
    search_end = min(len(words), target_idx + 3)

    best_idx = target_idx
    best_priority = 999

    for idx in range(search_start, search_end):
        word_before = words[idx - 1]
        word_curr = words[idx] if idx < len(words) else ""
        priority = 999

        if any(c in WAQF_MARKERS for c in word_before):
            priority = 1
        elif word_before and word_before[-1] in ARABIC_PUNCTUATION:
            priority = 2
        elif word_curr.startswith("وَ") or word_curr.startswith("فَ"):
            priority = 3
        elif idx == target_idx:
            priority = 4
        else:
            priority = 5 + abs(idx - target_idx)

        if priority < best_priority:
            best_priority = priority
            best_idx = idx

    return best_idx


def segment_arabic_into_phrases(
    arabic_text: str,
    target_words_per_phrase: int = 6,
    max_words_per_phrase: int = 8,
) -> list[str]:
    """Split Arabic Quran text into readable semantic phrase segments.

    Preserves exact Uthmani text. Uses available 2-line screen space to keep
    complete semantic units together.
    Guarantees: ' '.join(result).split() == arabic_text.split()
    """
    if not arabic_text or not arabic_text.strip():
        raise SubtitleError("arabic_text must be non-empty.")

    words = arabic_text.split()
    if not words:
        raise SubtitleError("arabic_text contains no words after splitting.")

    if len(words) <= max_words_per_phrase:
        return [arabic_text.strip()]

    phrases: list[str] = []
    pos = 0

    while pos < len(words):
        remaining = len(words) - pos

        if remaining <= max_words_per_phrase:
            phrases.append(" ".join(words[pos:]))
            break

        target = pos + target_words_per_phrase
        split_at = _find_best_split(words, min(target, len(words)), pos=pos)

        if split_at - pos > max_words_per_phrase:
            split_at = pos + max_words_per_phrase
        if split_at <= pos:
            split_at = pos + 1

        if len(words) - split_at <= 2 and split_at - pos <= max_words_per_phrase - 2:
            split_at = len(words)

        phrases.append(" ".join(words[pos:split_at]))
        pos = split_at

    return phrases


def segment_translation_to_match(
    translation_text: str,
    num_arabic_phrases: int,
    arabic_phrases: Optional[list[str]] = None,
) -> list[str]:
    """Split translation text into quote-aware segments corresponding to Arabic phrases.

    Preserves 100% exact words and punctuation without rewrite.
    Guarantees: ' '.join(result).split() == translation_text.split()
    """
    if not translation_text or not translation_text.strip():
        raise SubtitleError("translation_text must be non-empty.")

    if num_arabic_phrases < 1:
        raise SubtitleError("num_arabic_phrases must be >= 1.")

    words = translation_text.split()
    if not words:
        raise SubtitleError("translation_text contains no words after splitting.")

    if num_arabic_phrases == 1 or len(words) <= 1:
        return [" ".join(words)]

    total_words = len(words)
    splits: list[int] = []
    pos = 0

    for seg_idx in range(num_arabic_phrases - 1):
        if arabic_phrases and len(arabic_phrases) == num_arabic_phrases:
            ar_words_so_far = sum(len(p.split()) for p in arabic_phrases[:seg_idx + 1])
            ar_total_words = sum(len(p.split()) for p in arabic_phrases)
            target_pos = int(round((ar_words_so_far / ar_total_words) * total_words))
        else:
            target_pos = int(round((seg_idx + 1) * (total_words / num_arabic_phrases)))

        w_start = max(pos + 2, target_pos - 4)
        w_end = min(total_words - (num_arabic_phrases - 1 - seg_idx), target_pos + 5)

        best_split = target_pos
        best_score = 999

        for idx in range(w_start, w_end):
            prev_w = words[idx - 1]
            curr_w = words[idx] if idx < total_words else ""

            score = abs(idx - target_pos) * 2

            if prev_w.endswith(",") or prev_w.endswith(";") or prev_w.endswith(":") or prev_w.endswith(".") or prev_w.endswith("!”") or prev_w.endswith("!\"") or prev_w.endswith("us."):
                score -= 14
            if curr_w.lower() in ("and", "indeed,", "indeed", "so", "they", "we", "he", "when", "that"):
                score -= 6
            if prev_w.lower() in ("said,", "saying,") and (curr_w.startswith('"') or curr_w.startswith("“") or curr_w.startswith("'")):
                score += 8
            if prev_w.lower().endswith("patience"):
                score -= 12

            if score < best_score:
                best_score = score
                best_split = idx

        splits.append(best_split)
        pos = best_split

    segments: list[str] = []
    prev = 0
    for s in splits:
        segments.append(" ".join(words[prev:s]))
        prev = s
    segments.append(" ".join(words[prev:]))

    return segments


def build_phrase_panels(
    surah_number: int,
    ayah_number: int,
    arabic_text: str,
    translation_text: str,
    start_ms: int,
    end_ms: int,
    config: Optional[SubtitleConfig] = None,
) -> list[BilingualPhrasePanel]:
    """Build unified progressive bilingual phrase panels for a single ayah.

    Ensures Arabic and Translation boundaries are selected together as single
    cohesive bilingual units with translation never running ahead of Arabic.
    """
    cfg = config or SubtitleConfig()

    if cfg.subtitle_mode == MODE_STATIC_FULL_AYAH:
        return [BilingualPhrasePanel(
            arabic_text=arabic_text.strip(),
            translation_text=translation_text.strip(),
            start_ms=start_ms,
            end_ms=end_ms,
            surah_number=surah_number,
            ayah_number=ayah_number,
            panel_index=0,
            total_panels=1,
            visual_start_ms=max(0, start_ms - cfg.subtitle_lead_ms),
            visual_end_ms=end_ms,
            verse_key=f"{surah_number}:{ayah_number}",
        )]

    bilingual_segments = segment_bilingual_phrases(arabic_text, translation_text)
    arabic_phrases = [arabic for arabic, _ in bilingual_segments]
    translation_segments = [translation for _, translation in bilingual_segments]

    num_panels = len(arabic_phrases)
    ayah_duration = end_ms - start_ms

    if ayah_duration <= 0:
        raise SubtitleError(f"Invalid ayah timing: {start_ms}..{end_ms}")

    # Phrase-level timing remains an explicitly approximate distribution within
    # the provider-authentic ayah boundary.  Weight by recited Arabic base
    # letters instead of assigning visibly different phrases equal durations.
    phrase_weights = [
        max(
            1,
            sum(
                1
                for char in phrase
                if not unicodedata.combining(char)
                and not char.isspace()
                and char not in WAQF_MARKERS
            ),
        )
        for phrase in arabic_phrases
    ]
    total_weight = sum(phrase_weights)

    raw_panels: list[dict] = []
    cumulative_weight = 0
    for i in range(num_panels):
        panel_start = start_ms + int(round(cumulative_weight * ayah_duration / total_weight))
        cumulative_weight += phrase_weights[i]
        panel_end = start_ms + int(round(cumulative_weight * ayah_duration / total_weight))

        if i == num_panels - 1:
            panel_end = end_ms

        raw_panels.append({
            "arabic_text": arabic_phrases[i],
            "translation_text": translation_segments[i],
            "start_ms": panel_start,
            "end_ms": panel_end,
            "panel_index": i,
        })

    panels: list[BilingualPhrasePanel] = []
    lead_ms = cfg.subtitle_lead_ms

    for i, p in enumerate(raw_panels):
        v_start = max(0, p["start_ms"] - lead_ms)
        if i + 1 < len(raw_panels):
            next_start = raw_panels[i + 1]["start_ms"]
            v_end = max(v_start + 100, next_start - lead_ms)
        else:
            v_end = p["end_ms"]

        panels.append(BilingualPhrasePanel(
            arabic_text=p["arabic_text"],
            translation_text=p["translation_text"],
            start_ms=p["start_ms"],
            end_ms=p["end_ms"],
            surah_number=surah_number,
            ayah_number=ayah_number,
            panel_index=i,
            total_panels=num_panels,
            visual_start_ms=v_start,
            visual_end_ms=v_end,
            verse_key=f"{surah_number}:{ayah_number}",
        ))

    return panels


def build_multilingual_phrase_panels(
    surah_number: int,
    ayah_number: int,
    arabic_text: str,
    translations: Mapping[str, str],
    start_ms: int,
    end_ms: int,
    config: Optional[SubtitleConfig] = None,
    target_arabic_words: Optional[int] = None,
    maximum_arabic_words: Optional[int] = None,
) -> list[MultilingualPhrasePanel]:
    """Build immutable panels from one boundary plan shared by all translations."""
    cfg = config or SubtitleConfig()
    if end_ms <= start_ms:
        raise SubtitleError(f"Invalid ayah timing: {start_ms}..{end_ms}")

    if cfg.subtitle_mode == MODE_STATIC_FULL_AYAH:
        segments = [(
            arabic_text.strip(),
            tuple(TranslationPhrase(language, text.strip()) for language, text in translations.items()),
        )]
    else:
        translation_count = len(translations)
        target_words = target_arabic_words or (4 if translation_count > 1 else 6)
        maximum_words = maximum_arabic_words or (7 if translation_count > 1 else 10)
        segments = segment_multilingual_phrases(
            arabic_text,
            translations,
            target_arabic_words=target_words,
            maximum_arabic_words=maximum_words,
        )

    return build_multilingual_panels_from_segments(
        surah_number=surah_number,
        ayah_number=ayah_number,
        segments=segments,
        start_ms=start_ms,
        end_ms=end_ms,
        config=cfg,
    )


def build_multilingual_panels_from_segments(
    *,
    surah_number: int,
    ayah_number: int,
    segments: Sequence[tuple[str, Sequence[TranslationPhrase]]],
    start_ms: int,
    end_ms: int,
    config: Optional[SubtitleConfig] = None,
) -> list[MultilingualPhrasePanel]:
    """Time exact, already-authorized source segments without realigning them.

    This is the production entry point for reviewed alignment data.  It only
    applies the approved Arabic-base-letter timing subdivision and never
    invents Arabic/translation boundaries.
    """
    cfg = config or SubtitleConfig()
    if end_ms <= start_ms:
        raise SubtitleError(f"Invalid ayah timing: {start_ms}..{end_ms}")
    if not segments:
        raise SubtitleError("At least one subtitle segment is required.")

    normalized_segments: list[tuple[str, tuple[TranslationPhrase, ...]]] = []
    for arabic, translated in segments:
        arabic_text = arabic.strip()
        translations = tuple(translated)
        if not arabic_text or not translations:
            raise SubtitleError("Every subtitle segment needs Arabic and translation text.")
        normalized_segments.append((arabic_text, translations))

    phrase_weights = [
        max(
            1,
            sum(
                1
                for char in arabic
                if not unicodedata.combining(char)
                and not char.isspace()
                and char not in WAQF_MARKERS
            ),
        )
        for arabic, _ in normalized_segments
    ]
    total_weight = sum(phrase_weights)
    duration = end_ms - start_ms
    panels: list[MultilingualPhrasePanel] = []
    cumulative = 0
    panel_times: list[tuple[int, int]] = []
    for index, weight in enumerate(phrase_weights):
        panel_start = start_ms + int(round(cumulative * duration / total_weight))
        cumulative += weight
        panel_end = start_ms + int(round(cumulative * duration / total_weight))
        if index == len(segments) - 1:
            panel_end = end_ms
        panel_times.append((panel_start, panel_end))

    for index, ((arabic, translated), (panel_start, panel_end)) in enumerate(
        zip(normalized_segments, panel_times)
    ):
        visual_start = max(0, panel_start - cfg.subtitle_lead_ms)
        visual_end = (
            max(visual_start + 100, panel_times[index + 1][0] - cfg.subtitle_lead_ms)
            if index + 1 < len(panel_times)
            else panel_end
        )
        panels.append(MultilingualPhrasePanel(
            arabic_text=arabic,
            translations=translated,
            start_ms=panel_start,
            end_ms=panel_end,
            surah_number=surah_number,
            ayah_number=ayah_number,
            panel_index=index,
            total_panels=len(normalized_segments),
            visual_start_ms=visual_start,
            visual_end_ms=visual_end,
            verse_key=f"{surah_number}:{ayah_number}",
        ))
    return panels


def verify_arabic_reconstruction(panels: Sequence[PhrasePanel], original_text: str) -> bool:
    """Verify that concatenating panel Arabic texts reproduces the original.

    Comparison is whitespace-normalized.
    """
    reconstructed_words = []
    for panel in panels:
        reconstructed_words.extend(panel.arabic_text.split())

    original_words = original_text.split()
    return reconstructed_words == original_words


def verify_translation_reconstruction(panels: Sequence[PhrasePanel], original_text: str) -> bool:
    """Verify that concatenating panel translation texts reproduces the original.

    Comparison is whitespace-normalized. Empty panels are skipped.
    """
    reconstructed_words = []
    for panel in panels:
        if panel.translation_text.strip():
            reconstructed_words.extend(panel.translation_text.split())

    original_words = original_text.split()
    return reconstructed_words == original_words


def verify_multilingual_reconstruction(
    panels: Sequence[MultilingualPhrasePanel],
    originals: Mapping[str, str],
) -> bool:
    """Verify every enabled translation independently after whitespace normalization."""
    for language, original in originals.items():
        reconstructed = [
            word
            for panel in panels
            for segment in panel.translations
            if segment.language == language
            for word in segment.text.split()
        ]
        if reconstructed != original.split():
            return False
    return True


def format_header_text(
    arabic_surah_name: str,
    english_surah_name: str,
    first_ayah: int,
    last_ayah: int,
    surah_number: int,
    config: Optional[SubtitleConfig] = None,
) -> tuple[str, str]:
    """Format Surah header text based on configuration.

    Returns:
        (arabic_line, english_line) — either may be empty if hidden.
    """
    cfg = config or SubtitleConfig()

    if cfg.header_style == HEADER_HIDDEN:
        return ("", "")

    clean_name = arabic_surah_name.strip()
    if clean_name.startswith("سورة ") or clean_name.startswith("سُورَةُ "):
        arabic_line = clean_name
    else:
        arabic_line = f"سورة {clean_name}"

    if cfg.header_content == HEADER_SURAH_ONLY:
        english_line = ""
    elif cfg.header_content == HEADER_AR_TR_NAME:
        english_line = english_surah_name
    else:  # HEADER_AR_TR_AYAH
        if first_ayah == last_ayah:
            ayah_ref = f"{surah_number}:{first_ayah}"
        else:
            ayah_ref = f"{surah_number}:{first_ayah}–{last_ayah}"

        if cfg.ayah_reference_position == REF_TOP_HEADER:
            english_line = f"{english_surah_name} • {ayah_ref}"
        else:
            english_line = english_surah_name

    return (arabic_line, english_line)
