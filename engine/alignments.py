"""Source-bound reviewed Arabic/English phrase alignments.

Production phrase progression is allowed only when a repository record is
explicitly reviewed and still matches the exact canonical Arabic and Rowwad
English sources.  Candidate-generation heuristics deliberately live elsewhere
and are never consulted by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ALIGNMENT_SCHEMA_VERSION = 1
ALIGNMENT_DATA_VERSION = "english_rwwad-v1"
TOKENIZATION_VERSION = "whitespace_v1"
TIMING_STRATEGY_ARABIC_BASE_LETTER_WEIGHT_V1 = "arabic_base_letter_weight_v1"
STATUS_VERIFIED = "verified"
SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE = "VERIFIED_PHRASE_PROGRESSIVE"
SUBTITLE_MODE_FULL_AYAH_FALLBACK = "FULL_AYAH_FALLBACK"
DEFAULT_ALIGNMENT_ROOT = (
    Path(__file__).resolve().parents[1] / "alignments" / "english_rwwad" / "v1"
)

_VERSE_KEY_RE = re.compile(r"^([1-9]|[1-9][0-9]|1[01][0-9]|11[0-4]):([1-9][0-9]{0,2})$")


class AlignmentValidationError(ValueError):
    """Raised when reviewed alignment data is malformed or source-stale."""


@dataclass(frozen=True)
class AlignmentPanel:
    """One contiguous half-open Arabic/English token span."""

    panel_index: int
    arabic_word_start: int
    arabic_word_end: int
    english_token_start: int
    english_token_end: int
    arabic_text: Optional[str] = None
    english_text: Optional[str] = None


@dataclass(frozen=True)
class AlignmentRecord:
    """Parsed repository alignment record before source validation."""

    schema_version: int
    data_version: str
    verse_key: str
    arabic_source: str
    arabic_normalized_sha256: str
    english_translation_key: str
    english_translation_version: str
    english_normalized_sha256: str
    tokenization_version: str
    status: str
    reviewed_at: str
    review_note: str
    timing_strategy: str
    panels: tuple[AlignmentPanel, ...]
    source_path: str


@dataclass(frozen=True)
class ValidatedAlignmentPanel:
    """Exact source slices proven by a validated record."""

    panel_index: int
    arabic_text: str
    english_text: str


@dataclass(frozen=True)
class ValidatedAlignment:
    """Production-eligible reviewed alignment and its audit provenance."""

    record: AlignmentRecord
    panels: tuple[ValidatedAlignmentPanel, ...]

    def provenance(self) -> dict[str, Any]:
        return {
            "verse_key": self.record.verse_key,
            "subtitle_mode": SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE,
            "alignment_schema_version": self.record.schema_version,
            "alignment_data_version": self.record.data_version,
            "alignment_path": self.record.source_path,
            "alignment_status": self.record.status,
            "arabic_source_hash": self.record.arabic_normalized_sha256,
            "english_source_hash": self.record.english_normalized_sha256,
            "english_translation_key": self.record.english_translation_key,
            "english_translation_version": self.record.english_translation_version,
            "timing_strategy": self.record.timing_strategy,
        }


def normalize_source_text(text: str) -> str:
    """Apply the manifest's stable whitespace-only normalization."""
    if not isinstance(text, str) or not text.strip():
        raise AlignmentValidationError("Alignment source text must be non-empty.")
    return " ".join(text.split())


def normalized_sha256(text: str) -> str:
    """Hash normalized UTF-8 source text without Unicode rewriting."""
    return hashlib.sha256(normalize_source_text(text).encode("utf-8")).hexdigest()


def alignment_path_for_verse(
    verse_key: str,
    root: Path = DEFAULT_ALIGNMENT_ROOT,
) -> Path:
    """Resolve one deterministic repository path without accepting traversal."""
    match = _VERSE_KEY_RE.fullmatch(verse_key)
    if match is None:
        raise AlignmentValidationError(f"Invalid verse_key: {verse_key!r}.")
    surah = int(match.group(1))
    ayah = int(match.group(2))
    return root / f"{surah:03d}" / f"{ayah:03d}.json"


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    context: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AlignmentValidationError(
            f"{context} keys mismatch; missing={missing}, extra={extra}."
        )


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlignmentValidationError(f"{field_name} must be a non-empty string.")
    return value


def _require_index(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AlignmentValidationError(f"{field_name} must be a non-negative integer.")
    return value


def parse_alignment_record(
    raw: Mapping[str, Any],
    *,
    source_path: str,
) -> AlignmentRecord:
    """Parse one schema-v1 record and reject silent/unknown fields."""
    if not isinstance(raw, Mapping):
        raise AlignmentValidationError("Alignment record must be a JSON object.")
    _require_exact_keys(
        raw,
        {
            "schema_version",
            "data_version",
            "verse_key",
            "arabic",
            "english",
            "tokenization_version",
            "status",
            "reviewed_at",
            "review_note",
            "timing_strategy",
            "panels",
        },
        "alignment record",
    )
    if raw["schema_version"] != ALIGNMENT_SCHEMA_VERSION:
        raise AlignmentValidationError(
            f"Unsupported alignment schema_version {raw['schema_version']!r}."
        )
    if raw["data_version"] != ALIGNMENT_DATA_VERSION:
        raise AlignmentValidationError(
            f"Unsupported alignment data_version {raw['data_version']!r}."
        )

    arabic = raw["arabic"]
    english = raw["english"]
    if not isinstance(arabic, Mapping) or not isinstance(english, Mapping):
        raise AlignmentValidationError("arabic and english metadata must be objects.")
    _require_exact_keys(arabic, {"source", "normalized_sha256"}, "arabic metadata")
    _require_exact_keys(
        english,
        {"translation_key", "translation_version", "normalized_sha256"},
        "english metadata",
    )

    raw_panels = raw["panels"]
    if not isinstance(raw_panels, list) or not raw_panels:
        raise AlignmentValidationError("panels must be a non-empty array.")
    panels: list[AlignmentPanel] = []
    for index, item in enumerate(raw_panels):
        if not isinstance(item, Mapping):
            raise AlignmentValidationError(f"panels[{index}] must be an object.")
        _require_exact_keys(
            item,
            {
                "panel_index",
                "arabic_word_start",
                "arabic_word_end",
                "english_token_start",
                "english_token_end",
                "arabic_text",
                "english_text",
            },
            f"panels[{index}]",
        )
        panels.append(
            AlignmentPanel(
                panel_index=_require_index(item["panel_index"], f"panels[{index}].panel_index"),
                arabic_word_start=_require_index(
                    item["arabic_word_start"], f"panels[{index}].arabic_word_start"
                ),
                arabic_word_end=_require_index(
                    item["arabic_word_end"], f"panels[{index}].arabic_word_end"
                ),
                english_token_start=_require_index(
                    item["english_token_start"], f"panels[{index}].english_token_start"
                ),
                english_token_end=_require_index(
                    item["english_token_end"], f"panels[{index}].english_token_end"
                ),
                arabic_text=_require_string(item["arabic_text"], f"panels[{index}].arabic_text"),
                english_text=_require_string(item["english_text"], f"panels[{index}].english_text"),
            )
        )

    return AlignmentRecord(
        schema_version=ALIGNMENT_SCHEMA_VERSION,
        data_version=ALIGNMENT_DATA_VERSION,
        verse_key=_require_string(raw["verse_key"], "verse_key"),
        arabic_source=_require_string(arabic["source"], "arabic.source"),
        arabic_normalized_sha256=_require_string(
            arabic["normalized_sha256"], "arabic.normalized_sha256"
        ),
        english_translation_key=_require_string(
            english["translation_key"], "english.translation_key"
        ),
        english_translation_version=_require_string(
            english["translation_version"], "english.translation_version"
        ),
        english_normalized_sha256=_require_string(
            english["normalized_sha256"], "english.normalized_sha256"
        ),
        tokenization_version=_require_string(
            raw["tokenization_version"], "tokenization_version"
        ),
        status=_require_string(raw["status"], "status"),
        reviewed_at=_require_string(raw["reviewed_at"], "reviewed_at"),
        review_note=_require_string(raw["review_note"], "review_note"),
        timing_strategy=_require_string(raw["timing_strategy"], "timing_strategy"),
        panels=tuple(panels),
        source_path=source_path,
    )


def load_alignment_record(
    verse_key: str,
    *,
    root: Path = DEFAULT_ALIGNMENT_ROOT,
) -> Optional[AlignmentRecord]:
    """Load one record, returning ``None`` only when no record exists."""
    path = alignment_path_for_verse(verse_key, root)
    if not path.is_file():
        return None
    def reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise AlignmentValidationError(
                    f"Duplicate JSON key {key!r} in alignment {path}."
                )
            value[key] = item
        return value

    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        raise AlignmentValidationError(f"Failed to read alignment {path}: {err}") from err
    try:
        relative = path.resolve().relative_to(Path(__file__).resolve().parents[1])
        source_path = relative.as_posix()
    except ValueError:
        source_path = str(path.resolve())
    return parse_alignment_record(raw, source_path=source_path)


def validate_alignment_record(
    record: AlignmentRecord,
    *,
    verse_key: str,
    arabic_text: str,
    english_text: str,
    english_translation_key: str,
    english_translation_version: str,
) -> ValidatedAlignment:
    """Prove that a reviewed record exactly covers the current source pair."""
    if record.status != STATUS_VERIFIED:
        raise AlignmentValidationError("Alignment status is not verified.")
    panels = validate_alignment_sources(
        record, verse_key=verse_key, arabic_text=arabic_text,
        english_text=english_text, english_translation_key=english_translation_key,
        english_translation_version=english_translation_version,
    )
    return ValidatedAlignment(record=record, panels=panels)


def validate_alignment_sources(
    record: AlignmentRecord,
    *,
    verse_key: str,
    arabic_text: str,
    english_text: str,
    english_translation_key: str,
    english_translation_version: str,
) -> tuple[ValidatedAlignmentPanel, ...]:
    """Check exact source coverage only, without granting editorial approval.

    Offline proposals may use these slices for human review. Production must
    still call validate_alignment_record, which separately requires verified status.
    """
    if record.verse_key != verse_key:
        raise AlignmentValidationError(
            f"Alignment verse_key mismatch: expected {verse_key}, got {record.verse_key}."
        )
    if record.arabic_source != "quran_foundation_text_uthmani":
        raise AlignmentValidationError(f"Unsupported Arabic source {record.arabic_source!r}.")
    if record.tokenization_version != TOKENIZATION_VERSION:
        raise AlignmentValidationError(
            f"Unsupported tokenization_version {record.tokenization_version!r}."
        )
    if record.timing_strategy != TIMING_STRATEGY_ARABIC_BASE_LETTER_WEIGHT_V1:
        raise AlignmentValidationError(
            f"Unsupported timing_strategy {record.timing_strategy!r}."
        )
    if record.english_translation_key != english_translation_key:
        raise AlignmentValidationError("English translation key mismatch.")
    if record.english_translation_version != english_translation_version:
        raise AlignmentValidationError("English translation version mismatch.")
    if record.arabic_normalized_sha256 != normalized_sha256(arabic_text):
        raise AlignmentValidationError("Canonical Arabic source hash mismatch.")
    if record.english_normalized_sha256 != normalized_sha256(english_text):
        raise AlignmentValidationError("Exact Rowwad English source hash mismatch.")

    arabic_words = normalize_source_text(arabic_text).split(" ")
    english_tokens = normalize_source_text(english_text).split(" ")
    expected_arabic_start = 0
    expected_english_start = 0
    validated: list[ValidatedAlignmentPanel] = []
    for expected_index, panel in enumerate(record.panels):
        if panel.panel_index != expected_index:
            raise AlignmentValidationError("Panel indices must be consecutive and ordered.")
        if panel.arabic_word_start != expected_arabic_start:
            raise AlignmentValidationError("Arabic ranges must be contiguous and non-overlapping.")
        if panel.english_token_start != expected_english_start:
            raise AlignmentValidationError("English ranges must be contiguous and non-overlapping.")
        if not (
            panel.arabic_word_start < panel.arabic_word_end <= len(arabic_words)
        ):
            raise AlignmentValidationError("Arabic panel range is empty or out of bounds.")
        if not (
            panel.english_token_start < panel.english_token_end <= len(english_tokens)
        ):
            raise AlignmentValidationError("English panel range is empty or out of bounds.")
        arabic_panel_text = " ".join(
            arabic_words[panel.arabic_word_start : panel.arabic_word_end]
        )
        english_panel_text = " ".join(
            english_tokens[panel.english_token_start : panel.english_token_end]
        )
        if normalize_source_text(panel.arabic_text or "") != arabic_panel_text:
            raise AlignmentValidationError("Stored Arabic panel text does not match its source range.")
        if normalize_source_text(panel.english_text or "") != english_panel_text:
            raise AlignmentValidationError("Stored English panel text does not match its source range.")
        validated.append(
            ValidatedAlignmentPanel(
                panel_index=panel.panel_index,
                arabic_text=arabic_panel_text,
                english_text=english_panel_text,
            )
        )
        expected_arabic_start = panel.arabic_word_end
        expected_english_start = panel.english_token_end

    if expected_arabic_start != len(arabic_words):
        raise AlignmentValidationError("Arabic ranges do not cover the complete source.")
    if expected_english_start != len(english_tokens):
        raise AlignmentValidationError("English ranges do not cover the complete source.")
    if " ".join(item.arabic_text for item in validated).split() != arabic_words:
        raise AlignmentValidationError("Arabic reconstruction is not exact.")
    if " ".join(item.english_text for item in validated).split() != english_tokens:
        raise AlignmentValidationError("English reconstruction is not exact.")
    return tuple(validated)


def load_validated_alignment(
    *,
    verse_key: str,
    arabic_text: str,
    english_text: str,
    english_translation_key: str,
    english_translation_version: str,
    root: Path = DEFAULT_ALIGNMENT_ROOT,
) -> Optional[ValidatedAlignment]:
    """Load and validate one production record; missing records return ``None``."""
    record = load_alignment_record(verse_key, root=root)
    if record is None:
        return None
    return validate_alignment_record(
        record,
        verse_key=verse_key,
        arabic_text=arabic_text,
        english_text=english_text,
        english_translation_key=english_translation_key,
        english_translation_version=english_translation_version,
    )


def count_verified_alignments(root: Path = DEFAULT_ALIGNMENT_ROOT) -> int:
    """Count parseable verified records for diagnostics without source validation."""
    count = 0
    if not root.is_dir():
        return 0
    for path in sorted(root.glob("[0-9][0-9][0-9]/[0-9][0-9][0-9].json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        record = parse_alignment_record(raw, source_path=str(path))
        if record.status == STATUS_VERIFIED:
            count += 1
    return count


def build_panels_from_alignment(
    alignment: ValidatedAlignment,
) -> Sequence[tuple[str, str]]:
    """Expose exact reviewed source panels to the subtitle timing builder."""
    return tuple((panel.arabic_text, panel.english_text) for panel in alignment.panels)
