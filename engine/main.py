"""Local end-to-end Quran Reel orchestration pipeline."""

from __future__ import annotations

import os
import json
import hashlib
import random
import shutil
import tempfile
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from engine.audio import (
    DEFAULT_AUDIO_SERVER,
    DEFAULT_RECITER_NAME,
    DEFAULT_RIWAYA_NAME,
    DEFAULT_TIMING_READ_ID,
    EXPECTED_SURAH_COUNT,
    SurahAudio,
    TimingReadMetadata,
    build_surah_audio_url,
    download_surah_audio,
    fetch_available_timing_soar,
    fetch_surah_timing,
    fetch_timing_read_metadata,
    to_engine_verse_timing,
)
from engine.backgrounds import (
    ALLOWED_NATURE_QUERIES,
    BackgroundCandidate,
    DownloadedBackground,
    DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES,
    background_candidate_runtime_rank,
    candidate_cost_rejection_reason,
    download_background,
    search_background_candidates,
)
from engine.background_pool import (
    BackgroundProvider,
    BackgroundUsageEvent,
    VerifiedBackgroundRecord,
    compute_render_config_fingerprint,
    select_verified_pool_records,
    background_reuse_warnings,
    make_provider,
)
from engine.config import (
    DEFAULT_MINIMUM_DURATION_SECONDS,
    DEFAULT_MAXIMUM_DURATION_SECONDS,
    MAXIMUM_ALLOWED_DURATION_SECONDS,
    MINIMUM_ALLOWED_DURATION_SECONDS,
    DurationConfig,
    VerseSelectionLimits,
)
from engine.alignments import SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE, normalized_sha256
from engine.quran import (
    QuranData,
    QuranSourceError,
    fetch_quran_data,
    validate_quran_source_for_run,
)
from engine.renderer import (
    RenderAyah,
    RenderBackground,
    RenderConfig,
    RenderError,
    RenderRequest,
    RenderResult,
    RenderTranslation,
    SubtitlePreflightError,
    calculate_background_segments,
    preflight_subtitle_layout,
    resolve_render_ayah_subtitle_plan,
    render_reel,
)
from engine.safety import SafetyConfig, SafetyScanResult, scan_video
from engine.storage import HistoricalBackgroundUse
from engine.translation import (
    EXPECTED_TRANSLATION_VERSIONS,
    SUPPORTED_TRANSLATION_SOURCES,
    TranslationData,
    fetch_translation_data_for_language,
)
from engine.verses import (
    VerseCandidate, VerseSelectionError, select_verse_candidate,
    select_owner_v1_ayah_candidate,
)
from engine.calm_reels_cycle import owner_ordered_keys, FreshCycleError
from engine.owner_reviewed_passages import REVIEWED_OWNER_PASSAGES, ReviewedPassageApproval
from engine.reel_publication_policy import TRANSLATION_HASH_FIELDS
from engine.reel_theme_policy import (
    THEME_POLICY_VERSION,
    eligible_verse_keys,
    evaluate_range,
    require_candidate_eligible,
)
from engine.production_authorization import (
    ProductionAuthorizationCatalog,
)
from engine.visual_coherence import (
    are_all_clips_coherent,
    extract_visual_attributes,
    plan_background_clips,
)


# Pipeline Stages
STAGE_STARTED = "STARTED"
STAGE_FETCHING_QURAN = "FETCHING_QURAN"
STAGE_FETCHING_TRANSLATION = "FETCHING_TRANSLATION"
STAGE_FETCHING_AUDIO_METADATA = "FETCHING_AUDIO_METADATA"
STAGE_SELECTING_VERSES = "SELECTING_VERSES"
STAGE_PREFLIGHTING_SUBTITLES = "PREFLIGHTING_SUBTITLES"
STAGE_DOWNLOADING_AUDIO = "DOWNLOADING_AUDIO"
STAGE_SELECTING_BACKGROUNDS = "SELECTING_BACKGROUNDS"
STAGE_SCANNING_BACKGROUNDS = "SCANNING_BACKGROUNDS"
STAGE_RENDERING = "RENDERING"
STAGE_FINAL_SAFETY_SCAN = "FINAL_SAFETY_SCAN"
STAGE_SUCCESS = "SUCCESS"
STAGE_FAILED = "FAILED"

# Exact MP3Quran read 92, Surah 1, independently matched to the isolated ayah recording.
FATIHA_READ_92_MP3_SHA256 = '08237b30a2fcdabe9df6633208442395b5bbb0cd0e612f15bb43de5d0ed72a33'


def _verify_opening_recording(audio_path: str) -> None:
    with open(audio_path, 'rb') as audio_file:
        digest = hashlib.file_digest(audio_file, 'sha256').hexdigest()
    if digest != FATIHA_READ_92_MP3_SHA256:
        raise GenerationError('OWNER_V1_OPENING_AUDIO_CHANGED')


ALLOWED_SLOTS = ("morning", "evening", "test")
ALLOWED_RUN_KINDS = ("scheduled", "manual_production", "test")
DEFAULT_MAX_TEST_CANDIDATE_ATTEMPTS = 64
MAX_TEST_CANDIDATE_ATTEMPTS = 293
TEST_VERIFIED_VERSE_KEYS = ("2:21",)
DEFAULT_MAX_BACKGROUND_SEARCH_SECONDS = 600.0
DEFAULT_MAX_CANDIDATE_PROCESSING_SECONDS = 45.0

LOGGER = logging.getLogger("quran_reels.engine")


class GenerationError(Exception):
    """Raised when the local Quran Reel generation pipeline fails."""


@dataclass(frozen=True)
class RunRequest:
    """Input configuration for a single local Reel generation run."""

    slot: str = "morning"
    run_kind: Optional[str] = None
    seed: Optional[int] = None
    recent_ayah_ranges: tuple[tuple[int, int, int], ...] = ()  # (surah, first_ayah, last_ayah)
    recent_pexels_video_ids: tuple[int, ...] = ()
    historical_pexels_video_ids: tuple[int, ...] = ()
    covered_ayahs: tuple[str, ...] = ()
    output_path: Optional[str] = None
    background_clip_strategy: str = "auto_prefer_single"  # "auto_prefer_single" | "single" | "multiple"
    max_background_clips: int = 3
    target_background_clips: int = 1
    max_background_attempts: int = 40
    max_background_search_seconds: float = DEFAULT_MAX_BACKGROUND_SEARCH_SECONDS
    max_candidate_processing_seconds: float = DEFAULT_MAX_CANDIDATE_PROCESSING_SECONDS
    max_background_source_file_size_bytes: int = DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES
    max_render_attempts: int = 4
    verified_background_records: tuple[VerifiedBackgroundRecord, ...] = ()
    background_usage_events: tuple[BackgroundUsageEvent | HistoricalBackgroundUse, ...] = ()
    max_surah_attempts: int = 15
    dry_run: bool = False
    requested_ayah_range: Optional[tuple[int, int, int]] = None
    test_verse_key: Optional[str] = None
    max_test_candidate_attempts: int = DEFAULT_MAX_TEST_CANDIDATE_ATTEMPTS
    translation_languages: tuple[str, ...] = ("en",)
    duration_config: DurationConfig = field(default_factory=DurationConfig)
    verse_limits: VerseSelectionLimits = field(default_factory=VerseSelectionLimits)
    render_config: RenderConfig = field(default_factory=RenderConfig)
    production_authorization: Optional[ProductionAuthorizationCatalog] = None
    owner_ayah_key: Optional[str] = None  # explicit technical-only owner-v1 source target
    owner_cycle_number: Optional[int] = None  # must be rechecked by DB claim later
    owner_pending_successor: str | None = None
    owner_reviewed_ayah_keys: tuple[str, ...] = ()
    owner_reviewed_registry: tuple[tuple[str, ...], ...] = REVIEWED_OWNER_PASSAGES
    owner_reviewed_approval: ReviewedPassageApproval | None = None

    def __post_init__(self) -> None:
        if self.render_config.allow_unverified_alignment_preview:
            raise GenerationError("UNVERIFIED_ALIGNMENT_PREVIEW_IS_OFFLINE_ONLY")
        resolved_kind = self.run_kind
        if resolved_kind is None:
            if self.slot in ("morning", "evening"):
                resolved_kind = "scheduled"
            elif self.slot == "test":
                resolved_kind = "test"
            else:
                raise GenerationError(
                    f"Invalid slot '{self.slot}'. Dynamic slots require an explicit run_kind."
                )
        if resolved_kind not in ALLOWED_RUN_KINDS:
            raise GenerationError(f"Invalid run_kind '{resolved_kind}'.")
        if not self.slot or any(part in self.slot for part in ("/", "\\", "..")):
            raise GenerationError("slot must be a safe non-empty identifier.")
        if resolved_kind == "manual_production" and self.slot != "manual-production":
            raise GenerationError("manual_production requires slot='manual-production'.")
        if resolved_kind == "test" and self.slot != "test":
            raise GenerationError("test run_kind requires slot='test'.")
        object.__setattr__(self, "run_kind", resolved_kind)
        if self.owner_ayah_key is not None:
            if (resolved_kind not in ("scheduled", "manual_production")
                or self.production_authorization is not None
                or self.requested_ayah_range is not None
                or self.test_verse_key is not None
                or not isinstance(self.owner_ayah_key, str)
                or type(self.owner_cycle_number) is not int
                or self.owner_cycle_number < 1):
                raise GenerationError("OWNER_V1_REQUEST_MODE_INVALID")
            try:
                if self.owner_ayah_key not in owner_ordered_keys():
                    raise GenerationError("OWNER_V1_AYAH_NOT_IN_FROZEN_CATALOG")
            except FreshCycleError as error:
                raise GenerationError("OWNER_V1_CATALOG_UNAVAILABLE") from error
            if self.owner_pending_successor is not None:
                surah, ayah = self.owner_ayah_key.split(':')
                if (self.owner_pending_successor != f'{surah}:{int(ayah) + 1}'
                        or self.owner_pending_successor not in owner_ordered_keys()):
                    raise GenerationError('OWNER_V1_INVALID_SUCCESSOR')
            if self.owner_reviewed_ayah_keys:
                from engine.owner_reviewed_passages import require_reviewed_owner_passage
                try:
                    require_reviewed_owner_passage(self.owner_reviewed_ayah_keys, self.owner_reviewed_registry)
                except ValueError as error:
                    raise GenerationError('OWNER_V1_REVIEW_IDENTITY_INVALID') from error
                if (self.owner_reviewed_ayah_keys[0] != self.owner_ayah_key
                    or not set(self.owner_reviewed_ayah_keys).issubset(owner_ordered_keys())):
                    raise GenerationError('OWNER_V1_REVIEW_CATALOG_MISMATCH')
                if self.owner_reviewed_approval is not None and (
                    not isinstance(self.owner_reviewed_approval, ReviewedPassageApproval)
                    or self.owner_reviewed_approval.ayah_keys != self.owner_reviewed_ayah_keys
                ):
                    raise GenerationError("OWNER_V1_REVIEW_APPROVAL_MISMATCH")
            elif self.owner_reviewed_approval is not None:
                raise GenerationError("OWNER_V1_REVIEW_APPROVAL_WITHOUT_PASSAGE")
        elif (self.owner_cycle_number is not None or self.owner_pending_successor is not None
              or self.owner_reviewed_ayah_keys or self.owner_reviewed_approval is not None):
            raise GenerationError("OWNER_V1_CYCLE_WITHOUT_TARGET")
        if self.background_clip_strategy not in ("auto_prefer_single", "single", "multiple"):
            raise GenerationError(
                f"Invalid background_clip_strategy '{self.background_clip_strategy}'. "
                f"Must be one of ('auto_prefer_single', 'single', 'multiple')."
            )
        if not (1 <= self.target_background_clips <= 3):
            raise GenerationError(
                f"target_background_clips must be between 1 and 3 (got {self.target_background_clips})."
            )
        if not (1 <= self.max_background_clips <= 3):
            raise GenerationError(
                f"max_background_clips must be between 1 and 3 (got {self.max_background_clips})."
            )
        if self.max_background_attempts < 1:
            raise GenerationError(
                f"max_background_attempts ({self.max_background_attempts}) must be >= 1."
            )
        if (
            isinstance(self.max_background_search_seconds, bool)
            or not isinstance(self.max_background_search_seconds, (int, float))
            or not math.isfinite(float(self.max_background_search_seconds))
            or float(self.max_background_search_seconds) <= 0.0
        ):
            raise GenerationError("max_background_search_seconds must be a positive finite number.")
        object.__setattr__(
            self,
            "max_background_search_seconds",
            float(self.max_background_search_seconds),
        )
        if (
            isinstance(self.max_candidate_processing_seconds, bool)
            or not isinstance(self.max_candidate_processing_seconds, (int, float))
            or not math.isfinite(float(self.max_candidate_processing_seconds))
            or float(self.max_candidate_processing_seconds) <= 0.0
        ):
            raise GenerationError("max_candidate_processing_seconds must be a positive finite number.")
        object.__setattr__(
            self,
            "max_candidate_processing_seconds",
            float(self.max_candidate_processing_seconds),
        )
        if (
            isinstance(self.max_background_source_file_size_bytes, bool)
            or not isinstance(self.max_background_source_file_size_bytes, int)
            or self.max_background_source_file_size_bytes <= 0
        ):
            raise GenerationError(
                "max_background_source_file_size_bytes must be a positive integer."
            )
        if (
            isinstance(self.max_render_attempts, bool)
            or not isinstance(self.max_render_attempts, int)
            or not (1 <= self.max_render_attempts <= 10)
        ):
            raise GenerationError(
                f"max_render_attempts must be an integer between 1 and 10 (got {self.max_render_attempts})."
            )
        if self.requested_ayah_range is not None:
            if self.run_kind != "test":
                raise GenerationError("requested_ayah_range is permitted only for local slot='test' runs.")
            if (
                len(self.requested_ayah_range) != 3
                or any(not isinstance(value, int) or isinstance(value, bool) for value in self.requested_ayah_range)
            ):
                raise GenerationError("requested_ayah_range must be a (surah, first_ayah, last_ayah) integer tuple.")
            surah, first_ayah, last_ayah = self.requested_ayah_range
            if not (1 <= surah <= 114 and 1 <= first_ayah <= last_ayah):
                raise GenerationError("requested_ayah_range contains an invalid Surah or ayah range.")
        if self.test_verse_key is not None:
            if self.run_kind != "test":
                raise GenerationError("test_verse_key is permitted only for test runs.")
            if self.requested_ayah_range is not None:
                raise GenerationError("test_verse_key and requested_ayah_range cannot be combined.")
            if self.test_verse_key not in TEST_VERIFIED_VERSE_KEYS:
                raise GenerationError(
                    "test_verse_key must name an explicitly approved verified test fixture."
                )
        if (
            isinstance(self.max_test_candidate_attempts, bool)
            or not isinstance(self.max_test_candidate_attempts, int)
            or not 1 <= self.max_test_candidate_attempts <= MAX_TEST_CANDIDATE_ATTEMPTS
        ):
            raise GenerationError(
                "max_test_candidate_attempts must be between 1 and "
                f"{MAX_TEST_CANDIDATE_ATTEMPTS}."
            )
        if not self.translation_languages:
            raise GenerationError("translation_languages cannot be empty.")
        if len(set(self.translation_languages)) != len(self.translation_languages):
            raise GenerationError("translation_languages cannot contain duplicates.")
        unsupported = [
            language for language in self.translation_languages
            if language not in SUPPORTED_TRANSLATION_SOURCES
        ]
        if unsupported:
            raise GenerationError(f"Unsupported translation languages: {unsupported}")
        canonical_languages = tuple(
            language for language in ("en", "de")
            if language in self.translation_languages
        )
        object.__setattr__(self, "translation_languages", canonical_languages)
        if self.test_verse_key is not None and canonical_languages != ("en",):
            raise GenerationError(
                "The verified 2:21 test fixture requires English-only translation display."
            )


def _build_subtitle_preflight_request(
    *,
    candidate: VerseCandidate,
    selected_surah: int,
    quran_data: QuranData,
    translations_by_language: Mapping[str, TranslationData],
    request: RunRequest,
) -> tuple[RenderRequest, list[RenderAyah]]:
    """Build the exact immutable text/timing request shared by preflight and render."""
    surah_obj = quran_data.get_surah(selected_surah)
    render_ayahs: list[RenderAyah] = []
    for rec in candidate.records:
        q_ayah = quran_data.get_ayah(rec.surah, rec.ayah)
        translated_ayahs = tuple(
            (
                language,
                translations_by_language[language].get_ayah(rec.surah, rec.ayah),
            )
            for language in request.translation_languages
        )
        primary_translation = translated_ayahs[0][1]
        render_ayahs.append(
            RenderAyah(
                surah_number=rec.surah,
                ayah_number=rec.ayah,
                arabic_text=q_ayah.arabic_text,
                english_text=primary_translation.translation_text,
                footnotes=primary_translation.footnotes,
                start_ms=int(round(rec.start_time * 1000)),
                end_ms=int(round(rec.end_time * 1000)),
                translations=tuple(
                    RenderTranslation(
                        language=language,
                        text=ayah.translation_text,
                        footnotes=ayah.footnotes,
                        translation_key=(
                            translations_by_language[language].source.translation_key
                        ),
                        translation_version=(
                            translations_by_language[language].source.version
                        ),
                    )
                    for language, ayah in translated_ayahs
                ),
            )
        )

    resolved_ayahs = [
        resolve_render_ayah_subtitle_plan(ayah, request.render_config)
        for ayah in render_ayahs
    ]
    return (
        RenderRequest(
            surah_number=selected_surah,
            arabic_surah_name=surah_obj.arabic_name,
            english_surah_name=surah_obj.english_name,
            ayahs=tuple(resolved_ayahs),
            surah_audio_path="",
            backgrounds=(),
            output_path="",
            seed=request.seed,
        ),
        resolved_ayahs,
    )


@dataclass(frozen=True)
class RunResult:
    """Structured result of a Reel generation run."""

    success: bool
    slot: str
    surah_number: Optional[int] = None
    first_ayah: Optional[int] = None
    last_ayah: Optional[int] = None
    duration_seconds: Optional[float] = None
    output_path: Optional[str] = None
    pexels_video_ids: tuple[int, ...] = ()
    final_safety_result: Optional[SafetyScanResult] = None
    stages_completed: tuple[str, ...] = ()
    failure_reason: Optional[str] = None
    provenance: dict = field(default_factory=dict)


def _prioritize_authorized_surahs(
    surahs: Sequence[int],
    catalog: ProductionAuthorizationCatalog,
    covered_ayahs: Sequence[str],
    recent_ranges: Sequence[tuple[int, int, int]],
    rng: random.Random,
    *,
    quran_data: QuranData,
    translations_by_language: Mapping[str, TranslationData],
    translation_languages: Sequence[str],
    limits: VerseSelectionLimits,
) -> list[int]:
    """Try surahs with fresh, exact authorized passages that fit text limits.

    Ordering is only a prefilter: real audio timing, provenance, and subtitle
    checks still decide whether the exact passage can be rendered.
    """
    allowed_surahs = set(surahs)
    covered = set(covered_ayahs)
    fresh_counts: dict[int, int] = {}
    for passage in catalog.passages:
        surah = passage.surah_number
        if surah not in allowed_surahs:
            continue
        if limits.maximum_ayahs is not None and len(passage.verse_keys) > limits.maximum_ayahs:
            continue
        if passage.verse_keys & covered:
            continue
        if any(
            recent_surah == surah
            and passage.first_ayah <= recent_last
            and passage.last_ayah >= recent_first
            for recent_surah, recent_first, recent_last in recent_ranges
        ):
            continue

        # All datasets are authenticated and loaded before this ordering step.
        # Missing exact ayah evidence is not treated as a valid candidate.
        try:
            arabic_chars = 0
            translated_chars = 0
            for ayah_number in range(passage.first_ayah, passage.last_ayah + 1):
                arabic_chars += len(quran_data.get_ayah(surah, ayah_number).arabic_text.strip())
                translated_chars += max(
                    len(translations_by_language[language].get_ayah(
                        surah, ayah_number
                    ).translation_text.strip())
                    for language in translation_languages
                )
        except Exception:
            continue
        if (arabic_chars > limits.maximum_arabic_characters
                or translated_chars > limits.maximum_english_characters):
            continue
        fresh_counts[surah] = fresh_counts.get(surah, 0) + 1

    prioritized = [surah for surah in surahs if fresh_counts.get(surah, 0)]
    rng.shuffle(prioritized)
    prioritized.sort(key=lambda surah: fresh_counts[surah], reverse=True)
    return prioritized


def generate_reel(
    request: RunRequest,
    *,
    fetch_quran_fn: Optional[Callable[[], QuranData]] = None,
    fetch_translation_fn: Optional[Callable[[], TranslationData]] = None,
    fetch_translation_for_language_fn: Optional[Callable[[str], TranslationData]] = None,
    fetch_timing_read_fn: Optional[Callable[[], TimingReadMetadata]] = None,
    fetch_timing_soar_fn: Optional[Callable[..., tuple[int, ...]]] = None,
    fetch_surah_timing_fn: Optional[Callable[..., SurahAudio]] = None,
    download_audio_fn: Optional[Callable[..., str]] = None,
    search_backgrounds_fn: Optional[Callable[..., tuple[BackgroundCandidate, ...]]] = None,
    download_background_fn: Optional[Callable[..., DownloadedBackground]] = None,
    scan_video_fn: Optional[Callable[..., SafetyScanResult]] = None,
    subtitle_preflight_fn: Optional[Callable[..., object]] = None,
    render_reel_fn: Optional[Callable[..., RenderResult]] = None,
    monotonic_fn: Optional[Callable[[], float]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    quran_data: Optional[QuranData] = None,
    translation_data: Optional[TranslationData] = None,
    translation_data_by_language: Optional[Mapping[str, TranslationData]] = None,
    background_pool_provider: Optional[BackgroundProvider] = None,
    mark_pool_stale_fn: Optional[
        Callable[[VerifiedBackgroundRecord, str, Optional[str]], None]
    ] = None,
    record_render_safety_failure_fn: Optional[
        Callable[..., None]
    ] = None,
) -> RunResult:
    """Execute the complete local Reel generation pipeline end-to-end."""
    monotonic = monotonic_fn or time.monotonic
    emit_log = log_fn or LOGGER.info
    pipeline_started_at = monotonic()
    stages: list[str] = [STAGE_STARTED]
    rng = random.Random(request.seed) if request.seed is not None else random.Random()
    run_temp_dir = tempfile.mkdtemp(prefix=f"reel_run_{request.slot}_")
    final_output_created = False
    selected_surah: Optional[int] = None
    first_ayah: Optional[int] = None
    last_ayah: Optional[int] = None
    final_output_path = request.output_path or os.path.join(
        tempfile.gettempdir(),
        f"quran_reel_{request.slot}_{os.getpid()}_{rng.randint(1000, 9999)}.mp4",
    )

    try:
        production = request.run_kind in ("scheduled", "manual_production")
        production_authorization = request.production_authorization
        if production and production_authorization is None and request.owner_ayah_key is None:
            raise GenerationError("PRODUCTION_AUTHORIZATION_REQUIRED")
        if production and request.requested_ayah_range is not None:
            verdict = evaluate_range(
                *request.requested_ayah_range,
                production_authorization=production_authorization,
            )
            if not verdict.eligible:
                raise GenerationError(verdict.reason_code)

        # 1. Fetch Arabic Quran Data (once)
        if quran_data is None:
            fetch_quran = fetch_quran_fn or fetch_quran_data
            try:
                quran_data = fetch_quran()
            except Exception as err:
                raise GenerationError(f"Failed to fetch Arabic Quran dataset: {err}") from err

        # Production runs strictly require the complete 114-Surah canonical production dataset
        try:
            validate_quran_source_for_run(request.run_kind, quran_data)
        except (QuranSourceError, ValueError) as err:
            raise GenerationError(str(err)) from err
        stages.append(STAGE_FETCHING_QURAN)

        # 2. Fetch every enabled exact Rowwad translation dataset once.
        translations_by_language = dict(translation_data_by_language or {})
        if translation_data is not None and "en" not in translations_by_language:
            translations_by_language["en"] = translation_data
        for language in request.translation_languages:
            if language in translations_by_language:
                continue
            try:
                if fetch_translation_for_language_fn is not None:
                    data = fetch_translation_for_language_fn(language)
                elif language == "en" and fetch_translation_fn is not None:
                    data = fetch_translation_fn()
                else:
                    data = fetch_translation_data_for_language(language)
            except Exception as err:
                raise GenerationError(
                    f"Failed to fetch Rowwad {language} translation dataset: {err}"
                ) from err
            translations_by_language[language] = data

        for language in request.translation_languages:
            data = translations_by_language[language]
            expected_key = SUPPORTED_TRANSLATION_SOURCES[language]
            if (
                data.source.language_iso_code != language
                or data.source.translation_key != expected_key
            ):
                raise GenerationError(
                    f"Translation identity mismatch for '{language}': expected "
                    f"{expected_key}/{language}, got "
                    f"{data.source.translation_key}/{data.source.language_iso_code}."
                )
            expected_version = EXPECTED_TRANSLATION_VERSIONS.get(expected_key)
            if expected_version is not None and data.source.version != expected_version:
                raise GenerationError(
                    f"Translation version mismatch for '{language}': expected "
                    f"'{expected_version}', got '{data.source.version}'."
                )
        stages.append(STAGE_FETCHING_TRANSLATION)

        # 3. Bind timing and MP3 download to one provider-declared read identity.
        if fetch_timing_read_fn is not None:
            fetch_timing_read = fetch_timing_read_fn
        elif fetch_timing_soar_fn is not None or fetch_surah_timing_fn is not None:
            # Dependency-injected timing tests remain network-free, but their
            # SurahAudio URL is still checked against this exact identity.
            fetch_timing_read = lambda: TimingReadMetadata(
                read_id=DEFAULT_TIMING_READ_ID,
                reciter_name=DEFAULT_RECITER_NAME,
                riwaya=DEFAULT_RIWAYA_NAME,
                audio_server=DEFAULT_AUDIO_SERVER,
                surah_count=EXPECTED_SURAH_COUNT,
            )
        else:
            fetch_timing_read = fetch_timing_read_metadata
        try:
            timing_read = fetch_timing_read()
        except Exception as err:
            raise GenerationError(f"Failed to verify timing/audio read identity: {err}") from err
        if timing_read.read_id != DEFAULT_TIMING_READ_ID:
            raise GenerationError(
                f"Timing read identity mismatch: got {timing_read.read_id}, expected {DEFAULT_TIMING_READ_ID}."
            )

        fetch_soar = fetch_timing_soar_fn or fetch_available_timing_soar
        try:
            available_soar = (
                fetch_soar()
                if fetch_timing_soar_fn is not None
                else fetch_soar(timing_read_id=timing_read.read_id)
            )
        except Exception as err:
            raise GenerationError(f"Failed to fetch available timing Surahs: {err}") from err
        if not available_soar:
            raise GenerationError("No Surahs with timing coverage returned from audio provider.")
        stages.append(STAGE_FETCHING_AUDIO_METADATA)

        # 4. Verse Selection with Coverage Awareness and Full Footnote Eligibility (Decision D-011)
        subtitle_stage_started_at = monotonic()
        fetch_timing = fetch_surah_timing_fn or fetch_surah_timing
        available_quran_surahs = {s.number for s in quran_data.surahs}
        candidate_surahs = [s for s in available_soar if s in available_quran_surahs]
        if production:
            approved_keys = (
                frozenset(request.owner_reviewed_ayah_keys or (request.owner_ayah_key, request.owner_pending_successor)) - {None} if request.owner_ayah_key is not None
                else production_authorization.authorized_verse_keys
                if production_authorization is not None
                else eligible_verse_keys()
            )
            approved_surahs = {int(key.split(":")[0]) for key in approved_keys}
            candidate_surahs = [number for number in candidate_surahs if number in approved_surahs]
        auto_test_selection = (
            request.run_kind == "test" and request.requested_ayah_range is None
        )
        if request.requested_ayah_range is not None:
            requested_surah, _, _ = request.requested_ayah_range
            candidate_surahs = [requested_surah] if requested_surah in candidate_surahs else []
        elif request.owner_ayah_key is not None:
            requested_surah = int(request.owner_ayah_key.split(":", 1)[0])
            candidate_surahs = [requested_surah] if requested_surah in candidate_surahs else []
        elif request.test_verse_key is not None:
            requested_surah = int(request.test_verse_key.split(":", 1)[0])
            candidate_surahs = [requested_surah] if requested_surah in candidate_surahs else []
        elif auto_test_selection and request.translation_languages == ("en",):
            # Put Surahs with approved test fixtures first, then keep
            # randomized diversity for fallback candidates.
            verified_surah_numbers = {
                int(verse_key.split(":", 1)[0])
                for verse_key in TEST_VERIFIED_VERSE_KEYS
            }
            verified_surahs = [
                surah for surah in candidate_surahs
                if surah in verified_surah_numbers
            ]
            remaining_surahs = [
                surah for surah in candidate_surahs
                if surah not in verified_surah_numbers
            ]
            rng.shuffle(remaining_surahs)
            candidate_surahs = verified_surahs + remaining_surahs
        else:
            if production and production_authorization is not None:
                candidate_surahs = _prioritize_authorized_surahs(
                    candidate_surahs,
                    production_authorization,
                    request.covered_ayahs,
                    request.recent_ayah_ranges,
                    rng,
                    quran_data=quran_data,
                    translations_by_language=translations_by_language,
                    translation_languages=request.translation_languages,
                    limits=request.verse_limits,
                )
            else:
                rng.shuffle(candidate_surahs)

        selected_candidate: Optional[VerseCandidate] = None
        selected_surah = None
        render_ayahs: list[RenderAyah] = []
        subtitle_preflight: object | None = None
        preflight_fn = subtitle_preflight_fn or preflight_subtitle_layout
        test_candidates_attempted = 0
        test_verified_attempts = 0
        test_fallback_attempts = 0
        test_failure_codes: list[str] = []
        pending_test_candidates: list[
            tuple[bool, VerseCandidate, RenderRequest, list[RenderAyah]]
        ] = []
        surahs_tried = 0
        selection_failure_counts = {
            "timing_unavailable": 0,
            "no_authorized_timings": 0,
            "no_selectable_range": 0,
            "subtitle_preflight_rejected": 0,
            "unexpected_selection_error": 0,
        }

        for surah_num in candidate_surahs:
            if surahs_tried >= request.max_surah_attempts:
                break
            surahs_tried += 1

            try:
                surah_audio = (
                    fetch_timing(surah_num)
                    if fetch_surah_timing_fn is not None
                    else fetch_timing(
                        surah_num,
                        timing_read_id=timing_read.read_id,
                        audio_server=timing_read.audio_server,
                    )
                )
            except Exception:
                selection_failure_counts["timing_unavailable"] += 1
                continue

            expected_audio_url = build_surah_audio_url(
                surah_num,
                audio_server=timing_read.audio_server,
                available_surahs=available_soar,
            )
            if surah_audio.surah_number != surah_num or surah_audio.audio_url != expected_audio_url:
                raise GenerationError(
                    f"Timing/audio identity mismatch for Surah {surah_num}: timing resource advertises "
                    f"'{surah_audio.audio_url}', expected '{expected_audio_url}' from read {timing_read.read_id}."
                )

            # Production timing candidates are restricted to the approved corpus.
            eligible_timings = []
            for timing in surah_audio.timings:
                approved_keys = (
                    frozenset(request.owner_reviewed_ayah_keys or (request.owner_ayah_key, request.owner_pending_successor)) - {None} if request.owner_ayah_key is not None
                    else production_authorization.authorized_verse_keys
                    if production_authorization is not None
                    else eligible_verse_keys()
                )
                if production and f"{surah_num}:{timing.ayah_number}" not in approved_keys:
                    continue
                try:
                    translated_ayahs = {
                        language: translations_by_language[language].get_ayah(
                            surah_num, timing.ayah_number
                        )
                        for language in request.translation_languages
                    }
                except Exception:
                    continue

                try:
                    q_ayah = quran_data.get_ayah(surah_num, timing.ayah_number)
                except Exception:
                    continue

                verse_timing = to_engine_verse_timing(
                    timing,
                    arabic_text=q_ayah.arabic_text,
                    # The legacy selector exposes one translation-length field.
                    # Feed it the longest enabled source text so no language is
                    # hidden from the pre-render visual bound.
                    english_text=max(
                        (ayah.translation_text for ayah in translated_ayahs.values()),
                        key=len,
                    ),
                )
                eligible_timings.append(verse_timing)

            if not eligible_timings:
                selection_failure_counts["no_authorized_timings"] += 1
                continue

            try:
                if auto_test_selection:
                    single_ayah_candidates: list[VerseCandidate] = []
                    for record in eligible_timings:
                        verse_key = f"{record.surah}:{record.ayah}"
                        if request.test_verse_key is not None and verse_key != request.test_verse_key:
                            continue
                        duration = record.end_time - record.start_time
                        is_verified_fixture = (
                            request.translation_languages == ("en",)
                            and verse_key in TEST_VERIFIED_VERSE_KEYS
                        )
                        minimum_duration = (
                            MINIMUM_ALLOWED_DURATION_SECONDS
                            if is_verified_fixture
                            else request.duration_config.minimum_duration
                        )
                        maximum_duration = (
                            MAXIMUM_ALLOWED_DURATION_SECONDS
                            if is_verified_fixture
                            else request.duration_config.maximum_duration
                        )
                        if not minimum_duration <= duration <= maximum_duration:
                            continue
                        single_ayah_candidates.append(
                            VerseCandidate(
                                records=(record,),
                                duration_seconds=duration,
                                target_duration_seconds=duration,
                            )
                        )

                    rng.shuffle(single_ayah_candidates)
                    for candidate in single_ayah_candidates:
                        preflight_request, candidate_render_ayahs = _build_subtitle_preflight_request(
                            candidate=candidate,
                            selected_surah=surah_num,
                            quran_data=quran_data,
                            translations_by_language=translations_by_language,
                            request=request,
                        )
                        verified = all(
                            ayah.subtitle_mode == SUBTITLE_MODE_VERIFIED_PHRASE_PROGRESSIVE
                            for ayah in candidate_render_ayahs
                        )
                        pending_test_candidates.append(
                            (
                                verified,
                                candidate,
                                preflight_request,
                                candidate_render_ayahs,
                            )
                        )
                    continue
                elif request.owner_ayah_key is not None:
                    cand = select_owner_v1_ayah_candidate(
                        eligible_timings,
                        request.owner_ayah_key,
                        pending_successor=request.owner_pending_successor,
                        reviewed_ayah_keys=request.owner_reviewed_ayah_keys,
                        reviewed_registry=request.owner_reviewed_registry,
                        duration_config=request.duration_config,
                        limits=request.verse_limits,
                    )
                elif request.requested_ayah_range is not None:
                    _, requested_first, requested_last = request.requested_ayah_range
                    records_by_ayah = {record.ayah: record for record in eligible_timings}
                    requested_records = tuple(
                        records_by_ayah[ayah_number]
                        for ayah_number in range(requested_first, requested_last + 1)
                    )
                    duration = requested_records[-1].end_time - requested_records[0].start_time
                    if not (
                        request.duration_config.minimum_duration
                        <= duration
                        <= request.duration_config.maximum_duration
                    ):
                        raise GenerationError(
                            f"Requested ayah range duration {duration:.3f}s is outside the configured "
                            f"{request.duration_config.minimum_duration:.3f}.."
                            f"{request.duration_config.maximum_duration:.3f}s range."
                        )
                    if sum(len(record.arabic_text.strip()) for record in requested_records) > request.verse_limits.maximum_arabic_characters:
                        raise GenerationError("Requested ayah range exceeds the Arabic text limit.")
                    if sum(len(record.english_text.strip()) for record in requested_records) > request.verse_limits.maximum_english_characters:
                        raise GenerationError("Requested ayah range exceeds the English text limit.")
                    if (
                        request.verse_limits.maximum_ayahs is not None
                        and len(requested_records) > request.verse_limits.maximum_ayahs
                    ):
                        raise GenerationError("Requested ayah range exceeds the ayah-count limit.")
                    cand = VerseCandidate(
                        records=requested_records,
                        duration_seconds=duration,
                        target_duration_seconds=duration,
                    )
                else:
                    surah_excluded_ranges = set(request.recent_ayah_ranges)
                    cand = None
                    max_preflight_attempts = 250
                    for _ in range(max_preflight_attempts):
                        try:
                            candidate_attempt = select_verse_candidate(
                                eligible_timings,
                                duration_config=request.duration_config,
                                recent_ranges=surah_excluded_ranges,
                                limits=request.verse_limits,
                                rng=rng,
                                covered_ayahs=request.covered_ayahs,
                                production_authorization=production_authorization,
                            )
                        except VerseSelectionError:
                            selection_failure_counts["no_selectable_range"] += 1
                            break

                        if production:
                            require_candidate_eligible(
                                candidate_attempt,
                                production_authorization=production_authorization,
                            )
                        candidate_preflight_req, candidate_render_ayahs = (
                            _build_subtitle_preflight_request(
                                candidate=candidate_attempt,
                                selected_surah=surah_num,
                                quran_data=quran_data,
                                translations_by_language=translations_by_language,
                                request=request,
                            )
                        )
                        try:
                            cand_preflight = preflight_fn(
                                candidate_preflight_req,
                                config=request.render_config,
                            )
                            cand = candidate_attempt
                            render_ayahs = candidate_render_ayahs
                            subtitle_preflight = cand_preflight
                            break
                        except SubtitlePreflightError:
                            selection_failure_counts["subtitle_preflight_rejected"] += 1
                            surah_excluded_ranges.add(
                                (
                                    surah_num,
                                    candidate_attempt.first_ayah,
                                    candidate_attempt.last_ayah,
                                )
                            )
                            continue

                if cand is not None:
                    selected_candidate = cand
                    selected_surah = surah_num
                    break
            except GenerationError:
                raise
            except VerseSelectionError as error:
                if request.owner_ayah_key is not None:
                    raise GenerationError(str(error)) from error
                selection_failure_counts["no_selectable_range"] += 1
                continue
            except Exception:
                if request.owner_ayah_key is not None:
                    raise GenerationError("OWNER_V1_TECHNICAL_PREFLIGHT_FAILED")
                selection_failure_counts["unexpected_selection_error"] += 1
                continue

        if auto_test_selection:
            # Search globally across the bounded Surah pool so a verified
            # fixture always precedes every fallback candidate, independent
            # of randomized fallback ordering.
            pending_test_candidates.sort(key=lambda item: not item[0])
            for verified, candidate, preflight_request, candidate_render_ayahs in pending_test_candidates:
                if test_candidates_attempted >= request.max_test_candidate_attempts:
                    break
                test_candidates_attempted += 1
                if verified:
                    test_verified_attempts += 1
                else:
                    test_fallback_attempts += 1
                try:
                    candidate_preflight = preflight_fn(
                        preflight_request,
                        config=request.render_config,
                    )
                except SubtitlePreflightError as err:
                    test_failure_codes.append(err.code)
                    continue
                except Exception as err:
                    verse_key = (
                        f"{candidate.records[0].surah}:{candidate.records[0].ayah}"
                    )
                    raise GenerationError(
                        f"SUBTITLE_PREFLIGHT_FAILED; verse_key={verse_key}; reason={err}"
                    ) from err

                selected_candidate = candidate
                selected_surah = candidate.records[0].surah
                render_ayahs = candidate_render_ayahs
                subtitle_preflight = candidate_preflight
                break

        if selected_candidate is None or selected_surah is None:
            if auto_test_selection:
                failure_counts = {
                    code: test_failure_codes.count(code)
                    for code in sorted(set(test_failure_codes))
                }
                raise GenerationError(
                    "NO_RENDERABLE_TEST_AYAH; "
                    f"attempted={test_candidates_attempted}; "
                    f"bound={request.max_test_candidate_attempts}; "
                    f"verified_attempts={test_verified_attempts}; "
                    f"fallback_attempts={test_fallback_attempts}; "
                    f"test_verse_key={request.test_verse_key or 'auto'}; "
                    f"layout_failures={failure_counts}"
                )
            raise GenerationError(
                "NO_ELIGIBLE_VERSE_RANGE: no range satisfies production constraints; "
                f"surahs_tried={surahs_tried}; candidate_surahs={len(candidate_surahs)}; "
                f"failure_counts={selection_failure_counts}"
            )
        source_lead_ms = 0
        if production and request.owner_ayah_key is not None:
            keys = tuple(f'{row.surah}:{row.ayah}' for row in selected_candidate.records)
            expected_ranges = ((request.owner_reviewed_ayah_keys,) if request.owner_reviewed_ayah_keys
                               else ((request.owner_ayah_key,), (request.owner_ayah_key, request.owner_pending_successor)))
            if keys not in expected_ranges:
                raise GenerationError("OWNER_V1_EXACT_SELECTION_MISMATCH")
            # One audited source boundary, not a general minimum-duration waiver.
            # The ayah annotations stay at 300..3240 ms; audio uses the matching
            # 100..3240 ms excerpt only after verifying the downloaded recording.
            if selected_candidate.duration_seconds < 3.0:
                first = selected_candidate.records[0]
                if (keys != ('1:1',) or timing_read.read_id != 92
                        or timing_read.audio_server not in (
                            DEFAULT_AUDIO_SERVER, 'https://cdn.mp3quran.net/audio/yasser-dosari/r1/',
                        )
                        or first.start_time != 0.300 or first.end_time != 3.240):
                    raise GenerationError('OWNER_V1_BELOW_BUFFER_REEL_MINIMUM')
                source_lead_ms = 200
        elif production:
            require_candidate_eligible(
                selected_candidate,
                production_authorization=production_authorization,
            )
        stages.append(STAGE_SELECTING_VERSES)

        first_ayah = selected_candidate.first_ayah
        last_ayah = selected_candidate.last_ayah

        # 5. Build the exact immutable render text/timing request and validate
        # every subtitle panel with the final libass geometry before any media
        # download, Pexels processing, or visual safety scan.
        surah_obj = quran_data.get_surah(selected_surah)
        if subtitle_preflight is None:
            preflight_request, render_ayahs = _build_subtitle_preflight_request(
                candidate=selected_candidate,
                selected_surah=selected_surah,
                quran_data=quran_data,
                translations_by_language=translations_by_language,
                request=request,
            )
            if source_lead_ms:
                from dataclasses import replace
                preflight_request = replace(preflight_request, source_lead_ms=source_lead_ms)
            try:
                subtitle_preflight = preflight_fn(
                    preflight_request,
                    config=request.render_config,
                )
            except SubtitlePreflightError as err:
                raise GenerationError(
                    f"SUBTITLE_PREFLIGHT_FAILED; verse_key={selected_surah}:"
                    f"{first_ayah}-{last_ayah}; reason={err}"
                ) from err
            except Exception as err:
                raise GenerationError(
                    f"SUBTITLE_PREFLIGHT_FAILED; verse_key={selected_surah}:"
                    f"{first_ayah}-{last_ayah}; reason={err}"
                ) from err
        stages.append(STAGE_PREFLIGHTING_SUBTITLES)
        subtitle_modes = ",".join(
            f"{ayah.surah_number}:{ayah.ayah_number}={ayah.subtitle_mode or 'UNRESOLVED'}"
            for ayah in render_ayahs
        )
        emit_log(
            "[STAGE] subtitle_preflight completed "
            f"seconds={monotonic() - subtitle_stage_started_at:.3f} "
            f"verse={selected_surah}:{first_ayah}-{last_ayah} modes={subtitle_modes}"
        )

        # 6. Audio File Download
        audio_started_at = monotonic()
        download_audio = download_audio_fn or download_surah_audio
        try:
            audio_path = (
                download_audio(selected_surah, target_dir=run_temp_dir)
                if download_audio_fn is not None
                else download_audio(
                    selected_surah,
                    target_dir=run_temp_dir,
                    audio_server=timing_read.audio_server,
                )
            )
        except Exception as err:
            raise GenerationError(f"Failed to download Surah audio for Surah {selected_surah}: {err}") from err
        if source_lead_ms:
            _verify_opening_recording(audio_path)
        stages.append(STAGE_DOWNLOADING_AUDIO)
        emit_log(
            f"[STAGE] audio completed seconds={monotonic() - audio_started_at:.3f} "
            f"surah={selected_surah}"
        )

        # 7. Background Selection and Source Safety Scanning
        search_bgs = search_backgrounds_fn or search_background_candidates
        download_bg = download_background_fn or download_background
        scan_vid = scan_video_fn or scan_video

        def _invoke_search_bgs(q: str, p: int) -> tuple[BackgroundCandidate, ...]:
            try:
                return search_bgs(q, page=p)
            except TypeError:
                return search_bgs(q)

        target_reel_duration = selected_candidate.duration_seconds + source_lead_ms / 1000.0
        strategy = request.background_clip_strategy
        # Phase 8G.5: Maximum background clips capped at 3, preferring 1
        max_clips = max(1, min(3, request.max_background_clips if strategy == "multiple" else request.target_background_clips))
        # A verified single-clip pool may safely furnish alternative clips for
        # bounded final-safety retries.  Prefetching is deliberately limited to
        # the retry bound and only applies to prior-source-scanned records.
        pool_prefetch_count = (
            max_clips * request.max_render_attempts
            if strategy in ("auto_prefer_single", "single")
            else max_clips
        )

        safe_backgrounds: list[RenderBackground] = []
        safe_candidates: list[BackgroundCandidate] = []
        safe_pool_records: list[VerifiedBackgroundRecord] = []
        background_source_mode = "LIVE_SEARCH_FALLBACK"
        recent_ids = set(request.recent_pexels_video_ids) | set(request.historical_pexels_video_ids)
        scanned_ids: set[int] = set()
        safety_scanned_ids: set[int] = set()
        attempts = 0
        rejection_reasons: dict[str, int] = {}
        queries_attempted: set[str] = set()
        pages_attempted: set[int] = set()
        person_rejections = 0
        face_rejections = 0
        both_rejections = 0
        other_rejections = 0
        candidate_timeout_rejections = 0
        prefilter_rejections: dict[str, int] = {}
        background_started_at = monotonic()

        def _background_elapsed() -> float:
            return monotonic() - background_started_at

        def _background_diagnostics(code: str, message: str = "") -> GenerationError:
            rejection_summary = "; ".join(
                f"{reason}: {count}" for reason, count in sorted(rejection_reasons.items())
            )
            return GenerationError(
                f"{code}; "
                f"unique_candidates_attempted={len(scanned_ids)}; "
                f"unique_candidates_scanned={len(safety_scanned_ids)}; "
                f"elapsed_seconds={_background_elapsed():.3f}; "
                f"time_budget_seconds={request.max_background_search_seconds:.3f}; "
                f"candidate_limit={request.max_background_attempts}; "
                f"queries_attempted={len(queries_attempted)}; "
                f"pages_attempted={len(pages_attempted)}; "
                f"person_rejections={person_rejections}; "
                f"face_rejections={face_rejections}; "
                f"both_rejections={both_rejections}; "
                f"other_rejections={other_rejections}; "
                f"candidate_timeout_rejections={candidate_timeout_rejections}; "
                f"prefilter_rejections={prefilter_rejections}; "
                f"rejection_summary=[{rejection_summary}]"
                f"{'; ' + message if message else ''}"
            )

        def _ensure_background_time_budget() -> None:
            if _background_elapsed() >= request.max_background_search_seconds:
                raise _background_diagnostics("SAFE_BACKGROUND_TIME_BUDGET_EXHAUSTED")

        emit_log(
            "[STAGE] background_acquisition started "
            f"candidate_limit={request.max_background_attempts} "
            f"time_budget_seconds={request.max_background_search_seconds:.3f} "
            f"candidate_budget_seconds={request.max_candidate_processing_seconds:.3f} "
            f"max_source_bytes={request.max_background_source_file_size_bytes}"
        )

        # Prefer source clips already verified under the exact current policy.
        # Provider IDs locate the current variant; the stored SHA-256 binds the
        # prior source scan to the bytes downloaded for this run.
        render_config_fp = compute_render_config_fingerprint(request.render_config)
        pool_records = select_verified_pool_records(
            request.verified_background_records,
            request.background_usage_events,
            required_duration_seconds=target_reel_duration * 0.5,
            recent_asset_ids=tuple(recent_ids),
            render_config_fingerprint=render_config_fp,
            seed=request.seed,
        )
        unavailable_pool_records: set[str] = set()

        def _acquire_pool_record(record: VerifiedBackgroundRecord) -> bool:
            nonlocal background_source_mode
            # Alternate encodes of an already acquired video are not distinct
            # backgrounds. Keep variants available only until one succeeds.
            if any(
                (existing.provider, existing.provider_asset_id)
                == (record.provider, record.provider_asset_id)
                for existing in safe_pool_records
            ):
                return False
            unavailable_pool_records.add(record.record_id)
            _ensure_background_time_budget()
            try:
                provider = background_pool_provider or make_provider(record.provider)
            except Exception as err:
                emit_log(
                    f"[BACKGROUND] source=VERIFIED_POOL provider={record.provider} "
                    f"verdict=SKIP error_type={type(err).__name__}"
                )
                return False
            if record.provider != provider.name:
                return False
            downloaded: Optional[DownloadedBackground] = None
            try:
                asset = provider.get_asset(record.provider_asset_id)
                candidate = provider.resolve_variant(
                    asset,
                    getattr(record, "provider_variant_id", getattr(record, "provider_file_id", "")),
                )
                if (
                    candidate.duration_seconds < target_reel_duration * 0.5
                    or candidate.file_width < 1080
                    or candidate.file_height < 1920
                ):
                    if mark_pool_stale_fn is not None:
                        mark_pool_stale_fn(record, "variant_unsuitable", None)
                    return False
                if download_background_fn is None:
                    downloaded = download_bg(
                        candidate,
                        target_dir=run_temp_dir,
                        max_file_size_bytes=request.max_background_source_file_size_bytes,
                        abort_check=lambda: _background_elapsed()
                        >= request.max_background_search_seconds,
                    )
                else:
                    downloaded = download_bg(candidate, target_dir=run_temp_dir)
                hash_match = downloaded.content_sha256 == record.content_sha256
                emit_log(
                    f"[BACKGROUND] asset={record.provider}:{record.provider_asset_id} "
                    f"file_id={getattr(record, 'provider_variant_id', getattr(record, 'provider_file_id', ''))} hash_match={str(hash_match).lower()}"
                )
                if not hash_match:
                    if mark_pool_stale_fn is not None:
                        mark_pool_stale_fn(
                            record,
                            "source_changed",
                            downloaded.content_sha256 or None,
                        )
                    try:
                        os.remove(downloaded.local_path)
                    except FileNotFoundError:
                        pass
                    return False

                # Coherence applies within a multi-clip Reel. Single-clip modes
                # prefetch independent retry alternatives, not one montage.
                if safe_pool_records and strategy == "multiple":
                    existing_attrs = [
                        extract_visual_attributes(r.query, r.render_robustness)
                        for r in safe_pool_records
                    ]
                    cand_attr = extract_visual_attributes(record.query, record.render_robustness)
                    coherent, reason = are_all_clips_coherent(existing_attrs + [cand_attr])
                    if not coherent:
                        emit_log(
                            f"[BACKGROUND] source=VERIFIED_POOL asset={record.provider}:{record.provider_asset_id} "
                            f"verdict=SKIP reason=INCOHERENT detail={reason}"
                        )
                        if downloaded is not None:
                            try:
                                os.remove(downloaded.local_path)
                            except FileNotFoundError:
                                pass
                        return False

                safe_candidates.append(candidate)
                safe_backgrounds.append(RenderBackground(
                    local_path=downloaded.local_path,
                    source_duration_seconds=candidate.duration_seconds,
                    pexels_video_id=candidate.pexels_video_id,
                    requires_center_crop=candidate.requires_center_crop,
                ))
                safe_pool_records.append(record)
                unavailable_pool_records.discard(record.record_id)
                background_source_mode = "VERIFIED_POOL"
                return True
            except Exception as err:
                message = str(err)
                if mark_pool_stale_fn is not None:
                    if "_ASSET_MISSING" in message:
                        mark_pool_stale_fn(record, "missing", None)
                    elif "_VARIANT_MISSING" in message or "_VARIANT_UNSUITABLE" in message:
                        mark_pool_stale_fn(record, "missing_variant", None)
                emit_log(
                    f"[BACKGROUND] source=VERIFIED_POOL asset={record.provider}:"
                    f"{record.provider_asset_id} verdict=SKIP error_type={type(err).__name__}"
                )
                if downloaded is not None:
                    try:
                        os.remove(downloaded.local_path)
                    except FileNotFoundError:
                        pass
                return False

        if pool_records:
            emit_log(
                f"[BACKGROUND] source=VERIFIED_POOL eligible_records={len(pool_records)}"
            )
            for record in pool_records:
                if _acquire_pool_record(record):
                    if len(safe_pool_records) >= pool_prefetch_count:
                        break

        if not safe_backgrounds:
            if request.owner_ayah_key is not None:
                # Owner v1 cannot silently substitute newly discovered footage
                # for a source-hash-matched, policy-verified background. The
                # final rendered-Reel safety scan remains mandatory as well.
                raise GenerationError("OWNER_V1_VERIFIED_BACKGROUND_REQUIRED")
            emit_log("[BACKGROUND] source=LIVE_SEARCH_FALLBACK")

        def _record_rejection(scan_result: SafetyScanResult) -> str:
            nonlocal person_rejections, face_rejections, both_rejections, other_rejections
            person_count = len(scan_result.person_detections)
            face_count = len(scan_result.face_detections)
            if person_count > 0 and face_count > 0:
                both_rejections += 1
            elif person_count > 0:
                person_rejections += 1
            elif face_count > 0:
                face_rejections += 1
            else:
                other_rejections += 1
            reason = scan_result.failure_reason or "unknown_rejection"
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            return reason

        def _max_detection_confidence(detections: Sequence[object]) -> float:
            values: list[float] = []
            for detection in detections:
                try:
                    values.append(float(getattr(detection, "confidence")))
                except (TypeError, ValueError):
                    continue
            return max(values, default=0.0)

        def _process_background_candidate(
            candidate: BackgroundCandidate,
        ) -> tuple[Optional[RenderBackground], Optional[BackgroundCandidate]]:
            nonlocal attempts, other_rejections, candidate_timeout_rejections
            _ensure_background_time_budget()
            candidate_started_at = monotonic()
            scanned_ids.add(candidate.pexels_video_id)
            attempts += 1
            candidate_number = attempts
            parsed_source_url = urlsplit(candidate.file_url)
            emit_log(
                f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                f"id={candidate.pexels_video_id} file_id={candidate.video_file_id} "
                f"query={candidate.query!r} page={candidate.search_page} "
                f"resolution={candidate.file_width}x{candidate.file_height} "
                f"duration_seconds={candidate.duration_seconds:.3f} "
                f"declared_bytes={candidate.declared_file_size_bytes or 'unknown'} "
                f"source_host={parsed_source_url.hostname or 'unknown'} "
                f"source_path={parsed_source_url.path or '/'}"
            )

            download_started_at = monotonic()
            try:
                if download_background_fn is None:
                    downloaded = download_bg(
                        candidate,
                        target_dir=run_temp_dir,
                        max_file_size_bytes=request.max_background_source_file_size_bytes,
                    )
                else:
                    downloaded = download_bg(candidate, target_dir=run_temp_dir)
            except Exception as err:
                other_rejections += 1
                rejection_reasons["download_failed"] = rejection_reasons.get("download_failed", 0) + 1
                emit_log(
                    f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                    f"id={candidate.pexels_video_id} download_seconds="
                    f"{monotonic() - download_started_at:.3f} verdict=DOWNLOAD_FAILED "
                    f"total_candidate_seconds={monotonic() - candidate_started_at:.3f} "
                    f"error_type={type(err).__name__}"
                )
                _ensure_background_time_budget()
                return None, None

            download_seconds = monotonic() - download_started_at
            emit_log(
                f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                f"id={candidate.pexels_video_id} download_seconds={download_seconds:.3f} "
                f"downloaded_bytes={downloaded.file_size_bytes}"
            )
            if monotonic() - candidate_started_at >= request.max_candidate_processing_seconds:
                if os.path.exists(downloaded.local_path):
                    os.remove(downloaded.local_path)
                candidate_timeout_rejections += 1
                rejection_reasons["CANDIDATE_PROCESSING_TIMEOUT"] = (
                    rejection_reasons.get("CANDIDATE_PROCESSING_TIMEOUT", 0) + 1
                )
                emit_log(
                    f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                    f"id={candidate.pexels_video_id} verdict=CANDIDATE_PROCESSING_TIMEOUT "
                    f"total_candidate_seconds={monotonic() - candidate_started_at:.3f}"
                )
                _ensure_background_time_budget()
                return None, None
            if _background_elapsed() >= request.max_background_search_seconds:
                if os.path.exists(downloaded.local_path):
                    os.remove(downloaded.local_path)
                raise _background_diagnostics("SAFE_BACKGROUND_TIME_BUDGET_EXHAUSTED")

            scan_started_at = monotonic()
            safety_scanned_ids.add(candidate.pexels_video_id)
            try:
                if scan_video_fn is None:
                    scan_result = scan_vid(
                        downloaded.local_path,
                        config=SafetyConfig(animal_detection_required=True),
                        abort_check=lambda: (
                            _background_elapsed() >= request.max_background_search_seconds
                            or monotonic() - candidate_started_at
                            >= request.max_candidate_processing_seconds
                        ),
                    )
                else:
                    scan_result = scan_vid(downloaded.local_path)
            except Exception as err:
                if os.path.exists(downloaded.local_path):
                    os.remove(downloaded.local_path)
                other_rejections += 1
                rejection_reasons["scan_error"] = rejection_reasons.get("scan_error", 0) + 1
                emit_log(
                    f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                    f"id={candidate.pexels_video_id} scan_seconds="
                    f"{monotonic() - scan_started_at:.3f} verdict=SCAN_ERROR "
                    f"error_type={type(err).__name__}"
                )
                _ensure_background_time_budget()
                return None, None

            scan_seconds = monotonic() - scan_started_at
            total_candidate_seconds = monotonic() - candidate_started_at
            max_person_confidence = _max_detection_confidence(
                scan_result.person_detections
            )
            max_face_confidence = _max_detection_confidence(
                scan_result.face_detections
            )
            if _background_elapsed() >= request.max_background_search_seconds:
                if not scan_result.safe:
                    _record_rejection(scan_result)
                if os.path.exists(downloaded.local_path):
                    os.remove(downloaded.local_path)
                raise _background_diagnostics("SAFE_BACKGROUND_TIME_BUDGET_EXHAUSTED")
            if total_candidate_seconds >= request.max_candidate_processing_seconds:
                if os.path.exists(downloaded.local_path):
                    os.remove(downloaded.local_path)
                candidate_timeout_rejections += 1
                rejection_reasons["CANDIDATE_PROCESSING_TIMEOUT"] = (
                    rejection_reasons.get("CANDIDATE_PROCESSING_TIMEOUT", 0) + 1
                )
                emit_log(
                    f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                    f"id={candidate.pexels_video_id} scan_seconds={scan_seconds:.3f} "
                    f"total_candidate_seconds={total_candidate_seconds:.3f} "
                    f"frames_scanned={scan_result.frames_scanned} "
                    f"decode_seconds={scan_result.decode_seconds:.3f} "
                    f"person_seconds={scan_result.person_inference_seconds:.3f} "
                    f"face_seconds={scan_result.face_inference_seconds:.3f} "
                    f"other_scan_seconds={scan_result.other_processing_seconds:.3f} "
                    f"max_person_confidence={max_person_confidence:.6f} "
                    f"max_face_confidence={max_face_confidence:.6f} "
                    "verdict=CANDIDATE_PROCESSING_TIMEOUT"
                )
                return None, None
            if scan_result.safe:
                emit_log(
                    f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                    f"id={candidate.pexels_video_id} scan_seconds={scan_seconds:.3f} "
                    f"total_candidate_seconds={total_candidate_seconds:.3f} "
                    f"frames_scanned={scan_result.frames_scanned} "
                    f"decode_seconds={scan_result.decode_seconds:.3f} "
                    f"person_seconds={scan_result.person_inference_seconds:.3f} "
                    f"face_seconds={scan_result.face_inference_seconds:.3f} "
                    f"other_scan_seconds={scan_result.other_processing_seconds:.3f} "
                    f"max_person_confidence={max_person_confidence:.6f} "
                    f"max_face_confidence={max_face_confidence:.6f} verdict=SAFE"
                )
                return (
                    RenderBackground(
                        local_path=downloaded.local_path,
                        source_duration_seconds=candidate.duration_seconds,
                        pexels_video_id=candidate.pexels_video_id,
                        requires_center_crop=candidate.requires_center_crop,
                    ),
                    candidate,
                )

            reason = _record_rejection(scan_result)
            emit_log(
                f"[BACKGROUND] candidate={candidate_number}/{request.max_background_attempts} "
                f"id={candidate.pexels_video_id} scan_seconds={scan_seconds:.3f} "
                f"total_candidate_seconds={total_candidate_seconds:.3f} "
                f"frames_scanned={scan_result.frames_scanned} "
                f"decode_seconds={scan_result.decode_seconds:.3f} "
                f"person_seconds={scan_result.person_inference_seconds:.3f} "
                f"face_seconds={scan_result.face_inference_seconds:.3f} "
                f"other_scan_seconds={scan_result.other_processing_seconds:.3f} "
                f"max_person_confidence={max_person_confidence:.6f} "
                f"max_face_confidence={max_face_confidence:.6f} verdict=REJECT "
                f"reason={reason}"
            )
            if os.path.exists(downloaded.local_path):
                os.remove(downloaded.local_path)
            _ensure_background_time_budget()
            return None, None

        # Query pool with deterministic shuffling
        query_pool = list(ALLOWED_NATURE_QUERIES)
        rng.shuffle(query_pool)

        # Search pages to try for each query (page 1 first, then page 2 for diversity)
        search_pages = (1, 2)

        stages.append(STAGE_SELECTING_BACKGROUNDS)

        # 1. First attempt: If auto_prefer_single or single, search for ONE continuous clip covering the entire Reel
        if strategy in ("auto_prefer_single", "single"):
            for page in search_pages:
                _ensure_background_time_budget()
                if safe_backgrounds:
                    break
                if attempts >= request.max_background_attempts:
                    break
                for query in query_pool:
                    _ensure_background_time_budget()
                    if safe_backgrounds:
                        break
                    if attempts >= request.max_background_attempts:
                        break

                    try:
                        candidates = _invoke_search_bgs(query, page)
                        queries_attempted.add(query)
                        pages_attempted.add(page)
                        emit_log(
                            f"[BACKGROUND] search query={query!r} page={page} "
                            f"candidates={len(candidates)}"
                        )
                    except Exception as err:
                        emit_log(
                            f"[BACKGROUND] search query={query!r} page={page} "
                            f"verdict=SEARCH_FAILED error_type={type(err).__name__}"
                        )
                        continue

                    long_candidates = sorted(
                        (
                            candidate
                            for candidate in candidates
                            if candidate_cost_rejection_reason(
                                candidate,
                                target_reel_duration * 0.5,
                                request.max_background_source_file_size_bytes,
                            )
                            is None
                        ),
                        key=lambda candidate: background_candidate_runtime_rank(
                            candidate,
                            target_reel_duration,
                        ),
                    )
                    for candidate in candidates:
                        rejection = candidate_cost_rejection_reason(
                            candidate,
                            target_reel_duration * 0.5,
                            request.max_background_source_file_size_bytes,
                        )
                        if rejection is not None:
                            prefilter_rejections[rejection] = prefilter_rejections.get(rejection, 0) + 1
                    for cand in long_candidates:
                        if safe_backgrounds:
                            break
                        if attempts >= request.max_background_attempts:
                            break
                        if cand.pexels_video_id in recent_ids or cand.pexels_video_id in scanned_ids:
                            continue

                        render_background, safe_candidate = _process_background_candidate(cand)
                        if render_background is not None and safe_candidate is not None:
                            safe_candidates.append(safe_candidate)
                            safe_backgrounds.append(render_background)
                            break

        # 2. Second attempt: If multiple videos requested, or auto_prefer_single fallback when no single clip found
        if not safe_backgrounds and strategy != "single":
            accumulated_duration = 0.0
            multi_max_clips = max(1, min(3, request.max_background_clips))
            for page in search_pages:
                _ensure_background_time_budget()
                if len(safe_backgrounds) >= multi_max_clips:
                    break
                if (
                    safe_backgrounds
                    and (strategy != "multiple" or len(safe_backgrounds) >= 2)
                    and (accumulated_duration * 2.0) >= (
                        target_reel_duration + (len(safe_backgrounds) - 1) * request.render_config.crossfade_seconds
                    )
                ):
                    break
                if attempts >= request.max_background_attempts:
                    break
                for query in query_pool:
                    _ensure_background_time_budget()
                    if len(safe_backgrounds) >= multi_max_clips:
                        break
                    if (
                        safe_backgrounds
                        and (strategy != "multiple" or len(safe_backgrounds) >= 2)
                        and (accumulated_duration * 2.0) >= (
                            target_reel_duration + (len(safe_backgrounds) - 1) * request.render_config.crossfade_seconds
                        )
                    ):
                        break
                    if attempts >= request.max_background_attempts:
                        break

                    try:
                        candidates = _invoke_search_bgs(query, page)
                        queries_attempted.add(query)
                        pages_attempted.add(page)
                        emit_log(
                            f"[BACKGROUND] search query={query!r} page={page} "
                            f"candidates={len(candidates)}"
                        )
                    except Exception as err:
                        emit_log(
                            f"[BACKGROUND] search query={query!r} page={page} "
                            f"verdict=SEARCH_FAILED error_type={type(err).__name__}"
                        )
                        continue

                    ranked_candidates = sorted(
                        (
                            candidate
                            for candidate in candidates
                            if candidate_cost_rejection_reason(
                                candidate,
                                target_reel_duration,
                                request.max_background_source_file_size_bytes,
                                require_sufficient_duration=False,
                            )
                            is None
                        ),
                        key=lambda candidate: background_candidate_runtime_rank(
                            candidate,
                            max(0.0, target_reel_duration - accumulated_duration),
                        ),
                    )
                    for candidate in candidates:
                        rejection = candidate_cost_rejection_reason(
                            candidate,
                            target_reel_duration,
                            request.max_background_source_file_size_bytes,
                            require_sufficient_duration=False,
                        )
                        if rejection is not None:
                            prefilter_rejections[rejection] = prefilter_rejections.get(rejection, 0) + 1
                    for cand in ranked_candidates:
                        if len(safe_backgrounds) >= multi_max_clips:
                            break
                        if (
                            safe_backgrounds
                            and (strategy != "multiple" or len(safe_backgrounds) >= 2)
                            and (accumulated_duration * 2.0) >= (
                                target_reel_duration + (len(safe_backgrounds) - 1) * request.render_config.crossfade_seconds
                            )
                        ):
                            break
                        if attempts >= request.max_background_attempts:
                            break

                        if cand.pexels_video_id in recent_ids or cand.pexels_video_id in scanned_ids:
                            continue

                        # Visual Coherence Gate for multi-clip groups
                        if safe_candidates:
                            existing_attrs = [extract_visual_attributes(c.query) for c in safe_candidates]
                            cand_attr = extract_visual_attributes(cand.query)
                            coherent, reason = are_all_clips_coherent(existing_attrs + [cand_attr])
                            if not coherent:
                                emit_log(
                                    f"[BACKGROUND] candidate id={cand.pexels_video_id} "
                                    f"query={cand.query!r} verdict=SKIP reason=INCOHERENT detail={reason}"
                                )
                                continue

                        render_background, safe_candidate = _process_background_candidate(cand)
                        if render_background is not None and safe_candidate is not None:
                            safe_candidates.append(safe_candidate)
                            safe_backgrounds.append(render_background)
                            accumulated_duration += cand.duration_seconds

        if not safe_backgrounds:
            raise _background_diagnostics(
                "SAFE_BACKGROUND_EXHAUSTED",
                "Unable to collect verified safe background clips "
                f"(found {len(safe_backgrounds)} after {attempts} attempts).",
            )
        stages.append(STAGE_SCANNING_BACKGROUNDS)
        emit_log(
            f"[STAGE] background_acquisition completed seconds={_background_elapsed():.3f} "
            f"unique_candidates_attempted={len(scanned_ids)} "
            f"unique_candidates_scanned={len(safety_scanned_ids)} "
            f"safe_candidates={len(safe_backgrounds)}"
        )

        # 8 & 9. Bounded Composite Render + Final Safety Scan Retry Loop
        max_render_attempts = request.max_render_attempts
        render_fn = render_reel_fn or render_reel
        scan_vid_fn = scan_video_fn or scan_vid

        render_res: Optional[RenderResult] = None
        final_safety: Optional[SafetyScanResult] = None
        successful_render_attempt = 1
        last_failure_reason = "Unknown render safety failure"
        attempted_plan_signatures: set[tuple[tuple[str, float, float, Optional[float]], ...]] = set()
        selected_render_backgrounds: tuple[RenderBackground, ...] = tuple(safe_backgrounds)
        selected_pool_records: tuple[VerifiedBackgroundRecord, ...] = tuple(safe_pool_records)
        selected_safe_candidates: tuple[BackgroundCandidate, ...] = tuple(safe_candidates)
        can_rotate_single_background = (
            strategy == "single"
            or (
                strategy == "auto_prefer_single"
                and all(
                    background.source_duration_seconds >= target_reel_duration * 0.5
                    for background in safe_backgrounds
                )
            )
        )
        failed_background_identities: set[tuple[str, str]] = set()

        def _get_background_identity(
            idx: int,
            bgs: Sequence[RenderBackground],
            cands: Sequence[BackgroundCandidate],
            records: Sequence[VerifiedBackgroundRecord],
        ) -> tuple[str, str]:
            if idx < len(records):
                return (str(records[idx].provider), str(records[idx].provider_asset_id))
            if idx < len(cands):
                provider = getattr(cands[idx], "provider", "pexels")
                asset_id = getattr(cands[idx], "provider_asset_id", getattr(cands[idx], "pexels_video_id", 0))
                return (str(provider), str(asset_id))
            if idx < len(bgs):
                return ("pexels", str(bgs[idx].pexels_video_id))
            return ("unknown", str(idx))

        for render_attempt in range(1, max_render_attempts + 1):
            attempt_seed = (
                request.seed + (render_attempt - 1) * 10007
                if request.seed is not None
                else rng.randint(1000, 999999)
            )

            if can_rotate_single_background and safe_backgrounds:
                available_indices = [
                    idx
                    for idx in range(len(safe_backgrounds))
                    if _get_background_identity(idx, safe_backgrounds, safe_candidates, safe_pool_records)
                    not in failed_background_identities
                ]

                # If all currently downloaded backgrounds have failed, try to acquire the next eligible un-downloaded record
                if not available_indices and pool_records:
                    for rec in pool_records:
                        rec_ident = (str(rec.provider), str(rec.provider_asset_id))
                        if rec_ident in failed_background_identities:
                            continue
                        if any(
                            _get_background_identity(i, safe_backgrounds, safe_candidates, safe_pool_records) == rec_ident
                            for i in range(len(safe_backgrounds))
                        ):
                            continue
                        if _acquire_pool_record(rec):
                            new_idx = len(safe_backgrounds) - 1
                            available_indices.append(new_idx)
                            break

                if available_indices:
                    selected_index = available_indices[0]
                else:
                    # All eligible backgrounds have been tried in this run.
                    # Per policy: "Only reuse a previously failed background when no other
                    # verified eligible asset exists."
                    selected_index = (render_attempt - 1) % len(safe_backgrounds)

                attempt_backgrounds = (safe_backgrounds[selected_index],)
                attempt_candidates = (
                    (safe_candidates[selected_index],)
                    if selected_index < len(safe_candidates)
                    else ()
                )
                attempt_pool_records = (
                    (safe_pool_records[selected_index],)
                    if selected_index < len(safe_pool_records)
                    else ()
                )
            else:
                attempt_backgrounds = tuple(safe_backgrounds)
                attempt_candidates = tuple(safe_candidates)
                attempt_pool_records = tuple(safe_pool_records)

            render_req = RenderRequest(
                surah_number=selected_surah,
                arabic_surah_name=surah_obj.arabic_name,
                english_surah_name=surah_obj.english_name,
                ayahs=tuple(render_ayahs),
                surah_audio_path=audio_path,
                source_lead_ms=source_lead_ms,
                backgrounds=attempt_backgrounds,
                output_path=final_output_path,
                seed=attempt_seed,
            )

            # The production renderer uses this same deterministic segment
            # planner.  Preflight the retry identity before FFmpeg so a failed
            # final-safety retry never re-renders the identical plan.  Test
            # renderers are intentionally allowed to use synthetic tiny files
            # and need not satisfy FFmpeg source-duration validation.
            if render_reel_fn is None:
                planned_segments = calculate_background_segments(
                    render_req,
                    request.render_config,
                )
                plan_signature = tuple(
                    (
                        plan.local_path,
                        round(plan.start_offset_seconds, 6),
                        round(plan.segment_duration_seconds, 6),
                        (round(plan.transition_offset_seconds, 6)
                         if plan.transition_offset_seconds is not None else None),
                    )
                    for plan in planned_segments
                )
                if plan_signature in attempted_plan_signatures:
                    raise GenerationError(
                        "RENDER_RETRY_NO_DISTINCT_BACKGROUND_PLAN: refusing to replay "
                        "an identical source/offset plan after a final-safety failure."
                    )
                attempted_plan_signatures.add(plan_signature)

            if production:
                # Owner-v1 exact reviewed ranges were source/timing checked at
                # selection; retired theme grants must not override owner review.
                if request.owner_ayah_key is None:
                    require_candidate_eligible(
                        selected_candidate,
                        production_authorization=production_authorization,
                    )
                actual_keys = tuple(f"{ayah.surah_number}:{ayah.ayah_number}" for ayah in render_req.ayahs)
                expected_keys = tuple(f"{selected_surah}:{n}" for n in range(first_ayah, last_ayah + 1))
                if actual_keys != expected_keys:
                    raise GenerationError("THEME_POLICY_REJECTED: render range mismatch")

            render_started_at = monotonic()
            emit_log(f"[STAGE] final_render started attempt={render_attempt}/{max_render_attempts}")
            try:
                render_res = render_fn(render_req, config=request.render_config)
                final_output_created = True
            except Exception as err:
                raise GenerationError(f"Rendering failed: {err}") from err

            emit_log(
                f"[STAGE] final_render completed attempt={render_attempt} "
                f"seconds={monotonic() - render_started_at:.3f}"
            )

            # Mandatory Final Safety Scan on Rendered Output
            final_scan_started_at = monotonic()
            emit_log(f"[STAGE] final_safety_scan started attempt={render_attempt}")
            try:
                if scan_video_fn is None:
                    final_safety = scan_vid_fn(
                        final_output_path,
                        config=SafetyConfig(animal_detection_required=True),
                    )
                else:
                    final_safety = scan_vid_fn(final_output_path)
            except Exception as err:
                if os.path.exists(final_output_path):
                    os.remove(final_output_path)
                raise GenerationError(f"Final safety scan failed to execute: {err}") from err

            if final_safety.safe:
                successful_render_attempt = render_attempt
                selected_render_backgrounds = attempt_backgrounds
                selected_pool_records = attempt_pool_records
                selected_safe_candidates = attempt_candidates
                emit_log(
                    f"[STAGE] final_safety_scan completed attempt={render_attempt} "
                    f"seconds={monotonic() - final_scan_started_at:.3f} "
                    f"frames_scanned={final_safety.frames_scanned} verdict=SAFE"
                )
                break
            else:
                last_failure_reason = final_safety.failure_reason or "Visual safety violation detected"
                emit_log(
                    f"[STAGE] final_safety_scan rejected attempt={render_attempt}/{max_render_attempts} "
                    f"reason={last_failure_reason}"
                )

                # Record attempted background identities in per-run failed background set
                for idx, bg in enumerate(attempt_backgrounds):
                    rec = attempt_pool_records[idx] if idx < len(attempt_pool_records) else None
                    cand = attempt_candidates[idx] if idx < len(attempt_candidates) else None
                    if rec is not None:
                        ident = (str(rec.provider), str(rec.provider_asset_id))
                    elif cand is not None:
                        p = getattr(cand, "provider", "pexels")
                        aid = getattr(cand, "provider_asset_id", getattr(cand, "pexels_video_id", 0))
                        ident = (str(p), str(aid))
                    else:
                        ident = ("pexels", str(bg.pexels_video_id))
                    failed_background_identities.add(ident)

                # Record failure telemetry event to background_events
                if record_render_safety_failure_fn is not None:
                    all_detections = list(final_safety.person_detections) + list(final_safety.face_detections) + list(final_safety.animal_detections)
                    failing_clip_idx: Optional[int] = 0
                    ambiguous_attribution = False
                    if all_detections and getattr(render_res, "plans", None) and len(render_res.plans) > 1:
                        earliest_ts = None
                        for d in all_detections:
                            ts = getattr(d, "timestamp_seconds", getattr(d, "timestamp", None))
                            if ts is not None and (earliest_ts is None or ts < earliest_ts):
                                earliest_ts = ts
                        if earliest_ts is not None:
                            active_plan_indices: list[int] = []
                            for p in render_res.plans:
                                start_t = p.transition_offset_seconds if p.transition_offset_seconds is not None else 0.0
                                end_t = start_t + p.segment_duration_seconds
                                if start_t <= earliest_ts <= end_t:
                                    active_plan_indices.append(p.clip_index)
                            if len(active_plan_indices) == 1:
                                failing_clip_idx = active_plan_indices[0]
                            elif len(active_plan_indices) > 1:
                                # During an xfade the output contains more than
                                # one source.  Keep the complete plan for
                                # investigation but do not falsely blame the
                                # first source clip.
                                failing_clip_idx = None
                                ambiguous_attribution = True

                    primary_provider = "pexels"
                    primary_asset_id = "0"
                    primary_variant_id = "0"
                    primary_sha = ""
                    if ambiguous_attribution:
                        primary_provider = "unknown"
                        primary_asset_id = "ambiguous_crossfade"
                        primary_variant_id = "ambiguous_crossfade"
                    elif failing_clip_idx is not None and attempt_pool_records and failing_clip_idx < len(attempt_pool_records):
                        primary_provider = attempt_pool_records[failing_clip_idx].provider
                        primary_asset_id = attempt_pool_records[failing_clip_idx].provider_asset_id
                        primary_variant_id = attempt_pool_records[failing_clip_idx].provider_variant_id
                        primary_sha = attempt_pool_records[failing_clip_idx].content_sha256
                    elif failing_clip_idx is not None and attempt_backgrounds and failing_clip_idx < len(attempt_backgrounds):
                        primary_asset_id = str(attempt_backgrounds[failing_clip_idx].pexels_video_id)
                        primary_variant_id = str(attempt_backgrounds[failing_clip_idx].pexels_video_id)

                    v_keys = [f"{ayah.surah_number}:{ayah.ayah_number}" for ayah in render_ayahs]
                    counts = {
                        "person": len(final_safety.person_detections),
                        "face": len(final_safety.face_detections),
                        "animal": len(final_safety.animal_detections),
                    }
                    bg_plans = []
                    for p in getattr(render_res, "plans", ()):
                        p_prov = "pexels"
                        p_aid = "0"
                        p_vid = "0"
                        p_s = ""
                        if attempt_pool_records and p.clip_index < len(attempt_pool_records):
                            p_prov = attempt_pool_records[p.clip_index].provider
                            p_aid = attempt_pool_records[p.clip_index].provider_asset_id
                            p_vid = attempt_pool_records[p.clip_index].provider_variant_id
                            p_s = attempt_pool_records[p.clip_index].content_sha256
                        elif attempt_backgrounds and p.clip_index < len(attempt_backgrounds):
                            p_aid = str(attempt_backgrounds[p.clip_index].pexels_video_id)
                            p_vid = str(attempt_backgrounds[p.clip_index].pexels_video_id)

                        bg_plans.append(
                            {
                                "clip_index": p.clip_index,
                                "provider": p_prov,
                                "provider_asset_id": p_aid,
                                "provider_variant_id": p_vid,
                                "content_sha256": p_s,
                                "start_offset_seconds": p.start_offset_seconds,
                                "segment_duration_seconds": p.segment_duration_seconds,
                                "transition_offset_seconds": p.transition_offset_seconds,
                                "primary_attribution": (
                                    "AMBIGUOUS_CROSSFADE" if ambiguous_attribution
                                    else "DETERMINED"
                                ),
                            }
                        )
                    start_off = (
                        render_res.plans[failing_clip_idx].start_offset_seconds
                        if getattr(render_res, "plans", None) and failing_clip_idx is not None and failing_clip_idx < len(render_res.plans)
                        else 0.0
                    )
                    try:
                        record_render_safety_failure_fn(
                            provider=primary_provider,
                            provider_asset_id=primary_asset_id,
                            provider_variant_id=primary_variant_id,
                            content_sha256=primary_sha,
                            attempt_number=render_attempt,
                            start_offset_seconds=start_off,
                            verse_keys=v_keys,
                            render_config_fingerprint=render_config_fp,
                            failure_reason=last_failure_reason,
                            detector_counts=counts,
                            background_plan=bg_plans,
                        )
                    except Exception as tel_err:
                        emit_log(f"[TELEMETRY] record_render_safety_failure failed: {tel_err}")

                # Clean up failed temporary render
                if os.path.exists(final_output_path):
                    try:
                        os.remove(final_output_path)
                    except OSError:
                        pass
                final_output_created = False

                if render_attempt >= max_render_attempts:
                    raise GenerationError(
                        f"MAX_RENDER_SAFETY_RETRIES_EXCEEDED (attempted {max_render_attempts}): {last_failure_reason}"
                    )

        stages.append(STAGE_RENDERING)
        stages.append(STAGE_FINAL_SAFETY_SCAN)
        stages.append(STAGE_SUCCESS)
        emit_log(
            f"[STAGE] pipeline completed seconds={monotonic() - pipeline_started_at:.3f}"
        )

        # The successful render plan determines both usage history and warnings.
        selected_indices = {plan.clip_index for plan in getattr(render_res, "plans", ())}
        actual_pool_records = tuple(
            record for index, record in enumerate(selected_pool_records)
            if not selected_indices or index in selected_indices
        )
        reuse_warnings = background_reuse_warnings(
            actual_pool_records,
            tuple(record for record in pool_records
                  if record.record_id not in unavailable_pool_records
                  and (record.provider, record.provider_asset_id) not in failed_background_identities),
            request.background_usage_events,
            recent_provider_identities=tuple(("pexels", str(asset_id)) for asset_id in recent_ids),
        )
        for warning in reuse_warnings:
            emit_log("[BACKGROUND_REUSE_FALLBACK] " + json.dumps(warning, sort_keys=True))

        bg_provenance = [
            {
                "provider": getattr(cand, "provider", "pexels"),
                "provider_asset_id": getattr(cand, "provider_asset_id", str(getattr(cand, "pexels_video_id", 0))),
                "video_id": getattr(cand, "pexels_video_id", 0),
                "videographer_name": getattr(cand, "videographer_name", ""),
                "videographer_url": getattr(cand, "videographer_url", ""),
                "page_url": getattr(cand, "pexels_page_url", getattr(cand, "page_url", "")),
            }
            for index, cand in enumerate(selected_safe_candidates)
            if not selected_indices or index in selected_indices
        ]
        subtitle_evidence = []
        for ayah in render_ayahs:
            evidence = dict(ayah.subtitle_provenance)
            evidence["verse_key"] = f"{ayah.surah_number}:{ayah.ayah_number}"
            evidence["arabic_source_hash"] = normalized_sha256(ayah.arabic_text)
            for translation in ayah.ordered_translations:
                evidence[TRANSLATION_HASH_FIELDS[translation.language]] = normalized_sha256(translation.text)
            subtitle_evidence.append(evidence)

        provenance = {
            "theme_policy_version": (
                THEME_POLICY_VERSION if production and request.owner_ayah_key is None else None
            ),
            "production_authorization": (
                production_authorization.require_range(
                    selected_surah,
                    first_ayah,
                    last_ayah,
                ).snapshot()
                if production_authorization is not None
                else None
            ),
            "owner_v1": ({
                "catalog_id": "calm-reels-editorial-shortlist-v1",
                "source_sha256": hashlib.sha256(
                    render_ayahs[0].arabic_text.encode("utf-8")
                ).hexdigest(),
                "ayah_key": request.owner_ayah_key,
                **({"source_audio_window": {
                    "recording_sha256": FATIHA_READ_92_MP3_SHA256,
                    "clip_start_ms": 100, "annotated_start_ms": 300, "end_ms": 3240,
                }} if source_lead_ms else {}),
                "ayah_keys": [f'{a.surah_number}:{a.ayah_number}' for a in render_ayahs],
                "source_sha256_by_key": {f'{a.surah_number}:{a.ayah_number}': hashlib.sha256(a.arabic_text.encode('utf-8')).hexdigest() for a in render_ayahs},
                "cycle_number": request.owner_cycle_number,
                **({"review_approval": request.owner_reviewed_approval.snapshot(),
                    "review_approval_sha256": request.owner_reviewed_approval.sha256}
                   if request.owner_reviewed_approval is not None else {}),
                "editorial_selection_only": True,
            } if request.owner_ayah_key is not None else None),
            "quran_source": "Quran Foundation Content API v4 (text_uthmani)",
            # Records the policy checked at generation, never platform
            # delivery or current presence. Dashboard delivery evidence and
            # per-run cleanup reconciliation remain independent gates.
            **({
                "editorial_reconciliation_policy_version": "editorial-reconciliation-v1",
                "editorial_reconciliation_status": "APPROVED_AT_GENERATION",
            } if production and request.owner_ayah_key is None else {}),
            "translation_source": "Ruwwad Translation Center via QuranEnc.com",
            "translation_sources": [
                {
                    "language": language,
                    "translation_key": translations_by_language[language].source.translation_key,
                    "source": "Ruwwad Translation Center via QuranEnc.com",
                    "version": translations_by_language[language].source.version,
                    "last_update": translations_by_language[language].source.last_update,
                }
                for language in request.translation_languages
            ],
            "subtitle_ayahs": subtitle_evidence,
            # Caption text is derived only from this immutable render plan,
            # rather than re-fetching Quran or translation data after media
            # generation.  Hashes make stale/mutated provenance fail closed.
            "caption_ayahs": [
                {
                    "verse_key": f"{ayah.surah_number}:{ayah.ayah_number}",
                    "arabic_text": ayah.arabic_text,
                    "arabic_normalized_sha256": normalized_sha256(ayah.arabic_text),
                    "translations": {
                        translation.language: {
                            "text": translation.text,
                            "normalized_sha256": normalized_sha256(translation.text),
                            "translation_key": translation.translation_key,
                            "translation_version": translation.translation_version,
                        }
                        for translation in ayah.ordered_translations
                    },
                }
                for ayah in render_ayahs
            ],
            "audio_reciter": f"{timing_read.reciter_name} (MP3Quran read={timing_read.read_id})",
            "audio_riwaya": timing_read.riwaya,
            "audio_server": timing_read.audio_server,
            "render_attempts": successful_render_attempt,
            "pexels_videos": bg_provenance,
            "background_source_mode": background_source_mode,
            "background_reuse_warnings": list(reuse_warnings),
            "verified_backgrounds": [
                {
                    "provider": record.provider,
                    "provider_asset_id": record.provider_asset_id,
                    "provider_file_id": getattr(record, "provider_variant_id", getattr(record, "provider_file_id", "")),
                    "provider_variant_id": getattr(record, "provider_variant_id", getattr(record, "provider_file_id", "")),
                    "verified_record_id": getattr(record, "record_id", f"{record.provider}:{record.provider_asset_id}"),
                    "verified_content_sha256": record.content_sha256,
                    "safety_policy_fingerprint": record.safety.policy_fingerprint,
                }
                for record in actual_pool_records
            ],
        }

        return RunResult(
            success=True,
            slot=request.slot,
            surah_number=selected_surah,
            first_ayah=first_ayah,
            last_ayah=last_ayah,
            duration_seconds=render_res.duration_seconds,
            output_path=final_output_path,
            pexels_video_ids=tuple(
                bg.pexels_video_id for index, bg in enumerate(selected_render_backgrounds)
                if not selected_indices or index in selected_indices
            ),
            final_safety_result=final_safety,
            stages_completed=tuple(stages),
            provenance=provenance,
        )

    except GenerationError as err:
        stages.append(STAGE_FAILED)
        if final_output_created and os.path.exists(final_output_path):
            try:
                os.remove(final_output_path)
            except OSError:
                pass
        return RunResult(
            success=False,
            slot=request.slot,
            surah_number=selected_surah,
            first_ayah=first_ayah,
            last_ayah=last_ayah,
            stages_completed=tuple(stages),
            failure_reason=str(err),
        )
    except Exception as err:
        stages.append(STAGE_FAILED)
        if final_output_created and os.path.exists(final_output_path):
            try:
                os.remove(final_output_path)
            except OSError:
                pass
        return RunResult(
            success=False,
            slot=request.slot,
            surah_number=selected_surah,
            first_ayah=first_ayah,
            last_ayah=last_ayah,
            stages_completed=tuple(stages),
            failure_reason=f"Unexpected pipeline error: {err}",
        )
    finally:
        # Clean temporary downloaded assets and run directory
        if os.path.exists(run_temp_dir):
            shutil.rmtree(run_temp_dir, ignore_errors=True)
