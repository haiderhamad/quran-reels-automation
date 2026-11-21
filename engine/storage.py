"""Backblaze B2 (S3-Compatible API) storage module for configuration, run state, history, and media."""

from __future__ import annotations

from engine.layout_config import TRANSLATION_MAX_LINES, SURA_HEADER_OFFSET, clamp_header_offset

import datetime
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

from engine.reel_theme_policy import eligible_verse_keys, evaluate_range


DEFAULT_B2_PREFIX = "quran-reels/"
DEFAULT_PRESIGNED_EXPIRY_SECONDS = 21600  # 6 hours
MIN_PRESIGNED_EXPIRY_SECONDS = 1
MAX_PRESIGNED_EXPIRY_SECONDS = 604800  # 7 days

LEGACY_PRODUCTION_SLOTS = ("morning", "evening")
VALID_RUN_KINDS = ("scheduled", "manual_production", "test")
TRANSLATION_STACK_GAPS = (80, 100, 120)
LEGACY_TRANSLATION_STACK_GAP_MAP = {4: 80, 6: 100, 8: 120, 10: 80, 20: 100, 30: 120}
ARABIC_TRANSLATION_GAPS = (80, 100, 120)
ARABIC_TRANSLATION_GAP_VERSION = 3
LEGACY_ARABIC_TRANSLATION_GAPS = {
    1: {6: 80, 8: 100, 10: 80, 15: 100, 20: 120, 40: 100, 60: 120},
    2: {20: 80, 40: 100, 60: 120},
}
LEGACY_TRANSLATION_FONT_SIZE_MAP = {60: 50, 55: 40, 50: 30}

VALID_SLOTS = (*LEGACY_PRODUCTION_SLOTS, "manual-production", "test")
VALID_STATUSES = ("STARTED", "SUCCESS", "FAILED")
VALID_PUBLISH_STATUSES = ("NOT_ATTEMPTED", "ACCEPTED", "SUCCESS", "FAILED", "BLOCKED", "UNKNOWN", "NOT_CONFIGURED")
CURRENT_SCHEMA_VERSION = 1
TOTAL_CANONICAL_AYAHS = 6236

_DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_REGEX = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SAFE_KEY_SEGMENT_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
_SCHEDULE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
_PUBLIC_DELIVERY_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{20,160}$")


class StorageError(Exception):
    """Raised when an error occurs during Backblaze B2 storage operations."""


def _validate_date_format(date_str: str, field_name: str = "date") -> None:
    if not isinstance(date_str, str) or not _DATE_REGEX.match(date_str):
        raise StorageError(f"Invalid {field_name} format '{date_str}'. Must be YYYY-MM-DD.")
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError as err:
        raise StorageError(f"Invalid calendar date '{date_str}': {err}") from err


def _validate_time_format(time_str: str, field_name: str = "time") -> None:
    if not isinstance(time_str, str) or not _TIME_REGEX.match(time_str):
        raise StorageError(f"Invalid {field_name} format '{time_str}'. Must be HH:MM (24-hour).")


def _sanitize_key_segment(segment: str, segment_name: str) -> str:
    if not isinstance(segment, str) or not segment.strip():
        raise StorageError(f"{segment_name} must be a non-empty string.")
    cleaned = segment.strip()
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned or not _SAFE_KEY_SEGMENT_REGEX.match(cleaned):
        raise StorageError(f"Invalid characters in {segment_name} '{cleaned}'.")
    return cleaned


@dataclass(frozen=True)
class PublishingScheduleEntry:
    """One editable Europe/Berlin daily publishing slot with stable identity."""

    id: str
    time: str
    enabled: bool = True

    def __post_init__(self) -> None:
        _sanitize_key_segment(self.id, "publishing schedule id")
        if not _SCHEDULE_ID_REGEX.fullmatch(self.id):
            raise StorageError("publishing schedule id contains invalid characters or exceeds 80 characters.")
        _validate_time_format(self.time, "publishing schedule time")
        if not isinstance(self.enabled, bool):
            raise StorageError("publishing schedule enabled must be a boolean.")


def _validate_publishing_schedule(entries: Sequence[PublishingScheduleEntry]) -> None:
    if len(entries) > 8:
        raise StorageError("publishing_schedule supports at most 8 entries.")
    ids = [entry.id for entry in entries]
    if len(ids) != len(set(ids)):
        raise StorageError("publishing_schedule entry IDs must be unique.")
    enabled_times = [entry.time for entry in entries if entry.enabled]
    if len(enabled_times) != len(set(enabled_times)):
        raise StorageError("publishing_schedule cannot contain duplicate enabled times.")


def is_production_run(record: "RunRecord") -> bool:
    """Return whether a run consumes production coverage and may publish."""
    return record.run_kind in ("scheduled", "manual_production")


@dataclass(frozen=True)
class B2Config:
    """Connection parameters and bucket scoping for Backblaze B2 (S3-Compatible API)."""

    endpoint_url: str
    region_name: str
    application_key_id: str
    application_key: str
    bucket_name: str
    prefix: str = DEFAULT_B2_PREFIX

    def __post_init__(self) -> None:
        if not self.endpoint_url or not isinstance(self.endpoint_url, str):
            raise StorageError("endpoint_url must be a non-empty string.")
        if not self.region_name or not isinstance(self.region_name, str):
            raise StorageError("region_name must be a non-empty string.")
        if self.region_name.strip().lower() == "auto":
            raise StorageError(
                "Backblaze B2 requires a concrete region name (e.g., 'eu-central-003', 'us-west-004'). 'auto' is not supported."
            )
        if not self.application_key_id or not isinstance(self.application_key_id, str):
            raise StorageError("application_key_id must be a non-empty string.")
        if not self.application_key or not isinstance(self.application_key, str):
            raise StorageError("application_key must be a non-empty string.")
        if not self.bucket_name or not isinstance(self.bucket_name, str):
            raise StorageError("bucket_name must be a non-empty string.")

        clean_prefix = self.prefix.strip().lstrip("/")
        if clean_prefix and not clean_prefix.endswith("/"):
            clean_prefix += "/"
        object.__setattr__(self, "prefix", clean_prefix)


@dataclass(frozen=True)
class StoredConfig:
    """Runtime and dashboard configuration stored in B2 (config/current.json)."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    automation_enabled: bool = False
    timezone: str = "Europe/Berlin"
    morning_enabled: bool = True
    morning_time: Optional[str] = None
    evening_enabled: bool = True
    evening_time: Optional[str] = None
    publishing_schedule: Sequence[PublishingScheduleEntry] = field(default_factory=tuple)
    translation_languages: Sequence[str] = field(default_factory=lambda: ("en",))
    min_duration_seconds: float = 15.0
    max_duration_seconds: float = 45.0
    background_clip_strategy: str = "auto_prefer_single"
    max_background_clips: int = 3
    target_background_clips: int = 1
    facebook_enabled: bool = True
    instagram_enabled: bool = True
    reciter_reuse_confirmed: bool = False
    # Subtitle & visual style configuration
    subtitle_mode: str = "progressive_phrase_sync"
    arabic_font_size: int = 180
    translation_font_size: int = 50
    arabic_max_lines: int = 2
    translation_max_lines: int = TRANSLATION_MAX_LINES["en"]
    arabic_translation_gap_px: int = 80
    arabic_translation_gap_version: int = ARABIC_TRANSLATION_GAP_VERSION
    translation_stack_gap_px: int = 80
    arabic_line_spacing: float = 1.15
    translation_line_spacing: float = 1.25
    text_outline: str = "minimal"
    text_shadow: str = "very_soft"
    highlight_mode: str = "phrase_highlight"
    highlight_intensity: str = "normal"
    highlight_style: str = "phrase_fade"
    highlight_color: str = "warm_white"
    transition_duration_ms: int = 100
    subtitle_lead_ms: int = 0
    sura_header_top_offset: int = SURA_HEADER_OFFSET["default"]
    header_style: str = "minimal_capsule"
    header_content: str = "arabic_translation_ayah_range"
    header_backing_intensity: str = "low"
    ayah_reference_position: str = "top_header"
    subtitle_backing: str = "shadow_only"

    def __post_init__(self) -> None:
        object.__setattr__(self, "sura_header_top_offset", clamp_header_offset(self.sura_header_top_offset))
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise StorageError(
                f"Unsupported config schema_version {self.schema_version}. Expected {CURRENT_SCHEMA_VERSION}."
            )
        if self.min_duration_seconds > self.max_duration_seconds:
            raise StorageError("min_duration_seconds cannot exceed max_duration_seconds.")
        if self.min_duration_seconds < 10.0 or self.max_duration_seconds > 60.0:
            raise StorageError("Duration bounds must stay within 10.0 to 60.0 seconds.")
        if self.background_clip_strategy not in ("auto_prefer_single", "single", "multiple"):
            raise StorageError("background_clip_strategy must be auto_prefer_single, single, or multiple.")
        if self.target_background_clips < 1 or self.target_background_clips > 3:
            raise StorageError("target_background_clips must be between 1 and 3.")
        if self.max_background_clips < 1 or self.max_background_clips > 3:
            raise StorageError("max_background_clips must be between 1 and 3.")
        if self.arabic_translation_gap_version != ARABIC_TRANSLATION_GAP_VERSION:
            raise StorageError("Unsupported arabic_translation_gap_version.")
        if self.arabic_translation_gap_px not in ARABIC_TRANSLATION_GAPS:
            raise StorageError("arabic_translation_gap_px must be 80, 100, or 120.")
        if self.translation_stack_gap_px not in TRANSLATION_STACK_GAPS:
            raise StorageError("translation_stack_gap_px must be 80, 100, or 120.")
        if self.translation_font_size not in (30, 40, 50):
            raise StorageError("translation_font_size must be 50 (Standard), 40 (Small), or 30 (Smaller).")
        if self.morning_time is not None:
            _validate_time_format(self.morning_time, "morning_time")
        if self.evening_time is not None:
            _validate_time_format(self.evening_time, "evening_time")
        _validate_publishing_schedule(self.publishing_schedule)
        languages = tuple(self.translation_languages)
        if languages not in (("en",), ("de",), ("en", "de")):
            raise StorageError("translation_languages must be ('en',), ('de',), or ordered ('en', 'de').")


@dataclass(frozen=True)
class RunRecord:
    """Audit record for a generated Reel run (runs/YYYY-MM-DD/slot.json)."""

    run_id: str
    date: str
    slot: str
    status: str
    started_at: str
    run_kind: Optional[str] = None
    schedule_slot_id: Optional[str] = None
    schema_version: int = CURRENT_SCHEMA_VERSION
    stage: Optional[str] = None
    completed_at: Optional[str] = None
    surah_number: Optional[int] = None
    surah_name_ar: Optional[str] = None
    surah_name_en: Optional[str] = None
    first_ayah: Optional[int] = None
    last_ayah: Optional[int] = None
    duration_seconds: Optional[float] = None
    pexels_video_ids: Sequence[int] = field(default_factory=tuple)
    media_key: Optional[str] = None
    final_safety_passed: Optional[bool] = None
    instagram_status: str = "NOT_ATTEMPTED"
    facebook_status: str = "NOT_ATTEMPTED"
    instagram_media_id: Optional[str] = None
    facebook_media_id: Optional[str] = None
    # Buffer-specific Facebook publishing provenance.  These are optional so
    # existing historical RunRecords remain readable without migration.
    publisher_backend: Optional[str] = None
    buffer_post_id: Optional[str] = None
    # Buffer acceptance is not proof that Facebook has completed asynchronous
    # media ingestion/publishing.  Keep those facts separately auditable.
    buffer_post_status: Optional[str] = None
    buffer_accepted_at: Optional[str] = None
    facebook_reel_url: Optional[str] = None
    facebook_published_at: Optional[str] = None
    # Random, path-safe public-delivery capability for a final-safe MP4.  It is
    # intentionally separate from the private B2 media key and is never used
    # for B2 application state.
    public_media_delivery_id: Optional[str] = None
    facebook_failure_reason: Optional[str] = None
    instagram_failure_reason: Optional[str] = None
    failure_reason: Optional[str] = None
    translation_languages: Sequence[str] = field(default_factory=lambda: ("en",))
    translation_sources: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise StorageError(
                f"Unsupported RunRecord schema_version {self.schema_version}. Expected {CURRENT_SCHEMA_VERSION}."
            )
        if not self.run_id or not isinstance(self.run_id, str):
            raise StorageError("run_id must be a non-empty string.")
        _validate_date_format(self.date, "date")
        resolved_kind = self.run_kind
        if resolved_kind is None:
            if self.slot in LEGACY_PRODUCTION_SLOTS:
                resolved_kind = "scheduled"
            elif self.slot == "test":
                resolved_kind = "test"
            elif self.slot == "manual-production":
                resolved_kind = "manual_production"
            else:
                raise StorageError(
                    f"Invalid legacy slot '{self.slot}'. Arbitrary schedule IDs require run_kind='scheduled'."
                )
        if resolved_kind not in VALID_RUN_KINDS:
            raise StorageError(f"Invalid run_kind '{resolved_kind}'. Must be one of {VALID_RUN_KINDS}.")
        object.__setattr__(self, "run_kind", resolved_kind)
        if resolved_kind == "scheduled":
            resolved_slot_id = self.schedule_slot_id or self.slot
            _sanitize_key_segment(resolved_slot_id, "schedule_slot_id")
            if not _SCHEDULE_ID_REGEX.fullmatch(resolved_slot_id):
                raise StorageError("schedule_slot_id contains invalid characters or exceeds 80 characters.")
            object.__setattr__(self, "schedule_slot_id", resolved_slot_id)
        elif self.schedule_slot_id is not None:
            raise StorageError("schedule_slot_id is only valid for scheduled runs.")
        expected_slot = "test" if resolved_kind == "test" else "manual-production" if resolved_kind == "manual_production" else self.slot
        if self.slot != expected_slot:
            raise StorageError(f"slot '{self.slot}' is inconsistent with run_kind '{resolved_kind}'.")
        if self.status not in VALID_STATUSES:
            raise StorageError(f"Invalid status '{self.status}'. Must be one of {VALID_STATUSES}.")
        if self.instagram_status not in VALID_PUBLISH_STATUSES:
            raise StorageError(f"Invalid instagram_status '{self.instagram_status}'.")
        if self.facebook_status not in VALID_PUBLISH_STATUSES:
            raise StorageError(f"Invalid facebook_status '{self.facebook_status}'.")
        if self.surah_number is not None and (self.surah_number < 1 or self.surah_number > 114):
            raise StorageError(f"Invalid surah_number '{self.surah_number}'. Must be between 1 and 114.")
        if self.first_ayah is not None and self.first_ayah < 1:
            raise StorageError("first_ayah must be >= 1.")
        if self.last_ayah is not None and self.first_ayah is not None and self.last_ayah < self.first_ayah:
            raise StorageError("last_ayah cannot be less than first_ayah.")
        if self.publisher_backend is not None and self.publisher_backend not in ("buffer", "meta"):
            raise StorageError("publisher_backend must be 'buffer', 'meta', or None.")
        if self.public_media_delivery_id is not None and not _PUBLIC_DELIVERY_ID_REGEX.fullmatch(self.public_media_delivery_id):
            raise StorageError("public_media_delivery_id has an invalid format.")


@dataclass(frozen=True)
class CoverageState:
    """Historical canonical coverage; active cycles complete across editorial eligibility."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    cycle_number: int = 1
    total_ayahs: int = TOTAL_CANONICAL_AYAHS
    covered_count: int = 0
    covered_ayahs: Sequence[str] = field(default_factory=tuple)  # list of "surah:ayah"
    processed_run_ids: Sequence[str] = field(default_factory=tuple)  # deduplication check
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_updated_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise StorageError(
                f"Unsupported CoverageState schema_version {self.schema_version}. Expected {CURRENT_SCHEMA_VERSION}."
            )
        if self.cycle_number < 1:
            raise StorageError("cycle_number must be >= 1.")
        if self.total_ayahs != TOTAL_CANONICAL_AYAHS:
            raise StorageError(f"total_ayahs must be {TOTAL_CANONICAL_AYAHS}.")
        if self.covered_count < 0 or self.covered_count > TOTAL_CANONICAL_AYAHS:
            raise StorageError(f"covered_count must be between 0 and {TOTAL_CANONICAL_AYAHS}.")

    @property
    def remaining_count(self) -> int:
        return max(0, self.total_ayahs - self.covered_count)

    @property
    def progress_percentage(self) -> float:
        if self.total_ayahs <= 0:
            return 0.0
        return round((self.covered_count / self.total_ayahs) * 100.0, 2)

    @property
    def is_cycle_completed(self) -> bool:
        approved = eligible_verse_keys()
        return bool(approved) and approved.issubset(self.covered_ayahs)

    @property
    def eligible_covered_count(self) -> int:
        return len(set(self.covered_ayahs) & eligible_verse_keys())

    def is_covered(self, surah: int, ayah: int) -> bool:
        """Return True if the specific surah:ayah is marked covered in this cycle."""
        return f"{surah}:{ayah}" in self.covered_ayahs


@dataclass(frozen=True)
class CoverageCycleArchive:
    """Immutable archive record of a completed Quran coverage cycle."""

    cycle_number: int
    total_ayahs: int
    covered_count: int
    covered_ayahs: Sequence[str]
    processed_run_ids: Sequence[str]
    started_at: str
    completed_at: str
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(frozen=True)
class BackgroundHistory:
    """Tracks cross-cycle footage pairing to avoid reusing same Pexels videos for an ayah."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    # Map "surah:ayah" -> tuple of Pexels video IDs used in previous cycles
    ayah_pexels_ids: Mapping[str, Sequence[int]] = field(default_factory=dict)

    def get_used_video_ids(self, ayah_keys: Sequence[str]) -> set[int]:
        """Return set of Pexels video IDs previously used for any of the given ayahs."""
        used = set()
        for k in ayah_keys:
            if k in self.ayah_pexels_ids:
                used.update(self.ayah_pexels_ids[k])
        return used

    def get_used_pexels_ids_for_ayahs(self, ayah_keys: Sequence[str]) -> set[int]:
        """Alias for get_used_video_ids."""
        return self.get_used_video_ids(ayah_keys)


def build_config_key(prefix: str = DEFAULT_B2_PREFIX) -> str:
    """Return canonical S3 object key for current configuration."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    return f"{clean_prefix}config/current.json"


def build_coverage_key(prefix: str = DEFAULT_B2_PREFIX) -> str:
    """Return canonical S3 object key for current Quran coverage state."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    return f"{clean_prefix}coverage/current.json"


def build_cycle_archive_key(
    cycle_number: int,
    prefix: str = DEFAULT_B2_PREFIX,
) -> str:
    """Return canonical S3 object key for a completed coverage cycle archive."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    return f"{clean_prefix}coverage/cycles/{cycle_number:04d}.json"


def build_background_history_key(prefix: str = DEFAULT_B2_PREFIX) -> str:
    """Return canonical S3 object key for cross-cycle background history."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    return f"{clean_prefix}coverage/background_history.json"


def build_scheduler_claim_key(
    date: str,
    slot_id: str,
    prefix: str = DEFAULT_B2_PREFIX,
) -> str:
    """Return the deterministic claim key used by an external scheduler tick."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    _validate_date_format(date, "date")
    clean_slot_id = _sanitize_key_segment(slot_id, "schedule_slot_id")
    if not _SCHEDULE_ID_REGEX.fullmatch(clean_slot_id):
        raise StorageError("schedule_slot_id contains invalid characters or exceeds 80 characters.")
    return f"{clean_prefix}scheduler/claims/{date}/{clean_slot_id}.json"


def build_run_key(
    date: str,
    slot: str,
    run_id: Optional[str] = None,
    prefix: str = DEFAULT_B2_PREFIX,
    run_kind: Optional[str] = None,
) -> str:
    """Return deterministic S3 object key for a run record."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"

    _validate_date_format(date, "date")
    clean_slot = _sanitize_key_segment(slot, "slot")

    resolved_kind = run_kind
    if resolved_kind is None:
        if clean_slot == "test":
            resolved_kind = "test"
        elif clean_slot == "manual-production":
            resolved_kind = "manual_production"
        else:
            resolved_kind = "scheduled"
    if resolved_kind not in VALID_RUN_KINDS:
        raise StorageError(f"Invalid run_kind '{resolved_kind}'.")

    if resolved_kind == "test":
        if not run_id:
            raise StorageError("run_id is required for test run records.")
        clean_run_id = _sanitize_key_segment(run_id, "run_id")
        return f"{clean_prefix}runs/test/{clean_run_id}.json"

    if resolved_kind == "manual_production":
        if not run_id:
            raise StorageError("run_id is required for manual production run records.")
        clean_run_id = _sanitize_key_segment(run_id, "run_id")
        return f"{clean_prefix}runs/manual-production/{clean_run_id}.json"

    if not _SCHEDULE_ID_REGEX.fullmatch(clean_slot):
        raise StorageError("schedule slot contains invalid characters or exceeds 80 characters.")

    if clean_slot in LEGACY_PRODUCTION_SLOTS:
        return f"{clean_prefix}runs/{date}/{clean_slot}.json"

    return f"{clean_prefix}runs/{date}/scheduled/{clean_slot}.json"


def build_media_key(
    date: str,
    slot: str,
    run_id: str,
    prefix: str = DEFAULT_B2_PREFIX,
    run_kind: Optional[str] = None,
) -> str:
    """Return deterministic S3 object key for a temporary Reel media file."""
    clean_prefix = prefix.strip().lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"

    _validate_date_format(date, "date")
    clean_slot = _sanitize_key_segment(slot, "slot")
    clean_run_id = _sanitize_key_segment(run_id, "run_id")

    resolved_kind = run_kind or (
        "test" if clean_slot == "test" else "manual_production" if clean_slot == "manual-production" else "scheduled"
    )
    if resolved_kind not in VALID_RUN_KINDS:
        raise StorageError(f"Invalid run_kind '{resolved_kind}'.")
    if resolved_kind == "test":
        return f"{clean_prefix}media/test/{clean_run_id}.mp4"
    if resolved_kind == "manual_production":
        return f"{clean_prefix}media/manual-production/{clean_run_id}.mp4"
    if not _SCHEDULE_ID_REGEX.fullmatch(clean_slot):
        raise StorageError("schedule slot contains invalid characters or exceeds 80 characters.")
    if clean_slot not in LEGACY_PRODUCTION_SLOTS:
        return f"{clean_prefix}media/{date}/scheduled/{clean_slot}/{clean_run_id}.mp4"

    return f"{clean_prefix}media/{date}/{clean_slot}/{clean_run_id}.mp4"


def stored_config_to_dict(config: StoredConfig) -> dict[str, Any]:
    data = {
        "schema_version": config.schema_version,
        "automation_enabled": config.automation_enabled,
        "timezone": config.timezone,
        "morning_enabled": config.morning_enabled,
        "morning_time": config.morning_time,
        "evening_enabled": config.evening_enabled,
        "evening_time": config.evening_time,
        "translation_languages": list(config.translation_languages),
        "min_duration_seconds": config.min_duration_seconds,
        "max_duration_seconds": config.max_duration_seconds,
        "background_clip_strategy": config.background_clip_strategy,
        "max_background_clips": config.max_background_clips,
        "target_background_clips": config.target_background_clips,
        "facebook_enabled": config.facebook_enabled,
        "instagram_enabled": config.instagram_enabled,
        "reciter_reuse_confirmed": config.reciter_reuse_confirmed,
        "subtitle_mode": config.subtitle_mode,
        "arabic_font_size": config.arabic_font_size,
        "translation_font_size": config.translation_font_size,
        "arabic_max_lines": config.arabic_max_lines,
        "translation_max_lines": config.translation_max_lines,
        "arabic_translation_gap_px": config.arabic_translation_gap_px,
        "arabic_translation_gap_version": config.arabic_translation_gap_version,
        "translation_stack_gap_px": config.translation_stack_gap_px,
        "arabic_line_spacing": config.arabic_line_spacing,
        "translation_line_spacing": config.translation_line_spacing,
        "text_outline": config.text_outline,
        "text_shadow": config.text_shadow,
        "highlight_mode": config.highlight_mode,
        "highlight_intensity": config.highlight_intensity,
        "highlight_style": config.highlight_style,
        "highlight_color": config.highlight_color,
        "transition_duration_ms": config.transition_duration_ms,
        "subtitle_lead_ms": config.subtitle_lead_ms,
        "sura_header_top_offset": config.sura_header_top_offset,
        "header_style": config.header_style,
        "header_content": config.header_content,
        "header_backing_intensity": config.header_backing_intensity,
        "ayah_reference_position": config.ayah_reference_position,
        "subtitle_backing": config.subtitle_backing,
    }
    if config.publishing_schedule:
        data["publishing_schedule"] = [
            {"id": entry.id, "time": entry.time, "enabled": entry.enabled}
            for entry in config.publishing_schedule
        ]
    return data


def stored_arabic_translation_gap(data: Mapping[str, Any]) -> int:
    """Resolve a persisted preset to its current spacing while preserving the selected rank."""
    version = data.get("arabic_translation_gap_version", 1)
    if type(version) is not int or version not in (1, 2, ARABIC_TRANSLATION_GAP_VERSION):
        raise StorageError("Unsupported arabic_translation_gap_version.")
    if "arabic_translation_gap_px" not in data:
        return 80
    raw = data["arabic_translation_gap_px"]
    if version == ARABIC_TRANSLATION_GAP_VERSION:
        if raw not in ARABIC_TRANSLATION_GAPS:
            raise StorageError("arabic_translation_gap_px must be 80, 100, or 120.")
        return int(raw)
    try:
        return LEGACY_ARABIC_TRANSLATION_GAPS[version][int(raw)]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise StorageError("Invalid arabic_translation_gap_px for the stored preset version.") from None


def stored_translation_stack_gap(data: Mapping[str, Any]) -> int:
    """Read the English/German preset, preserving the rank of older pixel values."""
    raw = data.get("translation_stack_gap_px", 80)
    if raw in TRANSLATION_STACK_GAPS:
        return int(raw)
    try:
        return LEGACY_TRANSLATION_STACK_GAP_MAP[raw]
    except (KeyError, TypeError):
        raise StorageError("Invalid translation_stack_gap_px preset.") from None


def stored_config_from_dict(data: Mapping[str, Any]) -> StoredConfig:
    schema_v = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    raw_schedule = data.get("publishing_schedule")
    if raw_schedule is None:
        migrated_schedule: list[PublishingScheduleEntry] = []
        morning_time = data.get("morning_time")
        evening_time = data.get("evening_time")
        if morning_time is not None:
            migrated_schedule.append(
                PublishingScheduleEntry(
                    id="morning",
                    time=str(morning_time),
                    enabled=bool(data.get("morning_enabled", True)),
                )
            )
        if evening_time is not None:
            migrated_schedule.append(
                PublishingScheduleEntry(
                    id="evening",
                    time=str(evening_time),
                    enabled=bool(data.get("evening_enabled", True)),
                )
            )
    else:
        if not isinstance(raw_schedule, Sequence) or isinstance(raw_schedule, (str, bytes)):
            raise StorageError("publishing_schedule must be an array.")
        if not 1 <= len(raw_schedule) <= 8:
            raise StorageError("An explicit publishing_schedule must contain between 1 and 8 entries.")
        migrated_schedule = []
        for item in raw_schedule:
            if not isinstance(item, Mapping):
                raise StorageError("Each publishing_schedule entry must be an object.")
            migrated_schedule.append(
                PublishingScheduleEntry(
                    id=str(item.get("id", "")),
                    time=str(item.get("time", "")),
                    enabled=item.get("enabled", True),
                )
            )
    has_legacy_indicators = (
        data.get("arabic_translation_gap_px") in (6, 8)
        or data.get("translation_stack_gap_px") in (4, 6, 8)
        or data.get("translation_font_size") in (60, 55)
        or (data.get("morning_time") is not None and "publishing_schedule" not in data)
    )
    raw_font_size_val = data.get("translation_font_size")
    if raw_font_size_val is not None:
        raw_font_size = int(raw_font_size_val)
        if raw_font_size == 60:
            migrated_font_size = 50
        elif raw_font_size == 55:
            migrated_font_size = 40
        elif raw_font_size == 50 and has_legacy_indicators:
            migrated_font_size = 30
        else:
            migrated_font_size = raw_font_size
    else:
        migrated_font_size = 50
    return StoredConfig(
        schema_version=schema_v,
        automation_enabled=bool(data.get("automation_enabled", False)),
        timezone=str(data.get("timezone", "Europe/Berlin")),
        morning_enabled=bool(data.get("morning_enabled", True)),
        morning_time=data.get("morning_time"),
        evening_enabled=bool(data.get("evening_enabled", True)),
        evening_time=data.get("evening_time"),
        publishing_schedule=tuple(migrated_schedule),
        translation_languages=tuple(data.get("translation_languages", ["en"])),
        min_duration_seconds=float(data.get("min_duration_seconds", 15.0)),
        max_duration_seconds=float(data.get("max_duration_seconds", 30.0)),
        background_clip_strategy=str(data.get("background_clip_strategy", "auto_prefer_single")),
        max_background_clips=int(data.get("max_background_clips", 3)),
        target_background_clips=int(data.get("target_background_clips", 1)),
        facebook_enabled=bool(data.get("facebook_enabled", True)),
        instagram_enabled=bool(data.get("instagram_enabled", True)),
        reciter_reuse_confirmed=bool(data.get("reciter_reuse_confirmed", False)),
        subtitle_mode=str(data.get("subtitle_mode", "progressive_phrase_sync")),
        arabic_font_size=int(data.get("arabic_font_size", 180)),
        translation_font_size=migrated_font_size,
        arabic_max_lines=int(data.get("arabic_max_lines", 2)),
        translation_max_lines=int(data.get("translation_max_lines", TRANSLATION_MAX_LINES["en"])),
        arabic_translation_gap_px=stored_arabic_translation_gap(data),
        translation_stack_gap_px=stored_translation_stack_gap(data),
        arabic_line_spacing=float(data.get("arabic_line_spacing", 1.15)),
        translation_line_spacing=float(data.get("translation_line_spacing", 1.25)),
        text_outline=str(data.get("text_outline", "minimal")),
        text_shadow=str(data.get("text_shadow", "very_soft")),
        highlight_mode=str(data.get("highlight_mode", "phrase_highlight")),
        highlight_intensity=str(data.get("highlight_intensity", "normal")),
        highlight_style=str(data.get("highlight_style", "phrase_fade")),
        highlight_color=str(data.get("highlight_color", "warm_white")),
        transition_duration_ms=int(data.get("transition_duration_ms", 100)),
        subtitle_lead_ms=int(data.get("subtitle_lead_ms", 0)),
        sura_header_top_offset=clamp_header_offset(data.get("sura_header_top_offset", SURA_HEADER_OFFSET["default"])),
        header_style=str(data.get("header_style", "minimal_capsule")),
        header_content=str(data.get("header_content", "arabic_translation_ayah_range")),
        header_backing_intensity=str(data.get("header_backing_intensity", "low")),
        ayah_reference_position=str(data.get("ayah_reference_position", "top_header")),
        subtitle_backing=str(data.get("subtitle_backing", "shadow_only")),
    )


def run_record_to_dict(record: RunRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "run_id": record.run_id,
        "date": record.date,
        "slot": record.slot,
        "run_kind": record.run_kind,
        "schedule_slot_id": record.schedule_slot_id,
        "status": record.status,
        "started_at": record.started_at,
        "stage": record.stage,
        "completed_at": record.completed_at,
        "surah_number": record.surah_number,
        "surah_name_ar": record.surah_name_ar,
        "surah_name_en": record.surah_name_en,
        "first_ayah": record.first_ayah,
        "last_ayah": record.last_ayah,
        "duration_seconds": record.duration_seconds,
        "pexels_video_ids": list(record.pexels_video_ids),
        "media_key": record.media_key,
        "final_safety_passed": record.final_safety_passed,
        "instagram_status": record.instagram_status,
        "facebook_status": record.facebook_status,
        "instagram_media_id": record.instagram_media_id,
        "facebook_media_id": record.facebook_media_id,
        "publisher_backend": record.publisher_backend,
        "buffer_post_id": record.buffer_post_id,
        "buffer_post_status": record.buffer_post_status,
        "buffer_accepted_at": record.buffer_accepted_at,
        "facebook_reel_url": record.facebook_reel_url,
        "facebook_published_at": record.facebook_published_at,
        "public_media_delivery_id": record.public_media_delivery_id,
        "facebook_failure_reason": record.facebook_failure_reason,
        "instagram_failure_reason": record.instagram_failure_reason,
        "failure_reason": record.failure_reason,
        "translation_languages": list(record.translation_languages),
        "translation_sources": {key: dict(value) for key, value in record.translation_sources.items()},
        "provenance": dict(record.provenance),
    }


def run_record_from_dict(data: Mapping[str, Any]) -> RunRecord:
    schema_v = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    return RunRecord(
        schema_version=schema_v,
        run_id=str(data["run_id"]),
        date=str(data["date"]),
        slot=str(data["slot"]),
        run_kind=data.get("run_kind"),
        schedule_slot_id=data.get("schedule_slot_id"),
        status=str(data["status"]),
        started_at=str(data["started_at"]),
        stage=data.get("stage"),
        completed_at=data.get("completed_at"),
        surah_number=data.get("surah_number"),
        surah_name_ar=data.get("surah_name_ar"),
        surah_name_en=data.get("surah_name_en"),
        first_ayah=data.get("first_ayah"),
        last_ayah=data.get("last_ayah"),
        duration_seconds=data.get("duration_seconds"),
        pexels_video_ids=tuple(data.get("pexels_video_ids", ())),
        media_key=data.get("media_key"),
        final_safety_passed=data.get("final_safety_passed"),
        instagram_status=data.get("instagram_status", "NOT_ATTEMPTED"),
        facebook_status=data.get("facebook_status", "NOT_ATTEMPTED"),
        instagram_media_id=data.get("instagram_media_id"),
        facebook_media_id=data.get("facebook_media_id"),
        publisher_backend=data.get("publisher_backend"),
        buffer_post_id=data.get("buffer_post_id"),
        buffer_post_status=data.get("buffer_post_status"),
        buffer_accepted_at=data.get("buffer_accepted_at"),
        facebook_reel_url=data.get("facebook_reel_url"),
        facebook_published_at=data.get("facebook_published_at"),
        public_media_delivery_id=data.get("public_media_delivery_id"),
        facebook_failure_reason=data.get("facebook_failure_reason"),
        instagram_failure_reason=data.get("instagram_failure_reason"),
        failure_reason=data.get("failure_reason"),
        translation_languages=tuple(data.get("translation_languages", ["en"])),
        translation_sources={
            str(key): dict(value)
            for key, value in dict(data.get("translation_sources", {})).items()
        },
        provenance=dict(data.get("provenance", {})),
    )


def coverage_state_to_dict(state: CoverageState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "cycle_number": state.cycle_number,
        "total_ayahs": state.total_ayahs,
        "covered_count": state.covered_count,
        "covered_ayahs": list(state.covered_ayahs),
        "processed_run_ids": list(state.processed_run_ids),
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "last_updated_at": state.last_updated_at,
    }


def coverage_state_from_dict(data: Mapping[str, Any]) -> CoverageState:
    schema_v = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    return CoverageState(
        schema_version=schema_v,
        cycle_number=int(data.get("cycle_number", 1)),
        total_ayahs=int(data.get("total_ayahs", TOTAL_CANONICAL_AYAHS)),
        covered_count=int(data.get("covered_count", 0)),
        covered_ayahs=tuple(data.get("covered_ayahs", ())),
        processed_run_ids=tuple(data.get("processed_run_ids", ())),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        last_updated_at=data.get("last_updated_at"),
    )


def coverage_cycle_archive_to_dict(archive: CoverageCycleArchive) -> dict[str, Any]:
    return {
        "schema_version": archive.schema_version,
        "cycle_number": archive.cycle_number,
        "total_ayahs": archive.total_ayahs,
        "covered_count": archive.covered_count,
        "covered_ayahs": list(archive.covered_ayahs),
        "processed_run_ids": list(archive.processed_run_ids),
        "started_at": archive.started_at,
        "completed_at": archive.completed_at,
    }


def coverage_cycle_archive_from_dict(data: Mapping[str, Any]) -> CoverageCycleArchive:
    schema_v = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    return CoverageCycleArchive(
        schema_version=schema_v,
        cycle_number=int(data["cycle_number"]),
        total_ayahs=int(data.get("total_ayahs", TOTAL_CANONICAL_AYAHS)),
        covered_count=int(data.get("covered_count", TOTAL_CANONICAL_AYAHS)),
        covered_ayahs=tuple(data.get("covered_ayahs", ())),
        processed_run_ids=tuple(data.get("processed_run_ids", ())),
        started_at=str(data["started_at"]),
        completed_at=str(data["completed_at"]),
    )


def background_history_to_dict(history: BackgroundHistory) -> dict[str, Any]:
    return {
        "schema_version": history.schema_version,
        "ayah_pexels_ids": {k: list(v) for k, v in history.ayah_pexels_ids.items()},
    }


def background_history_from_dict(data: Mapping[str, Any]) -> BackgroundHistory:
    schema_version = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    raw_map = data.get("ayah_pexels_ids", {})
    parsed_map = {k: tuple(v) for k, v in raw_map.items()}
    return BackgroundHistory(
        schema_version=schema_version,
        ayah_pexels_ids=parsed_map,
    )


def load_b2_config_from_env(env: Optional[Mapping[str, str]] = None) -> B2Config:
    """Load Backblaze B2 configuration from environment variables."""
    source = env if env is not None else os.environ

    endpoint_url = source.get("B2_ENDPOINT", "").strip()
    region_name = source.get("B2_REGION", "").strip()
    application_key_id = source.get("B2_APPLICATION_KEY_ID", "").strip()
    application_key = source.get("B2_APPLICATION_KEY", "").strip()
    bucket_name = source.get("B2_BUCKET_NAME", "").strip()
    prefix = source.get("B2_PREFIX", DEFAULT_B2_PREFIX).strip()

    missing = []
    if not endpoint_url:
        missing.append("B2_ENDPOINT")
    if not region_name:
        missing.append("B2_REGION")
    if not application_key_id:
        missing.append("B2_APPLICATION_KEY_ID")
    if not application_key:
        missing.append("B2_APPLICATION_KEY")
    if not bucket_name:
        missing.append("B2_BUCKET_NAME")

    if missing:
        raise StorageError(f"Missing required Backblaze B2 environment variables: {', '.join(missing)}")

    return B2Config(
        endpoint_url=endpoint_url,
        region_name=region_name,
        application_key_id=application_key_id,
        application_key=application_key,
        bucket_name=bucket_name,
        prefix=prefix,
    )


def create_b2_client(config: B2Config) -> Any:
    """Create S3 client configured for Backblaze B2 endpoint."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as err:
        raise StorageError(f"boto3 is required for Backblaze B2 storage: {err}") from err

    boto_config = Config(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
    )

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.application_key_id,
        aws_secret_access_key=config.application_key,
        region_name=config.region_name,
        config=boto_config,
    )


def _safe_error_message(err: Exception) -> str:
    """Return error message scrubbed of potential secrets or tokens."""
    msg = str(err)
    return msg


def load_config(client: Any, b2_config: B2Config) -> Optional[StoredConfig]:
    """Load and parse stored configuration from B2 (config/current.json)."""
    key = build_config_key(b2_config.prefix)
    try:
        resp = client.get_object(Bucket=b2_config.bucket_name, Key=key)
        body_bytes = resp["Body"].read()
        data = json.loads(body_bytes.decode("utf-8"))
        return stored_config_from_dict(data)
    except Exception as err:
        error_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            return None
        if isinstance(err, StorageError):
            raise
        raise StorageError(f"Failed to load configuration from B2: {_safe_error_message(err)}") from err


def save_config(client: Any, b2_config: B2Config, config: StoredConfig) -> str:
    """Serialize and save configuration to B2 (config/current.json)."""
    key = build_config_key(b2_config.prefix)
    data = stored_config_to_dict(config)
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        client.put_object(
            Bucket=b2_config.bucket_name,
            Key=key,
            Body=json_bytes,
            ContentType="application/json",
        )
        return key
    except Exception as err:
        raise StorageError(f"Failed to save configuration to B2: {_safe_error_message(err)}") from err


def put_run_record(client: Any, b2_config: B2Config, record: RunRecord) -> str:
    """Save or update a RunRecord in B2."""
    key = build_run_key(
        record.date,
        record.slot,
        record.run_id,
        b2_config.prefix,
        run_kind=record.run_kind,
    )
    data = run_record_to_dict(record)
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        client.put_object(
            Bucket=b2_config.bucket_name,
            Key=key,
            Body=json_bytes,
            ContentType="application/json",
        )
        return key
    except Exception as err:
        raise StorageError(f"Failed to put run record to B2: {_safe_error_message(err)}") from err


def get_run_record(
    client: Any,
    b2_config: B2Config,
    date: str,
    slot: str,
    run_id: Optional[str] = None,
    run_kind: Optional[str] = None,
) -> Optional[RunRecord]:
    """Retrieve RunRecord for a specific calendar date and slot."""
    key = build_run_key(date, slot, run_id=run_id, prefix=b2_config.prefix, run_kind=run_kind)
    return get_run_record_by_key(client, b2_config, key)


def get_run_record_by_key(client: Any, b2_config: B2Config, key: str) -> Optional[RunRecord]:
    """Retrieve and parse a RunRecord directly by S3 object key."""
    try:
        resp = client.get_object(Bucket=b2_config.bucket_name, Key=key)
        body_bytes = resp["Body"].read()
        data = json.loads(body_bytes.decode("utf-8"))
        return run_record_from_dict(data)
    except Exception as err:
        error_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            return None
        if isinstance(err, StorageError):
            raise
        raise StorageError(f"Failed to get run record from B2: {_safe_error_message(err)}") from err


def list_recent_runs(client: Any, b2_config: B2Config, limit: Optional[int] = 20) -> tuple[RunRecord, ...]:
    """List recent RunRecords across all slots and dates, sorted newest first."""
    if limit is not None and limit < 1:
        return ()

    runs_prefix = f"{b2_config.prefix}runs/"
    paginator = client.get_paginator("list_objects_v2")
    try:
        pages = paginator.paginate(Bucket=b2_config.bucket_name, Prefix=runs_prefix)
        keys = []
        for page in pages:
            for item in page.get("Contents", []):
                k = item["Key"]
                if k.endswith(".json"):
                    keys.append(k)

        records = []
        for k in keys:
            rec = get_run_record_by_key(client, b2_config, k)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda record: (record.started_at, record.run_id), reverse=True)
        return tuple(records[:limit])
    except Exception as err:
        if isinstance(err, StorageError):
            raise
        raise StorageError(f"Failed to list recent runs from B2: {_safe_error_message(err)}") from err


@dataclass(frozen=True)
class HistoricalBackgroundUse:
    provider: str
    provider_asset_id: str
    used_at: str


def historical_background_usage(
    row: Mapping[str, Any],
) -> tuple[HistoricalBackgroundUse, ...]:
    """Recover dated provider identities without guessing a variant or creating usage events."""
    for used_at in (row.get("completed_at"), row.get("started_at")):
        if not isinstance(used_at, str):
            continue
        try:
            parsed = datetime.datetime.fromisoformat(used_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            break
    else:
        return ()

    qualified = row.get("verified_backgrounds") or row.get("pexels_videos")
    identities: set[tuple[str, str]] = set()
    if qualified:
        # Both providers once shared the numeric pexels_video_ids field.
        for clip in qualified:
            if not isinstance(clip, Mapping):
                continue
            provider = clip.get("provider", "pexels")
            asset_id = str(clip.get("provider_asset_id") or clip.get("video_id") or "")
            if provider in ("pexels", "pixabay") and asset_id.isdecimal() and int(asset_id) > 0:
                identities.add((provider, asset_id))
    else:
        for asset in row.get("pexels_video_ids") or ():
            if str(asset).isdecimal() and int(asset) > 0:
                identities.add(("pexels", str(asset)))
    return tuple(HistoricalBackgroundUse(provider, asset_id, used_at)
                 for provider, asset_id in sorted(identities))


def background_usage_for_run(run: RunRecord) -> tuple[HistoricalBackgroundUse, ...]:
    if (run.run_kind not in ("scheduled", "manual_production") or run.status != "SUCCESS"
            or not run.media_key or run.final_safety_passed is False):
        return ()
    return historical_background_usage({
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "verified_backgrounds": run.provenance.get("verified_backgrounds"),
        "pexels_videos": run.provenance.get("pexels_videos"),
        "pexels_video_ids": run.pexels_video_ids,
    })


def recent_content_history_entries(runs: Sequence[RunRecord]) -> tuple[dict[str, Any], ...]:
    history = []
    for run in runs:
        if run.status == "SUCCESS" and run.surah_number is not None:
            history.append({
                "surah_number": run.surah_number,
                "first_ayah": run.first_ayah,
                "last_ayah": run.last_ayah,
                "pexels_video_ids": [int(use.provider_asset_id) for use in background_usage_for_run(run)
                                     if use.provider == "pexels"],
                "date": run.date,
                "slot": run.slot,
                "run_id": run.run_id,
            })
    return tuple(history)


def get_recent_content_history(
    client: Any,
    b2_config: B2Config,
    last_n_runs: int = 30,
) -> tuple[dict[str, Any], ...]:
    """Retrieve history of recently used Surahs and production Pexels video IDs."""
    return recent_content_history_entries(list_recent_runs(client, b2_config, limit=last_n_runs))


def upload_reel(
    client: Any,
    b2_config: B2Config,
    local_file_path: Optional[str] = None,
    date: str = "",
    slot: str = "",
    run_id: str = "",
    local_path: Optional[str] = None,
    run_kind: Optional[str] = None,
) -> str:
    """Upload rendered MP4 Reel video file to B2. Returns the stored S3 media key."""
    resolved_path = local_file_path or local_path
    if not resolved_path or not os.path.isfile(resolved_path):
        raise StorageError(f"Local Reel file does not exist: '{resolved_path}'")

    file_size = os.path.getsize(resolved_path)
    if file_size <= 0:
        raise StorageError(f"Local Reel file is empty (0 bytes): '{resolved_path}'")

    media_key = build_media_key(date, slot, run_id, b2_config.prefix, run_kind=run_kind)
    try:
        with open(resolved_path, "rb") as f:
            if hasattr(client, "upload_fileobj"):
                client.upload_fileobj(
                    f,
                    b2_config.bucket_name,
                    media_key,
                    ExtraArgs={"ContentType": "video/mp4"},
                )
            else:
                client.put_object(
                    Bucket=b2_config.bucket_name,
                    Key=media_key,
                    Body=f,
                    ContentType="video/mp4",
                )
        return media_key
    except Exception as err:
        raise StorageError(f"Failed to upload Reel media to B2: {_safe_error_message(err)}") from err


def delete_object(client: Any, b2_config: B2Config, object_key: str) -> None:
    """Delete an S3 object from B2."""
    try:
        client.delete_object(Bucket=b2_config.bucket_name, Key=object_key)
    except Exception as err:
        raise StorageError(f"Failed to delete object '{object_key}' from B2: {_safe_error_message(err)}") from err


def object_exists(client: Any, b2_config: B2Config, object_key: str) -> bool:
    """Check if an object exists in B2 bucket using head_object."""
    try:
        client.head_object(Bucket=b2_config.bucket_name, Key=object_key)
        return True
    except Exception as err:
        error_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            return False
        return False


def create_presigned_get_url(
    client: Any,
    b2_config: B2Config,
    media_key: str,
    expires_seconds: int = DEFAULT_PRESIGNED_EXPIRY_SECONDS,
) -> str:
    """Generate a temporary presigned GET URL for B2 S3 object.

    Expires in `expires_seconds` (default: 6 hours = 21600s, max: 7 days = 604800s).
    Security note: Treat returned URL as a temporary bearer credential; do NOT persist or log.
    """
    if (
        not isinstance(expires_seconds, int)
        or isinstance(expires_seconds, bool)
        or expires_seconds < MIN_PRESIGNED_EXPIRY_SECONDS
        or expires_seconds > MAX_PRESIGNED_EXPIRY_SECONDS
    ):
        raise StorageError(
            f"expires_seconds must be between {MIN_PRESIGNED_EXPIRY_SECONDS} and {MAX_PRESIGNED_EXPIRY_SECONDS} seconds."
        )

    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": b2_config.bucket_name, "Key": media_key},
            ExpiresIn=expires_seconds,
        )
        return url
    except Exception as err:
        raise StorageError(f"Failed to generate presigned GET URL: {_safe_error_message(err)}") from err


def load_coverage(client: Any, b2_config: B2Config) -> CoverageState:
    """Load current Quran coverage state from B2 (coverage/current.json). Returns default Cycle 1 if missing."""
    key = build_coverage_key(b2_config.prefix)
    try:
        resp = client.get_object(Bucket=b2_config.bucket_name, Key=key)
        body = resp["Body"].read().decode("utf-8")
        data = json.loads(body)
        return coverage_state_from_dict(data)
    except Exception as err:
        error_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return CoverageState(cycle_number=1, started_at=now_iso, last_updated_at=now_iso)
        raise StorageError(f"Failed to load coverage state from B2: {_safe_error_message(err)}") from err


def save_coverage(client: Any, b2_config: B2Config, state: CoverageState) -> str:
    """Save current coverage state to B2 (coverage/current.json)."""
    key = build_coverage_key(b2_config.prefix)
    data = coverage_state_to_dict(state)
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        client.put_object(
            Bucket=b2_config.bucket_name,
            Key=key,
            Body=json_bytes,
            ContentType="application/json",
        )
        return key
    except Exception as err:
        raise StorageError(f"Failed to save coverage state to B2: {_safe_error_message(err)}") from err


def archive_cycle(client: Any, b2_config: B2Config, archive: CoverageCycleArchive) -> str:
    """Archive a completed coverage cycle snapshot under coverage/cycles/{cycle:04d}.json."""
    key = build_cycle_archive_key(archive.cycle_number, b2_config.prefix)
    data = coverage_cycle_archive_to_dict(archive)
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        client.put_object(
            Bucket=b2_config.bucket_name,
            Key=key,
            Body=json_bytes,
            ContentType="application/json",
        )
        return key
    except Exception as err:
        raise StorageError(f"Failed to archive completed coverage cycle to B2: {_safe_error_message(err)}") from err


def load_background_history(client: Any, b2_config: B2Config) -> BackgroundHistory:
    """Load cross-cycle background history from B2 (coverage/background_history.json). Returns empty if missing."""
    key = build_background_history_key(b2_config.prefix)
    try:
        resp = client.get_object(Bucket=b2_config.bucket_name, Key=key)
        body = resp["Body"].read().decode("utf-8")
        data = json.loads(body)
        return background_history_from_dict(data)
    except Exception as err:
        error_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            return BackgroundHistory()
        raise StorageError(f"Failed to load background history from B2: {_safe_error_message(err)}") from err


def save_background_history(client: Any, b2_config: B2Config, history: BackgroundHistory) -> str:
    """Save cross-cycle background history to B2 (coverage/background_history.json)."""
    key = build_background_history_key(b2_config.prefix)
    data = background_history_to_dict(history)
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        client.put_object(
            Bucket=b2_config.bucket_name,
            Key=key,
            Body=json_bytes,
            ContentType="application/json",
        )
        return key
    except Exception as err:
        raise StorageError(f"Failed to save background history to B2: {_safe_error_message(err)}") from err


def update_coverage_for_run(
    client: Any,
    b2_config: B2Config,
    run_record: RunRecord,
    pexels_video_ids: Sequence[int] = (),
    now: Optional[datetime.datetime] = None,
) -> tuple[CoverageState, bool]:
    """Durably record covered ayahs and background history for a successful production run.

    Returns:
        (updated_coverage_state, cycle_completed_boolean)
    """
    coverage = load_coverage(client, b2_config)
    bg_history = load_background_history(client, b2_config)

    # 1. Test/non-production and non-successful runs MUST NEVER consume coverage.
    if not is_production_run(run_record) or run_record.status != "SUCCESS":
        return coverage, False

    if (
        run_record.surah_number is None
        or run_record.first_ayah is None
        or run_record.last_ayah is None
    ):
        return coverage, False

    verdict = evaluate_range(run_record.surah_number, run_record.first_ayah, run_record.last_ayah)
    if not verdict.eligible:
        raise StorageError(verdict.reason_code)

    # 2. Idempotency check: duplicate run_id produces zero duplicate coverage
    if run_record.run_id in coverage.processed_run_ids:
        return coverage, False

    current_time_str = (
        now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    ).isoformat()

    # Canonical ayah identifiers "surah:ayah"
    new_ayah_keys = [
        f"{run_record.surah_number}:{ayah_num}"
        for ayah_num in range(run_record.first_ayah, run_record.last_ayah + 1)
    ]

    all_covered = set(coverage.covered_ayahs)
    all_covered.update(new_ayah_keys)
    new_covered_count = len(all_covered)

    updated_run_ids = tuple(list(coverage.processed_run_ids) + [run_record.run_id])

    # 3. Update Background History
    updated_ayah_pexels = dict(bg_history.ayah_pexels_ids)
    all_p_ids = tuple(sorted(set(list(run_record.pexels_video_ids) + list(pexels_video_ids))))
    if all_p_ids:
        for k in new_ayah_keys:
            existing = set(updated_ayah_pexels.get(k, ()))
            existing.update(all_p_ids)
            updated_ayah_pexels[k] = tuple(sorted(existing))

    updated_bg_history = BackgroundHistory(ayah_pexels_ids=updated_ayah_pexels)
    save_background_history(client, b2_config, updated_bg_history)

    # Completion concerns only the approved corpus; historical keys remain auditable.
    approved = eligible_verse_keys()
    if approved and approved.issubset(all_covered):
        # Archive completed cycle
        archive = CoverageCycleArchive(
            cycle_number=coverage.cycle_number,
            total_ayahs=TOTAL_CANONICAL_AYAHS,
            covered_count=new_covered_count,
            covered_ayahs=tuple(sorted(all_covered)),
            processed_run_ids=updated_run_ids,
            started_at=coverage.started_at or current_time_str,
            completed_at=current_time_str,
        )
        archive_cycle(client, b2_config, archive)

        # Initialize Cycle N+1 (empty covered list, starting next production generation)
        next_cycle_state = CoverageState(
            cycle_number=coverage.cycle_number + 1,
            total_ayahs=TOTAL_CANONICAL_AYAHS,
            covered_count=0,
            covered_ayahs=(),
            processed_run_ids=(run_record.run_id,),
            started_at=current_time_str,
            completed_at=None,
            last_updated_at=current_time_str,
        )
        save_coverage(client, b2_config, next_cycle_state)
        return next_cycle_state, True
    else:
        # Normal update inside current cycle
        new_state = CoverageState(
            cycle_number=coverage.cycle_number,
            total_ayahs=TOTAL_CANONICAL_AYAHS,
            covered_count=new_covered_count,
            covered_ayahs=tuple(sorted(all_covered)),
            processed_run_ids=updated_run_ids,
            started_at=coverage.started_at or current_time_str,
            completed_at=None,
            last_updated_at=current_time_str,
        )
        save_coverage(client, b2_config, new_state)
        return new_state, False


def reconcile_coverage_from_runs(
    client: Any,
    b2_config: B2Config,
    run_records: Sequence[RunRecord],
) -> CoverageState:
    """Reconcile missing production run records into coverage state idempotently."""
    coverage = load_coverage(client, b2_config)
    for run in run_records:
        if (
            is_production_run(run)
            and run.status == "SUCCESS"
            and run.first_ayah is not None
            and run.run_id not in coverage.processed_run_ids
            and evaluate_range(run.surah_number, run.first_ayah, run.last_ayah).eligible
        ):
            coverage, _ = update_coverage_for_run(client, b2_config, run)
    return coverage


def get_coverage_summary(coverage: CoverageState) -> dict:
    """Return human-readable and dashboard-ready summary of current coverage state."""
    return {
        "eligible_total_ayahs": len(eligible_verse_keys()),
        "eligible_covered_count": coverage.eligible_covered_count,
        "eligible_remaining_count": len(eligible_verse_keys()) - coverage.eligible_covered_count,
        "cycle_number": coverage.cycle_number,
        "total_ayahs": coverage.total_ayahs,
        "covered_count": coverage.covered_count,
        "remaining_count": coverage.remaining_count,
        "progress_percentage": coverage.progress_percentage,
        "started_at": coverage.started_at,
        "last_updated_at": coverage.last_updated_at,
    }
