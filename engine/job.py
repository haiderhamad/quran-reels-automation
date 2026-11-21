"""Production GitHub Actions worker and Backblaze B2-backed cloud run orchestration."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from engine.calm_reels_cycle import OwnerNextAyahDecision, preview_next_owner_ayah_from_store
from engine.calm_reels_scope import CalmReelsScopeError, restrict_to_owner_shortlist
from engine.config import DurationConfig, VerseSelectionLimits
from engine.reel_publication_policy import require_record_eligible
from engine.reel_theme_policy import EditorialPolicyError
from engine.production_authorization import (
    ProductionAuthorizationCatalog,
    ProductionAuthorizationError,
    load_production_authorization_catalog,
)
from engine.scheduler import production_schedule_entries
from engine.main import GenerationError, TEST_VERIFIED_VERSE_KEYS, RunRequest, RunResult, generate_reel
from engine.background_pool import (
    BackgroundPoolRepository,
    BackgroundUsageEvent,
    StaleBackgroundRecord,
    get_background_pool_repository,
    utc_now_iso as pool_utc_now_iso,
)
from engine.renderer import RenderConfig
from engine.subtitles import SubtitleConfig
from engine.layout_config import TRANSLATION_MAX_LINES, SURA_HEADER_OFFSET
from engine.publisher import (
    CANONICAL_SURAH_NAMES,
    MetaConfig,
    PlatformPublishResult,
    PublishError,
    PublishRequest,
    PublishResult,
    build_reel_caption_from_record,
    check_reciter_reuse_rights,
    load_meta_config_from_env,
    publish_reel,
)
from engine.buffer_publisher import (
    BUFFER_BACKEND,
    BufferChannel,
    BufferClient,
    BufferPublisherError,
    BufferPublishRequest,
    buffer_facebook_enabled,
    buffer_instagram_enabled,
    buffer_publishing_enabled,
    build_buffer_facebook_caption,
    load_buffer_config_from_env,
)
from engine.buffer_lifecycle import (
    BufferPublicationCoordinator,
    PUBLICATION_DISPATCHING,
    PUBLICATION_SUCCESS,
    PUBLICATION_UNKNOWN,
    publication_fingerprint,
    publication_source,
)
from engine.public_media_delivery import PublicMediaDeliveryError, ensure_public_media_delivery
from engine.meta_credentials import load_meta_config_from_b2
from engine.state_authority import (
    StateStore,
    get_active_state_store,
    get_state_authority,
)
from engine.storage import (
    BackgroundHistory,
    CoverageState,
    B2Config,
    RunRecord,
    StorageError,
    StoredConfig,
    stored_arabic_translation_gap,
    stored_translation_stack_gap,
    create_b2_client,
    get_recent_content_history,
    get_run_record,
    load_background_history,
    load_config,
    load_coverage,
    load_b2_config_from_env,
    object_exists,
    put_run_record,
    reconcile_coverage_from_runs,
    update_coverage_for_run,
    upload_reel,
)


class JobError(Exception):
    """Raised when an error occurs during cloud job orchestration."""


LOGGER = logging.getLogger("quran_reels.job")


_SAFE_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_SCHEDULE_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


@dataclass(frozen=True)
class JobRequest:
    """Request parameters for a cloud production or test Reel generation job."""

    slot: str  # stable schedule ID, "manual-production", or "test"
    run_kind: Optional[str] = None  # "scheduled" | "manual_production" | "test"
    schedule_slot_id: Optional[str] = None
    requested_run_id: Optional[str] = None
    force: bool = False
    seed: Optional[int] = None
    local_output: Optional[str] = None
    dry_run: bool = False
    test_verse_key: Optional[str] = None
    publish_facebook: bool = True
    publish_instagram: bool = True

    def __post_init__(self) -> None:
        resolved_kind = self.run_kind
        if resolved_kind is None:
            if self.slot in ("morning", "evening"):
                resolved_kind = "scheduled"
            elif self.slot == "test":
                resolved_kind = "test"
            else:
                raise JobError(
                    f"Invalid legacy slot '{self.slot}'. Dynamic slots require run_kind='scheduled'."
                )
        if resolved_kind not in ("scheduled", "manual_production", "test"):
            raise JobError("run_kind must be scheduled, manual_production, or test.")
        object.__setattr__(self, "run_kind", resolved_kind)
        if resolved_kind == "scheduled":
            slot_id = self.schedule_slot_id or self.slot
            if not slot_id or not _SCHEDULE_ID_REGEX.fullmatch(slot_id) or ".." in slot_id:
                raise JobError("schedule_slot_id must be a safe non-empty identifier.")
            object.__setattr__(self, "schedule_slot_id", slot_id)
            if self.slot != slot_id:
                raise JobError("slot and schedule_slot_id must match for scheduled runs.")
        elif resolved_kind == "manual_production":
            if self.slot != "manual-production" or self.schedule_slot_id is not None:
                raise JobError("manual_production requires slot='manual-production' and no schedule_slot_id.")
        elif self.slot != "test" or self.schedule_slot_id is not None:
            raise JobError("test run_kind requires slot='test' and no schedule_slot_id.")
        if self.test_verse_key is not None:
            if resolved_kind != "test":
                raise JobError("test_verse_key is permitted only for test runs.")
            if self.test_verse_key not in TEST_VERIFIED_VERSE_KEYS:
                raise JobError(
                    "test_verse_key must name an explicitly approved verified test fixture."
                )
        if self.requested_run_id is not None:
            requested = self.requested_run_id.strip()
            if not requested or not _SAFE_ID_REGEX.fullmatch(requested) or ".." in requested:
                raise JobError("requested_run_id must be a safe non-empty identifier.")
        if resolved_kind == "manual_production" and not (self.publish_facebook or self.publish_instagram):
            raise JobError("manual_production requires at least one publishing target.")


@dataclass(frozen=True)
class JobResult:
    """Structured result of a cloud production or test Reel generation job."""

    status: str  # "SUCCESS" | "SKIPPED" | "FAILED"
    slot: str
    date: str
    run_id: str
    media_key: Optional[str] = None
    generation_result: Optional[RunResult] = None
    facebook_status: Optional[str] = None
    instagram_status: Optional[str] = None
    facebook_media_id: Optional[str] = None
    buffer_post_id: Optional[str] = None
    facebook_reel_url: Optional[str] = None
    instagram_media_id: Optional[str] = None
    facebook_failure_reason: Optional[str] = None
    instagram_failure_reason: Optional[str] = None
    publishing_blocked: bool = False
    publishing_warning: Optional[str] = None
    skipped_reason: Optional[str] = None
    failure_reason: Optional[str] = None


def get_berlin_date(
    timezone_name: str = "Europe/Berlin",
    now: Optional[datetime.datetime] = None,
) -> str:
    """Return calendar date string (YYYY-MM-DD) for Europe/Berlin timezone."""
    tz = ZoneInfo(timezone_name)
    current_time = now if now is not None else datetime.datetime.now(tz)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=datetime.timezone.utc).astimezone(tz)
    else:
        current_time = current_time.astimezone(tz)
    return current_time.strftime("%Y-%m-%d")


def generate_run_id(slot: str, berlin_date: str) -> str:
    """Generate timestamped unique run_id."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S%f")
    clean_date = berlin_date.replace("-", "")
    return f"run_{clean_date}_{slot}_{ts}_{uuid.uuid4().hex[:8]}"


def normalize_translation_sources(raw_sources: Any) -> dict[str, dict[str, Any]]:
    """Normalize engine provenance into RunRecord's language-keyed audit mapping."""
    if isinstance(raw_sources, Mapping):
        return {
            str(language): dict(metadata)
            for language, metadata in raw_sources.items()
            if isinstance(metadata, Mapping)
        }
    if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes)):
        normalized: dict[str, dict[str, Any]] = {}
        for item in raw_sources:
            if not isinstance(item, Mapping):
                continue
            language = item.get("language")
            if not isinstance(language, str) or not language:
                continue
            normalized[language] = dict(item)
        return normalized
    return {}


def _record_production_pool_usage(
    repository: BackgroundPoolRepository,
    run_record: RunRecord,
    *,
    used_at: str,
) -> None:
    """Idempotently append the verified-pool usage event for a production checkpoint."""
    if run_record.run_kind not in ("scheduled", "manual_production"):
        return
    provenance = dict(run_record.provenance)
    if provenance.get("background_source_mode") != "VERIFIED_POOL":
        return
    records = provenance.get("verified_backgrounds", [])
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)) or not records:
        raise JobError("VERIFIED_POOL provenance is missing verified background identity.")
    for item in records:
        if not isinstance(item, Mapping):
            raise JobError("VERIFIED_POOL provenance has malformed background identity.")
        repository.save_usage(BackgroundUsageEvent(
            run_id=run_record.run_id,
            provider=str(item["provider"]),
            provider_asset_id=str(item["provider_asset_id"]),
            provider_variant_id=str(
                item.get("provider_variant_id", item.get("provider_file_id", ""))
            ),
            verified_record_id=str(item["verified_record_id"]),
            used_at=used_at,
            reel_duration_seconds=float(run_record.duration_seconds or 0.0),
        ))


def build_render_config_from_stored_config(stored_config: Any) -> RenderConfig:
    """Map stored configuration (or app_config dict/object) to production RenderConfig."""
    def _get(key: str, default: Any) -> Any:
        if isinstance(stored_config, Mapping):
            return stored_config.get(key, default)
        return getattr(stored_config, key, default)

    # Unversioned display-only fixtures specify measured pixels directly.
    if isinstance(stored_config, Mapping) and (
        "schema_version" in stored_config or "arabic_translation_gap_version" in stored_config
    ):
        arabic_gap = stored_arabic_translation_gap(stored_config)
        stack_gap = stored_translation_stack_gap(stored_config)
    else:
        arabic_gap = _get("arabic_translation_gap_px", 80)
        stack_gap = _get("translation_stack_gap_px", 80)

    sub_cfg = SubtitleConfig(
        subtitle_mode=_get("subtitle_mode", "progressive_phrase_sync"),
        arabic_font_size=_get("arabic_font_size", 180),
        translation_font_size=_get("translation_font_size", 50),
        arabic_max_lines=_get("arabic_max_lines", 2),
        translation_max_lines=_get("translation_max_lines", TRANSLATION_MAX_LINES["en"]),
        arabic_translation_gap_px=arabic_gap,
        translation_stack_gap_px=stack_gap,
        arabic_line_spacing=_get("arabic_line_spacing", 1.15),
        translation_line_spacing=_get("translation_line_spacing", 1.25),
        text_outline=_get("text_outline", "minimal"),
        text_shadow=_get("text_shadow", "very_soft"),
        highlight_mode=_get("highlight_mode", "phrase_highlight"),
        highlight_intensity=_get("highlight_intensity", "normal"),
        highlight_style=_get("highlight_style", "phrase_fade"),
        highlight_color=_get("highlight_color", "warm_white"),
        transition_duration_ms=_get("transition_duration_ms", 100),
        subtitle_lead_ms=_get("subtitle_lead_ms", 0),
        sura_header_top_offset=_get("sura_header_top_offset", SURA_HEADER_OFFSET["default"]),
        header_style=_get("header_style", "minimal_capsule"),
        header_content=_get("header_content", "arabic_translation_ayah_range"),
        header_backing_intensity=_get("header_backing_intensity", "low"),
        ayah_reference_position=_get("ayah_reference_position", "top_header"),
        subtitle_backing=_get("subtitle_backing", "shadow_only"),
    )
    return RenderConfig(subtitle_config=sub_cfg)


def _build_owner_v1_generation_request(
    request: JobRequest,
    config: StoredConfig,
    decision: OwnerNextAyahDecision,
) -> RunRequest:
    """Bind the first unconfirmed frozen ayah to the existing generator type.

    This prepares a technical preflight only: it does not grant publication,
    claim the ayah, generate media or advance the verified cycle.
    """
    if (request.run_kind not in ("scheduled", "manual_production")
        or not isinstance(decision, OwnerNextAyahDecision)
        or decision.state != "READY_FOR_TECHNICAL_PREFLIGHT"
        or not decision.next_ayah_key
        or type(decision.cycle_number) is not int
        or decision.cycle_number < 1):
        raise JobError("OWNER_V1_EXACT_TARGET_UNAVAILABLE")
    if decision.reviewed_ayah_keys and decision.reviewed_approval is None:
        raise JobError("OWNER_V1_REVIEW_APPROVAL_REQUIRED")
    return RunRequest(
        slot=request.slot,
        run_kind=request.run_kind,
        translation_languages=tuple(config.translation_languages),
        duration_config=DurationConfig(
            minimum_duration=config.min_duration_seconds,
            maximum_duration=config.max_duration_seconds,
        ),
        render_config=build_render_config_from_stored_config(config),
        owner_ayah_key=decision.next_ayah_key,
        owner_pending_successor=decision.pending_successor,
        owner_reviewed_ayah_keys=decision.reviewed_ayah_keys,
        owner_reviewed_registry=decision.reviewed_registry,
        owner_reviewed_approval=decision.reviewed_approval,
        verse_limits=VerseSelectionLimits(maximum_ayahs=None) if decision.reviewed_ayah_keys else VerseSelectionLimits(),
        owner_cycle_number=decision.cycle_number,
    )


def run_job(
    request: JobRequest,
    env: Optional[Mapping[str, str]] = None,
    b2_config: Optional[B2Config] = None,
    client: Optional[Any] = None,
    generate_reel_fn: Optional[Callable[..., RunResult]] = None,
    publish_reel_fn: Optional[Callable[..., PublishResult]] = None,
    publish_buffer_facebook_fn: Optional[Callable[..., PublishResult]] = None,
    now: Optional[datetime.datetime] = None,
    *,
    r2_config: Optional[B2Config] = None,
    production_authorization: Optional[ProductionAuthorizationCatalog] = None,
) -> JobResult:
    """Execute the cloud generation and publishing job coordinating B2 state, engine/main.py, and publisher.py."""
    # V1 production is NOT ready to dispatch. An explicit preview is read-only;
    # every other nonlegacy mode fails closed BEFORE opening any live provider.
    source_env = os.environ if env is None else env
    v1_mode = str(source_env.get("QURAN_REELS_V1_MODE", "legacy")).strip().lower()
    # Buffer is the sole operator-approved publishing transport for both
    # Facebook and Instagram. Do not initialize providers, render, upload, or
    # call the historic direct Meta publisher when explicitly configured.
    # Dependency-injected publishers exist only for offline legacy tests.
    if (
        request.run_kind in ("scheduled", "manual_production")
        and source_env.get("PUBLISHER_BACKEND", "").strip().lower() == "meta"
        and publish_reel_fn is None
    ):
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=get_berlin_date("Europe/Berlin", now=now),
            run_id=request.requested_run_id or f"buffer_only_blocked_{request.slot}",
            facebook_status="FAILED" if request.publish_facebook else "NOT_ATTEMPTED",
            instagram_status="FAILED" if request.publish_instagram else "NOT_ATTEMPTED",
            publishing_blocked=True,
            failure_reason="Meta configuration error: DIRECT_META_PUBLISHING_DISABLED_USE_BUFFER",
        )
    owner_v1_publish = (
        request.run_kind in ("scheduled", "manual_production")
        and v1_mode == "publish"
    )
    # Publication is never enabled by a mode string alone: only the existing
    # GitHub Action, explicit owner switch, Supabase and two Buffer channels.
    owner_v1_ready = (
        source_env.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        and source_env.get("STATE_AUTHORITY", "").strip().lower() == "supabase"
        and source_env.get("QURAN_REELS_V1_PUBLISH_ENABLED", "").strip().lower() == "true"
        and source_env.get("PUBLISHER_BACKEND", "").strip().lower() == BUFFER_BACKEND
        and buffer_facebook_enabled(source_env)
        and buffer_instagram_enabled(source_env)
        and request.publish_facebook and request.publish_instagram
        and not request.force and not request.dry_run
        and generate_reel_fn is None and publish_reel_fn is None
        and publish_buffer_facebook_fn is None
        and production_authorization is None
    )
    if request.run_kind in ("scheduled", "manual_production") and (
        v1_mode not in {"legacy", "preview", "publish"}
        or (owner_v1_publish and not owner_v1_ready)
    ):
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=get_berlin_date("Europe/Berlin", now=now),
            run_id=request.requested_run_id or f"v1_mode_blocked_{request.slot}",
            publishing_blocked=True,
            failure_reason=(
                "OWNER_V1_PUBLISH_NOT_READY"
                if owner_v1_publish else "CALM_V1_PUBLISHING_NOT_IMPLEMENTED"
            ),
        )
    # 1. Load B2 configuration and active StateStore
    try:
        config_b2 = b2_config if b2_config is not None else (r2_config if r2_config is not None else load_b2_config_from_env(env))
        s3_client = client if client is not None else create_b2_client(config_b2)
        authority = get_state_authority(env)
        state_store = get_active_state_store(env, b2_client=s3_client, b2_config=config_b2)
    except Exception as err:
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=get_berlin_date("Europe/Berlin", now=now),
            run_id=f"init_failed_{request.slot}",
            failure_reason=f"Failed to initialize storage: {err}",
        )

    # 2. Load StoredConfig from active state authority
    try:
        stored_config = state_store.load_config()
    except Exception as err:
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=get_berlin_date("Europe/Berlin", now=now),
            run_id=f"config_error_{request.slot}",
            failure_reason=f"Failed to load stored configuration: {err}",
        )

    if stored_config is None:
        if request.run_kind == "test":
            stored_config = StoredConfig()
        else:
            reason = (
                "Configuration (config/current.json) not found in Backblaze B2."
                if authority == "b2"
                else "Configuration not found in Supabase (app_config table)."
            )
            return JobResult(
                status="SKIPPED",
                slot=request.slot,
                date=get_berlin_date("Europe/Berlin", now=now),
                run_id=f"no_config_{request.slot}",
                skipped_reason=reason,
            )

    # 3. Validate fixed production timezone (StoredConfig validates language modes).
    if stored_config.timezone != "Europe/Berlin":
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=get_berlin_date("Europe/Berlin", now=now),
            run_id=f"invalid_tz_{request.slot}",
            failure_reason=f"Unsupported timezone '{stored_config.timezone}'. V1 production requires 'Europe/Berlin'.",
        )

    berlin_date = get_berlin_date(stored_config.timezone, now=now)
    run_id = request.requested_run_id or generate_run_id(request.slot, berlin_date)

    if request.run_kind in ("scheduled", "manual_production") and v1_mode == "preview":
        # Read the owner's frozen source-locked 2,211 ayah list and fresh
        # confirmed Facebook receipts only. No legacy topic-grant RPC, B2
        # media writes, run claims, publisher initialization, or dispatch.
        if authority != "supabase":
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                publishing_blocked=True, failure_reason="CALM_V1_SUPABASE_REQUIRED",
            )
        try:
            preview = preview_next_owner_ayah_from_store(state_store)
            if preview.state == "READY_FOR_TECHNICAL_PREFLIGHT":
                # Use the exact RunRequest understood by engine.main, rather
                # than the legacy random/authorized-passage selector. No
                # generator or external publisher is invoked in preview mode.
                _build_owner_v1_generation_request(request, stored_config, preview)
            elif preview.state != "COMPLETE_PENDING_VERIFIED_ROLLOVER" or preview.next_ayah_key is not None:
                raise JobError("OWNER_V1_PREVIEW_STATE_INVALID")
        except (JobError, GenerationError, ValueError, TypeError, AttributeError):
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                publishing_blocked=True, failure_reason="CALM_V1_PREVIEW_UNAVAILABLE",
            )
        return JobResult(
            status="SKIPPED", slot=request.slot, date=berlin_date, run_id=run_id,
            publishing_blocked=True,
            skipped_reason=(
                f"CALM_V1_{preview.state}: cycle={preview.cycle_number}; "
                f"ayah={preview.next_ayah_key or 'none'}; "
                f"confirmed={preview.confirmed_count}/2211; no publishing"
            ),
        )

    owner_v1_request: RunRequest | None = None
    if owner_v1_publish:
        if authority != "supabase" or not (
            stored_config.facebook_enabled and stored_config.instagram_enabled
        ) or not check_reciter_reuse_rights(stored_config.reciter_reuse_confirmed, env=source_env):
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                publishing_blocked=True, failure_reason="OWNER_V1_PUBLISH_CONFIGURATION_NOT_READY",
            )
        try:
            owner_decision = preview_next_owner_ayah_from_store(state_store)
            owner_v1_request = _build_owner_v1_generation_request(
                request, stored_config, owner_decision,
            )
        except (JobError, GenerationError, ValueError, TypeError, AttributeError):
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                publishing_blocked=True, failure_reason="OWNER_V1_NEXT_AYAH_UNAVAILABLE",
            )

    if request.run_kind in ("scheduled", "manual_production") and production_authorization is None and not owner_v1_publish:
        try:
            if authority == "supabase":
                authorization_store = state_store
            else:
                from engine.supabase_state import SupabaseConfig, SupabaseStateStore
                authorization_store = SupabaseStateStore(SupabaseConfig.from_env(env))
            production_authorization = load_production_authorization_catalog(
                authorization_store,
                policy_version="calm-inviting-v1",
            )
        except Exception as error:
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                failure_reason=f"PRODUCTION_AUTHORIZATION_UNAVAILABLE: {error}",
            )

    # A legacy production grant is necessary but not sufficient: restrict it
    # to whole passages inside the frozen owner's 2,211-ayah editorial scope.
    # This grants nothing new, never splits a context-dependent passage, and
    # leaves owner-v1 preview/publishing safety boundaries unchanged.
    # Injected generators/publishers are offline compatibility test seams; the
    # real GitHub Actions worker provides none of those override callbacks.
    if (request.run_kind in ("scheduled", "manual_production")
        and v1_mode == "legacy" and generate_reel_fn is None
        and publish_reel_fn is None and publish_buffer_facebook_fn is None):
        try:
            production_authorization = restrict_to_owner_shortlist(production_authorization)
        except (CalmReelsScopeError, ProductionAuthorizationError):
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                publishing_blocked=True,
                failure_reason="CALM_REELS_OWNER_SCOPE_OR_EXACT_GRANTS_UNAVAILABLE",
            )

    # Manual production is an explicit publish action and must fail closed before
    # rendering, upload, or Quran Coverage mutation when either rights gate is off.
    if request.run_kind == "manual_production" and not check_reciter_reuse_rights(
        stored_config.reciter_reuse_confirmed, env=env
    ):
        blocked_reason = "PUBLICATION_BLOCKED_RECITER_RIGHTS"
        return JobResult(
            status="SKIPPED",
            slot=request.slot,
            date=berlin_date,
            run_id=run_id,
            publishing_blocked=True,
            skipped_reason=blocked_reason,
        )

    # 4. Scheduled production always honors the automation master switch.
    if not stored_config.automation_enabled and request.run_kind == "scheduled":
        return JobResult(
            status="SKIPPED",
            slot=request.slot,
            date=berlin_date,
            run_id=run_id,
            skipped_reason="Automation is disabled in stored configuration (automation_enabled=False).",
        )

    # 5. The worker applies the same cadence gate as every scheduler provider.
    if request.run_kind == "scheduled":
        approved_slots = production_schedule_entries(stored_config.publishing_schedule)
        if not any(entry.id == request.schedule_slot_id for entry in approved_slots):
            return JobResult(
                status="SKIPPED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                skipped_reason="PUBLISHING_SCHEDULE_INELIGIBLE",
            )

    if owner_v1_request is not None:
        # A slow Buffer post may finish after the original job exits. Use this
        # invocation only to reconcile that same run, never to send it again.
        try:
            prior_id = state_store.pending_owner_dispatch_run(
                cycle_number=owner_v1_request.owner_cycle_number,
                ayah_key=owner_v1_request.owner_ayah_key,
            )
            if prior_id is not None:
                prior = state_store.get_run(prior_id)
                if (not isinstance(prior, RunRecord) or prior.run_id != prior_id
                    or prior.facebook_status != "ACCEPTED" or not prior.buffer_post_id
                    or prior.instagram_status != "ACCEPTED" or not prior.instagram_media_id
                    or not prior.public_media_delivery_id):
                    raise JobError("OWNER_V1_PARTIAL_OR_UNCERTAIN_DISPATCH")
                from engine.facebook_delivery_verification import confirm_after_buffer_acceptance
                checked, url = ensure_public_media_delivery(prior, source_env)
                if checked != prior:
                    raise JobError("OWNER_V1_SAVED_MEDIA_CHANGED")
                prior_buffer = BufferClient(load_buffer_config_from_env(source_env))
                verified, warning = confirm_after_buffer_acceptance(
                    prior, buffer=prior_buffer,
                    store=state_store, facebook_channel_id=prior_buffer.config.facebook_channel_id,
                    expected_public_media_url=url,
                )
                return JobResult(
                    status="SKIPPED", slot=request.slot, date=berlin_date, run_id=prior_id,
                    facebook_status=verified.facebook_status, instagram_status=verified.instagram_status,
                    facebook_reel_url=verified.facebook_reel_url, publishing_warning=warning,
                    skipped_reason="OWNER_V1_RECONCILED_EXISTING_RUN" if warning is None else "OWNER_V1_DELIVERY_STILL_UNCONFIRMED",
                )
        except Exception:
            return JobResult(status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                             publishing_blocked=True, failure_reason="OWNER_V1_CLAIM_RECONCILIATION_REQUIRED")

    # 6. Idempotency & Resumability check for production slots
    active_record: Optional[RunRecord] = None
    reused_generation: bool = False

    if request.run_kind in ("scheduled", "manual_production"):
        try:
            existing_rec = state_store.get_run_record(
                date=berlin_date,
                slot=request.slot,
                run_id=run_id if request.run_kind == "manual_production" else None,
                run_kind=request.run_kind,
            )
            if existing_rec is not None:
                if owner_v1_publish:
                    # Once any owner run exists, its provider outcome could be
                    # unknown; never re-render or re-send that logical run.
                    return JobResult(
                        status="FAILED", slot=request.slot, date=berlin_date,
                        run_id=run_id, publishing_blocked=True,
                        failure_reason="OWNER_V1_EXISTING_RUN_RECONCILE",
                    )
                if existing_rec.status == "STARTED":
                    return JobResult(
                        status="SKIPPED",
                        slot=request.slot,
                        date=berlin_date,
                        run_id=existing_rec.run_id,
                        skipped_reason=f"Run for {berlin_date} {request.slot} is currently in progress (status=STARTED).",
                    )
                elif existing_rec.status == "SUCCESS":
                    # Check if generated media exists in B2 (MediaStore)
                    if not existing_rec.media_key:
                        return JobResult(
                            status="FAILED",
                            slot=request.slot,
                            date=berlin_date,
                            run_id=existing_rec.run_id,
                            failure_reason="Existing run record is marked SUCCESS but missing media_key.",
                        )

                    if not object_exists(s3_client, config_b2, existing_rec.media_key):
                        return JobResult(
                            status="FAILED",
                            slot=request.slot,
                            date=berlin_date,
                            run_id=existing_rec.run_id,
                            media_key=existing_rec.media_key,
                            failure_reason=f"Stored Reel media is no longer available for publishing (key='{existing_rec.media_key}').",
                        )

                    # Check if all required publishing is already complete
                    # Facebook is primary; if Facebook is SUCCESS, ACCEPTED, or UNKNOWN, and Instagram is SUCCESS, ACCEPTED, UNKNOWN, or NOT_CONFIGURED, skip retry
                    fb_needed = (
                        stored_config.facebook_enabled
                        and request.publish_facebook
                        and existing_rec.facebook_status not in ("SUCCESS", "ACCEPTED", "UNKNOWN")
                    )
                    ig_needed = (
                        stored_config.instagram_enabled
                        and request.publish_instagram
                        and existing_rec.instagram_status not in ("SUCCESS", "ACCEPTED", "UNKNOWN", "NOT_CONFIGURED")
                    )

                    if not fb_needed and not ig_needed:
                        return JobResult(
                            status="SKIPPED",
                            slot=request.slot,
                            date=berlin_date,
                            run_id=existing_rec.run_id,
                            media_key=existing_rec.media_key,
                            facebook_status=existing_rec.facebook_status,
                            instagram_status=existing_rec.instagram_status,
                            facebook_media_id=existing_rec.facebook_media_id,
                            instagram_media_id=existing_rec.instagram_media_id,
                            facebook_failure_reason=existing_rec.facebook_failure_reason,
                            instagram_failure_reason=existing_rec.instagram_failure_reason,
                            skipped_reason=f"Run for {berlin_date} {request.slot} already completed successfully with all publishing finished.",
                        )

                    # Resume existing successful generation for incomplete publishing
                    active_record = existing_rec
                    reused_generation = True
                    run_id = existing_rec.run_id

        except Exception as err:
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                failure_reason=f"Failed to check existing run record: {err}",
            )

    gen_res: Optional[RunResult] = None
    utc_now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Use the same authoritative state adapter for pool evidence and usage.
    # In particular, never reconstruct Supabase usage from legacy
    # ``pexels_video_ids``: verified pool provenance carries the provider,
    # selected variant, record identity, and exact source hash.
    pool_repository = get_background_pool_repository(
        env,
        b2_client=s3_client,
        b2_config=config_b2,
        supabase_store=state_store if authority == "supabase" else None,
    )

    # 7. Execute Reel Generation pipeline (if not reusing existing generation)
    if not reused_generation:
        # Derive recent content history from active state store
        try:
            recent_history = state_store.get_recent_content_history(last_n_runs=30)
            recent_ranges = tuple(
                (int(item["surah_number"]), int(item["first_ayah"]), int(item["last_ayah"]))
                for item in recent_history
                if item.get("surah_number") is not None
                and item.get("first_ayah") is not None
                and item.get("last_ayah") is not None
            )
            recent_pexels_ids = tuple(sorted({
                int(video_id)
                for item in recent_history
                for video_id in item.get("pexels_video_ids", ())
            }))
        except Exception:
            recent_ranges, recent_pexels_ids = (), ()

        # Background-pool metadata failures degrade to the existing fully
        # scanned live-search path; they never disable the final Reel scan.
        try:
            stale_record_ids = pool_repository.stale_record_ids()
            verified_background_records = tuple(
                record
                for record in pool_repository.list_verified()
                if record.record_id not in stale_record_ids
            )
            background_usage_events = pool_repository.list_usage()
        except Exception as pool_load_err:
            LOGGER.warning(
                "[BACKGROUND] pool_metadata_unavailable fallback=LIVE_SEARCH_FALLBACK error_type=%s",
                type(pool_load_err).__name__,
            )
            verified_background_records = ()
            background_usage_events = ()

        # Quran Coverage Cycle State (Decision D-011)
        covered_ayahs: tuple[str, ...] = ()
        historical_pexels_ids: tuple[int, ...] = ()
        if request.run_kind in ("scheduled", "manual_production") and v1_mode != "publish":
            try:
                coverage_state = state_store.load_coverage()
                covered_ayahs = tuple(coverage_state.covered_ayahs)
                if authority == "b2":
                    bg_history = load_background_history(s3_client, config_b2)
                    all_used_pids: set[int] = set(recent_pexels_ids)
                    for p_tuple in bg_history.ayah_pexels_ids.values():
                        all_used_pids.update(p_tuple)
                    historical_pexels_ids = tuple(sorted(all_used_pids))
                else:
                    historical_pexels_ids = tuple(sorted(set(recent_pexels_ids)))
            except Exception as cov_load_err:
                return JobResult(
                    status="FAILED",
                    slot=request.slot,
                    date=berlin_date,
                    run_id=run_id,
                    failure_reason=f"Failed to load Quran coverage state: {cov_load_err}",
                )

        # Record STARTED status in active state store
        initial_record = RunRecord(
            run_id=run_id,
            date=berlin_date,
            slot=request.slot,
            status="STARTED",
            stage="STAGE_STARTED",
            started_at=utc_now_iso,
            run_kind=request.run_kind,
            schedule_slot_id=request.schedule_slot_id,
            translation_languages=stored_config.translation_languages,
        )
        try:
            state_store.start_run_record(initial_record)
        except Exception as err:
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                failure_reason=f"Failed to write initial STARTED record: {err}",
            )

        # Subtitle and visual style configuration from stored config
        render_cfg = build_render_config_from_stored_config(stored_config)

        # Build RunRequest for pure engine
        run_req = RunRequest(
            slot=request.slot,
            run_kind=request.run_kind,
            seed=request.seed,
            recent_ayah_ranges=recent_ranges,
            recent_pexels_video_ids=recent_pexels_ids,
            historical_pexels_video_ids=historical_pexels_ids,
            covered_ayahs=covered_ayahs,
            test_verse_key=request.test_verse_key,
            translation_languages=tuple(stored_config.translation_languages),
            output_path=None,
            background_clip_strategy=getattr(stored_config, "background_clip_strategy", "auto_prefer_single"),
            max_background_clips=getattr(stored_config, "max_background_clips", 3),
            target_background_clips=getattr(stored_config, "target_background_clips", 1),
            verified_background_records=verified_background_records,
            background_usage_events=background_usage_events,
            dry_run=request.dry_run,
            duration_config=DurationConfig(
                minimum_duration=stored_config.min_duration_seconds,
                maximum_duration=stored_config.max_duration_seconds,
            ),
            render_config=render_cfg,
            production_authorization=production_authorization,
        )
        if owner_v1_request is not None:
            run_req = replace(
                run_req,
                owner_ayah_key=owner_v1_request.owner_ayah_key,
                owner_pending_successor=owner_v1_request.owner_pending_successor,
                owner_reviewed_ayah_keys=owner_v1_request.owner_reviewed_ayah_keys,
                owner_reviewed_registry=owner_v1_request.owner_reviewed_registry,
                owner_reviewed_approval=owner_v1_request.owner_reviewed_approval,
                verse_limits=owner_v1_request.verse_limits,
                owner_cycle_number=owner_v1_request.owner_cycle_number,
                production_authorization=None,
                covered_ayahs=(),
                recent_ayah_ranges=(),
            )

        gen_fn = generate_reel_fn or generate_reel
        generation_started_at = time.monotonic()
        LOGGER.info(
            "[JOB] generation started run_id=%s run_kind=%s test_verse=%s",
            run_id,
            request.run_kind,
            request.test_verse_key or "auto",
        )
        try:
            if generate_reel_fn is None:
                def mark_pool_stale(record: Any, reason: str, actual_sha256: Optional[str]) -> None:
                    try:
                        pool_repository.save_stale(StaleBackgroundRecord(
                            provider=record.provider,
                            provider_asset_id=record.provider_asset_id,
                            provider_file_id=record.provider_file_id,
                            verified_record_id=record.record_id,
                            reason=reason,
                            detected_at=pool_utc_now_iso(),
                            expected_sha256=record.content_sha256,
                            actual_sha256=actual_sha256,
                        ))
                    except Exception as stale_err:
                        LOGGER.warning(
                            "[BACKGROUND] stale_metadata_write_failed asset=%s:%s error_type=%s",
                            record.provider,
                            record.provider_asset_id,
                            type(stale_err).__name__,
                        )

                def record_render_safety_failure(
                    provider: str,
                    provider_asset_id: str,
                    provider_variant_id: str,
                    content_sha256: str,
                    attempt_number: int,
                    start_offset_seconds: float,
                    verse_keys: Sequence[str],
                    render_config_fingerprint: str,
                    failure_reason: str,
                    detector_counts: Mapping[str, int],
                    background_plan: Optional[Sequence[Mapping[str, Any]]] = None,
                ) -> None:
                    try:
                        if authority == "supabase" and hasattr(state_store, "record_render_safety_failure"):
                            state_store.record_render_safety_failure(
                                provider=provider,
                                provider_asset_id=provider_asset_id,
                                provider_variant_id=provider_variant_id,
                                content_sha256=content_sha256,
                                run_id=run_id,
                                attempt_number=attempt_number,
                                start_offset_seconds=start_offset_seconds,
                                verse_keys=verse_keys,
                                render_config_fingerprint=render_config_fingerprint,
                                failure_reason=failure_reason,
                                detector_counts=detector_counts,
                                background_plan=background_plan,
                            )
                        elif hasattr(pool_repository, "record_render_safety_failure"):
                            pool_repository.record_render_safety_failure(
                                provider=provider,
                                provider_asset_id=provider_asset_id,
                                provider_variant_id=provider_variant_id,
                                content_sha256=content_sha256,
                                run_id=run_id,
                                attempt_number=attempt_number,
                                start_offset_seconds=start_offset_seconds,
                                verse_keys=verse_keys,
                                render_config_fingerprint=render_config_fingerprint,
                                failure_reason=failure_reason,
                                detector_counts=detector_counts,
                                background_plan=background_plan,
                            )
                    except Exception as tel_err:
                        LOGGER.warning("[TELEMETRY] render_safety_failure_record_failed error=%s", tel_err)

                gen_res = gen_fn(
                    run_req,
                    mark_pool_stale_fn=mark_pool_stale,
                    record_render_safety_failure_fn=record_render_safety_failure,
                )
            else:
                gen_res = gen_fn(run_req)
        except Exception as gen_err:
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            fail_rec = RunRecord(
                run_id=run_id,
                date=berlin_date,
                slot=request.slot,
                status="FAILED",
                stage="STAGE_GENERATION_EXCEPTION",
                started_at=utc_now_iso,
                run_kind=request.run_kind,
                schedule_slot_id=request.schedule_slot_id,
                completed_at=completed_at,
                failure_reason=f"Generation exception: {gen_err}",
                translation_languages=stored_config.translation_languages,
            )
            try:
                state_store.fail_run_record(fail_rec)
            except Exception:
                pass
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                failure_reason=f"Generation exception: {gen_err}",
            )

        if not gen_res.success or not gen_res.output_path:
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            fail_rec = RunRecord(
                run_id=run_id,
                date=berlin_date,
                slot=request.slot,
                status="FAILED",
                stage="STAGE_GENERATION_FAILED",
                started_at=utc_now_iso,
                run_kind=request.run_kind,
                schedule_slot_id=request.schedule_slot_id,
                completed_at=completed_at,
                failure_reason=gen_res.failure_reason or "Generation pipeline failed.",
                surah_number=gen_res.surah_number,
                first_ayah=gen_res.first_ayah,
                last_ayah=gen_res.last_ayah,
                duration_seconds=gen_res.duration_seconds,
                pexels_video_ids=gen_res.pexels_video_ids,
                translation_languages=stored_config.translation_languages,
            )
            try:
                state_store.fail_run_record(fail_rec)
            except Exception:
                pass
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                generation_result=gen_res,
                failure_reason=gen_res.failure_reason or "Generation pipeline failed.",
            )

        LOGGER.info(
            "[JOB] generation completed seconds=%.3f verse=%s:%s-%s",
            time.monotonic() - generation_started_at,
            gen_res.surah_number,
            gen_res.first_ayah,
            gen_res.last_ayah,
        )

        if request.run_kind in ("scheduled", "manual_production"):
            try:
                if owner_v1_publish:
                    if owner_v1_request is None:
                        raise EditorialPolicyError("OWNER_V1_GENERATED_AYAH_MISMATCH")
                    owner_data = (
                        gen_res.provenance.get("owner_v1")
                        if isinstance(gen_res.provenance, Mapping) else None
                    )
                    # Compare the ENTIRE generated range with the approved
                    # snapshot. Standalone/pair mode may choose one or two;
                    # reviewed 3+ mode must render every exact member.
                    rendered_keys = tuple(
                        f"{gen_res.surah_number}:{ayah}"
                        for ayah in range(gen_res.first_ayah, gen_res.last_ayah + 1)
                    ) if (type(gen_res.first_ayah) is int
                          and type(gen_res.last_ayah) is int) else ()
                    expected_keys = (
                        owner_v1_request.owner_reviewed_ayah_keys
                        if owner_v1_request.owner_reviewed_ayah_keys else (
                            (owner_v1_request.owner_ayah_key,)
                            if gen_res.first_ayah == gen_res.last_ayah else
                            (owner_v1_request.owner_ayah_key, owner_v1_request.owner_pending_successor)
                        )
                    )
                    if (not isinstance(owner_data, Mapping)
                        or owner_data.get("ayah_key") != owner_v1_request.owner_ayah_key
                        or owner_data.get("cycle_number") != owner_v1_request.owner_cycle_number
                        or owner_data.get("editorial_selection_only") is not True
                        or rendered_keys != expected_keys
                        or (len(rendered_keys) > 1
                            and owner_data.get("ayah_keys") != list(rendered_keys))):
                        raise EditorialPolicyError("OWNER_V1_GENERATED_AYAH_MISMATCH")
                    if owner_v1_request.owner_reviewed_approval is not None and (
                        owner_data.get("review_approval") != owner_v1_request.owner_reviewed_approval.snapshot()
                        or owner_data.get("review_approval_sha256") != owner_v1_request.owner_reviewed_approval.sha256
                    ):
                        raise EditorialPolicyError("OWNER_V1_GENERATED_REVIEW_APPROVAL_MISMATCH")
                else:
                    require_record_eligible(
                        gen_res,
                        production_authorization=production_authorization,
                    )
            except EditorialPolicyError as err:
                rejected = replace(initial_record, status="FAILED", stage="THEME_POLICY_REJECTED", failure_reason=str(err))
                state_store.save_run(rejected)
                return JobResult(status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id, failure_reason=str(err))

        # Upload rendered MP4 to Backblaze B2 (MediaStore)
        local_mp4_path = gen_res.output_path
        upload_started_at = time.monotonic()
        LOGGER.info("[JOB] media_upload started run_id=%s", run_id)
        try:
            media_key = upload_reel(
                client=s3_client,
                b2_config=config_b2,
                local_path=local_mp4_path,
                date=berlin_date,
                slot=request.slot,
                run_id=run_id,
                run_kind=request.run_kind,
            )
        except Exception as upload_err:
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            fail_rec = RunRecord(
                run_id=run_id,
                date=berlin_date,
                slot=request.slot,
                status="FAILED",
                stage="STAGE_UPLOAD",
                started_at=utc_now_iso,
                run_kind=request.run_kind,
                schedule_slot_id=request.schedule_slot_id,
                completed_at=completed_at,
                failure_reason=f"Failed to upload media to B2: {upload_err}",
                translation_languages=stored_config.translation_languages,
            )
            try:
                state_store.fail_run_record(fail_rec)
            except Exception:
                pass
            if os.path.exists(local_mp4_path):
                try:
                    os.remove(local_mp4_path)
                except OSError:
                    pass
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                generation_result=gen_res,
                failure_reason=f"Failed to upload media to B2: {upload_err}",
            )
        LOGGER.info(
            "[JOB] media_upload completed seconds=%.3f run_id=%s",
            time.monotonic() - upload_started_at,
            run_id,
        )

        # Clean local MP4 from runner
        if request.local_output is None:
            if os.path.exists(local_mp4_path):
                try:
                    os.remove(local_mp4_path)
                except OSError:
                    pass

        # Record SUCCESS generation status in active state store
        surah_tuple = (
            CANONICAL_SURAH_NAMES.get(gen_res.surah_number)
            if gen_res.surah_number is not None
            else None
        )
        surah_ar = surah_tuple[0] if surah_tuple else None
        surah_en = surah_tuple[1] if surah_tuple else None
        completed_gen_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        translation_sources = normalize_translation_sources(
            gen_res.provenance.get("translation_sources", {})
        )

        active_record = RunRecord(
            run_id=run_id,
            date=berlin_date,
            slot=request.slot,
            status="SUCCESS",
            stage="STAGE_GENERATION_SUCCESS",
            started_at=utc_now_iso,
            run_kind=request.run_kind,
            schedule_slot_id=request.schedule_slot_id,
            completed_at=completed_gen_at,
            surah_number=gen_res.surah_number,
            surah_name_ar=surah_ar,
            surah_name_en=surah_en,
            first_ayah=gen_res.first_ayah,
            last_ayah=gen_res.last_ayah,
            duration_seconds=gen_res.duration_seconds,
            pexels_video_ids=gen_res.pexels_video_ids,
            media_key=media_key,
            final_safety_passed=True,
            translation_languages=stored_config.translation_languages,
            translation_sources=translation_sources,
            provenance=gen_res.provenance,
        )

        try:
            state_store.complete_run_record(active_record)
        except Exception as err:
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                media_key=media_key,
                generation_result=gen_res,
                failure_reason=f"Failed to write SUCCESS record: {err}",
            )

        # Legacy generation coverage is not the new confirmed-posts counter.
        # Owner-v1 only advances through independently verified FB receipts.
        if request.run_kind in ("scheduled", "manual_production") and not owner_v1_publish:
            try:
                if authority == "supabase":
                    s_num = active_record.surah_number
                    f_ayah = active_record.first_ayah
                    l_ayah = active_record.last_ayah
                    if s_num is not None and f_ayah is not None and l_ayah is not None:
                        v_keys = [f"{s_num}:{a}" for a in range(f_ayah, l_ayah + 1)]
                        state_store.record_covered_ayahs(
                            active_record.run_id,
                            v_keys,
                            production_authorization=production_authorization,
                        )
                else:
                    update_coverage_for_run(
                        client=s3_client,
                        b2_config=config_b2,
                        run_record=active_record,
                        pexels_video_ids=active_record.pexels_video_ids,
                        now=now,
                    )
            except Exception as cov_save_err:
                return JobResult(
                    status="FAILED",
                    slot=request.slot,
                    date=berlin_date,
                    run_id=run_id,
                    media_key=media_key,
                    generation_result=gen_res,
                    failure_reason=f"Failed to persist Quran coverage checkpoint: {cov_save_err}",
                )
            try:
                _record_production_pool_usage(
                    pool_repository,
                    active_record,
                    used_at=completed_gen_at,
                )
            except Exception as usage_err:
                LOGGER.warning("[BACKGROUND] pool_usage_record_failed error=%s", usage_err)

    elif reused_generation and request.run_kind in ("scheduled", "manual_production") and active_record is not None:
        try:
            require_record_eligible(
                active_record,
                production_authorization=production_authorization,
            )
        except EditorialPolicyError as err:
            return JobResult(status="FAILED", slot=request.slot, date=berlin_date, run_id=active_record.run_id, failure_reason=str(err))
        # Reconcile coverage idempotently on resumed generation
        try:
            if authority == "supabase":
                s_num = active_record.surah_number
                f_ayah = active_record.first_ayah
                l_ayah = active_record.last_ayah
                if s_num is not None and f_ayah is not None and l_ayah is not None:
                    v_keys = [f"{s_num}:{a}" for a in range(f_ayah, l_ayah + 1)]
                    state_store.record_covered_ayahs(
                        active_record.run_id,
                        v_keys,
                        production_authorization=production_authorization,
                    )
            else:
                update_coverage_for_run(
                    client=s3_client,
                    b2_config=config_b2,
                    run_record=active_record,
                    pexels_video_ids=active_record.pexels_video_ids,
                    now=now,
                )
            _record_production_pool_usage(pool_repository, active_record, used_at=pool_utc_now_iso())
        except Exception as checkpoint_err:
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=run_id,
                media_key=active_record.media_key,
                failure_reason=f"Failed to reconcile production checkpoint: {checkpoint_err}",
            )

    # 8. Social Publishing Stage
    if request.run_kind in ("scheduled", "manual_production") and not owner_v1_publish:
        try:
            require_record_eligible(
                active_record,
                production_authorization=production_authorization,
            )
        except EditorialPolicyError as err:
            return JobResult(status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id, failure_reason=str(err))

    if active_record is None or not active_record.media_key:
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=berlin_date,
            run_id=run_id,
            failure_reason="Internal state error: active_record or media_key is missing.",
        )

    if owner_v1_publish:
        # Only the existing GitHub Actions worker may produce/send owner-v1.
        # Buffer is the only social API: submit to both channels, then read delivery.
        try:
            delivery_record, public_url = ensure_public_media_delivery(active_record, source_env)
            if delivery_record != active_record:
                state_store.save_run(delivery_record)
            buffer_client = BufferClient(load_buffer_config_from_env(source_env))
            sent = buffer_client.submit_owner_v1_pair(
                delivery_record, store=state_store,
                stored_config=stored_config, env=source_env,
            )
        except (BufferPublisherError, PublicMediaDeliveryError) as error:
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                media_key=active_record.media_key, generation_result=gen_res,
                publishing_blocked=True,
                failure_reason=f"OWNER_V1_BUFFER_DISPATCH_FAILED: {error.code}",
            )
        except Exception:
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=run_id,
                media_key=active_record.media_key, generation_result=gen_res,
                publishing_blocked=True,
                failure_reason="OWNER_V1_BUFFER_OUTCOME_UNKNOWN_RECONCILE",
            )
        # This helper can only verify the already-submitted post, never create one.
        from engine.facebook_delivery_verification import confirm_after_buffer_acceptance
        sent, warning = confirm_after_buffer_acceptance(
            sent, buffer=buffer_client, store=state_store,
            facebook_channel_id=buffer_client.config.facebook_channel_id,
            expected_public_media_url=public_url,
        )
        return JobResult(
            status="SUCCESS", slot=request.slot, date=berlin_date, run_id=run_id,
            media_key=sent.media_key, generation_result=gen_res,
            facebook_status=sent.facebook_status,
            instagram_status=sent.instagram_status,
            facebook_media_id=sent.facebook_media_id,
            instagram_media_id=sent.instagram_media_id,
            buffer_post_id=sent.buffer_post_id,
            facebook_reel_url=sent.facebook_reel_url,
            publishing_warning=warning,
        )

    # 8A. Tests are permanently non-public.  Buffer must be selected and
    # explicitly enabled; credentials alone never activate it. Direct Meta
    # publication is disabled for real workers; injected callbacks are legacy
    # offline test fixtures only.
    source_env = os.environ if env is None else env
    # An absent or misspelled backend is never permission to fall through to
    # legacy Meta publication. Any callback here is test-injected only.
    publisher_backend = source_env.get("PUBLISHER_BACKEND", "").strip().lower()
    target_fb = stored_config.facebook_enabled and request.publish_facebook
    target_ig = stored_config.instagram_enabled and request.publish_instagram

    if request.run_kind == "test" or (not target_fb and not target_ig):
        return JobResult(
            status="SUCCESS",
            slot=request.slot,
            date=berlin_date,
            run_id=active_record.run_id,
            media_key=active_record.media_key,
            generation_result=gen_res,
            facebook_status="NOT_ATTEMPTED",
            instagram_status="NOT_ATTEMPTED",
        )

    if publisher_backend not in {BUFFER_BACKEND, "meta"}:
        # A callback is an explicit test/dependency-injection opt-in for the
        # legacy publisher.  A real worker with an unset backend cannot
        # silently publish through Meta.
        if publish_reel_fn is None:
            disabled_record = replace(
                active_record, stage="STAGE_PUBLISHER_NOT_CONFIGURED", facebook_status="NOT_CONFIGURED",
                instagram_status="NOT_ATTEMPTED", facebook_failure_reason="No explicit publisher backend is configured.",
            )
            try:
                state_store.save_run(disabled_record)
            except Exception:
                pass
            return JobResult(status="SUCCESS", slot=request.slot, date=berlin_date, run_id=active_record.run_id,
                media_key=active_record.media_key, generation_result=gen_res, facebook_status="NOT_CONFIGURED",
                instagram_status="NOT_ATTEMPTED", facebook_failure_reason=disabled_record.facebook_failure_reason)
        publisher_backend = "meta"

    if publisher_backend == BUFFER_BACKEND:
        target_fb = target_fb and buffer_facebook_enabled(source_env)
        target_ig = target_ig and buffer_instagram_enabled(source_env)
        if not target_fb and not target_ig:
            disabled_record = replace(
                active_record,
                stage="STAGE_BUFFER_NOT_CONFIGURED",
                publisher_backend=BUFFER_BACKEND,
                facebook_status="NOT_CONFIGURED" if stored_config.facebook_enabled and request.publish_facebook else "NOT_ATTEMPTED",
                instagram_status="NOT_CONFIGURED" if stored_config.instagram_enabled and request.publish_instagram else "NOT_ATTEMPTED",
                facebook_failure_reason=(
                    "Buffer Facebook publishing is not explicitly enabled."
                    if stored_config.facebook_enabled and request.publish_facebook else None
                ),
                instagram_failure_reason=(
                    "Buffer Instagram publishing is not explicitly enabled."
                    if stored_config.instagram_enabled and request.publish_instagram else None
                ),
            )
            try:
                state_store.save_run(disabled_record)
            except Exception:
                pass
            return JobResult(
                status="SUCCESS",
                slot=request.slot,
                date=berlin_date,
                run_id=active_record.run_id,
                media_key=active_record.media_key,
                generation_result=gen_res,
                facebook_status=disabled_record.facebook_status,
                instagram_status=disabled_record.instagram_status,
                facebook_failure_reason=disabled_record.facebook_failure_reason,
                instagram_failure_reason=disabled_record.instagram_failure_reason,
            )

        rights_confirmed = check_reciter_reuse_rights(stored_config.reciter_reuse_confirmed, env=env)
        if not rights_confirmed:
            blocked_reason = "PUBLICATION_BLOCKED_RECITER_RIGHTS"
            blocked_record = replace(
                active_record,
                stage="STAGE_PUBLISHING_BLOCKED",
                publisher_backend=BUFFER_BACKEND,
                facebook_status="BLOCKED" if target_fb else "NOT_ATTEMPTED",
                instagram_status="BLOCKED" if target_ig else "NOT_ATTEMPTED",
                facebook_failure_reason=blocked_reason if target_fb else None,
                instagram_failure_reason=blocked_reason if target_ig else None,
            )
            try:
                state_store.save_run(blocked_record)
            except Exception:
                pass
            return JobResult(
                status="SUCCESS",
                slot=request.slot,
                date=berlin_date,
                run_id=active_record.run_id,
                media_key=active_record.media_key,
                generation_result=gen_res,
                facebook_status=blocked_record.facebook_status,
                instagram_status=blocked_record.instagram_status,
                facebook_failure_reason=blocked_record.facebook_failure_reason,
                instagram_failure_reason=blocked_record.instagram_failure_reason,
                publishing_blocked=True,
            )

        # A legacy B2 state store cannot atomically protect an external write.
        # Buffer publication is therefore Supabase-only and fails closed until
        # the durable claim migration has been applied.
        if authority != "supabase" or not hasattr(state_store, "claim_buffer_publication_dispatch"):
            return JobResult(
                status="FAILED", slot=request.slot, date=berlin_date, run_id=active_record.run_id,
                media_key=active_record.media_key, generation_result=gen_res,
                facebook_status="FAILED" if target_fb else "NOT_ATTEMPTED",
                instagram_status="FAILED" if target_ig else "NOT_ATTEMPTED",
                failure_reason="BUFFER_PUBLICATION_STATESTORE_REQUIRED",
            )

        caption = build_buffer_facebook_caption(active_record)
        buffer_failure_reason: Optional[str] = None
        try:
            delivery_record, public_media_url = ensure_public_media_delivery(active_record, source_env)
            if delivery_record is not active_record:
                state_store.save_run(delivery_record)
                active_record = delivery_record
            buffer_config = load_buffer_config_from_env(source_env)
            buffer_client = BufferClient(buffer_config)
        except (BufferPublisherError, PublicMediaDeliveryError) as buffer_err:
            buffer_failure_reason = f"{buffer_err.code}: Buffer publication preparation failed."
        except Exception:
            buffer_failure_reason = "BUFFER_PUBLISH_FAILED: Buffer publication preparation failed."

        if buffer_failure_reason is not None:
            buffer_record = replace(
                active_record,
                stage="STAGE_BUFFER_PUBLISH_FAILED",
                publisher_backend=BUFFER_BACKEND,
                facebook_status="FAILED" if target_fb else "NOT_ATTEMPTED",
                instagram_status="FAILED" if target_ig else "NOT_ATTEMPTED",
                facebook_failure_reason=buffer_failure_reason if target_fb else None,
                instagram_failure_reason=buffer_failure_reason if target_ig else None,
                failure_reason=buffer_failure_reason,
            )
            try:
                state_store.save_run(buffer_record)
            except Exception:
                pass
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=active_record.run_id,
                media_key=active_record.media_key,
                generation_result=gen_res,
                facebook_status=buffer_record.facebook_status,
                instagram_status=buffer_record.instagram_status,
                facebook_failure_reason=buffer_record.facebook_failure_reason,
                instagram_failure_reason=buffer_record.instagram_failure_reason,
                failure_reason=buffer_failure_reason,
            )

        media_identity = str(active_record.provenance.get("media_sha256") or active_record.media_key)
        targets_to_publish: list[tuple[str, BufferChannel]] = []
        if target_fb:
            try:
                organization, fb_channel = buffer_client.resolve_facebook_channel()
                targets_to_publish.append(("facebook", fb_channel))
            except Exception as ch_err:
                active_record = replace(
                    active_record,
                    facebook_status="FAILED",
                    facebook_failure_reason=f"{getattr(ch_err, 'code', type(ch_err).__name__)}: {getattr(ch_err, 'message', str(ch_err))}",
                )
        if target_ig:
            try:
                organization, ig_channel = buffer_client.resolve_instagram_channel()
                targets_to_publish.append(("instagram", ig_channel))
            except Exception as ch_err:
                active_record = replace(
                    active_record,
                    instagram_status="FAILED",
                    instagram_failure_reason=f"{getattr(ch_err, 'code', type(ch_err).__name__)}: {getattr(ch_err, 'message', str(ch_err))}",
                )

        for platform, channel in targets_to_publish:
            already_done = (
                (platform == "facebook" and active_record.facebook_status in ("SUCCESS", "ACCEPTED") and (active_record.buffer_post_id or active_record.facebook_media_id))
                or (platform == "instagram" and active_record.instagram_status in ("SUCCESS", "ACCEPTED") and active_record.instagram_media_id)
            )
            if already_done:
                continue

            fingerprint = publication_fingerprint(
                run_id=active_record.run_id, platform=platform, channel_id=channel.id,
                media_identity=media_identity, caption=caption,
            )
            source = publication_source(active_record.run_id, fingerprint)
            coordinator = BufferPublicationCoordinator(
                state_store, platform=platform, publisher_backend=BUFFER_BACKEND, channel_id=channel.id
            )
            try:
                claim = coordinator.claim(
                    run_id=active_record.run_id, fingerprint=fingerprint, source=source
                )
            except Exception:
                if platform == "facebook":
                    active_record = replace(active_record, facebook_status="FAILED", facebook_failure_reason="BUFFER_PUBLICATION_CLAIM_FAILED")
                else:
                    active_record = replace(active_record, instagram_status="FAILED", instagram_failure_reason="BUFFER_PUBLICATION_CLAIM_FAILED")
                continue

            claim_value = claim.get("value") if isinstance(claim.get("value"), Mapping) else claim
            claim_state = str(claim.get("state") or claim_value.get("state") or "")
            claim_was_created = bool(claim.get("_claim_created"))
            if claim_state == PUBLICATION_SUCCESS:
                post_id = str(claim.get("buffer_post_id") or claim_value.get("buffer_post_id") or "")
                if post_id:
                    if platform == "facebook":
                        active_record = replace(
                            active_record, facebook_status="ACCEPTED", facebook_media_id=post_id,
                            buffer_post_id=post_id, facebook_reel_url=claim.get("external_link") or claim_value.get("external_link"),
                            buffer_post_status=claim.get("buffer_post_status") or claim_value.get("buffer_post_status"),
                            buffer_accepted_at=claim.get("accepted_at") or claim_value.get("accepted_at"),
                            facebook_failure_reason=None,
                        )
                    else:
                        active_record = replace(
                            active_record, instagram_status="ACCEPTED", instagram_media_id=post_id,
                            instagram_failure_reason=None,
                        )
                    continue

            if not claim_was_created and claim_state in {PUBLICATION_DISPATCHING, PUBLICATION_UNKNOWN, "FAILED"}:
                reconciliation = buffer_client.reconcile_publication(
                    organization.id if 'organization' in locals() else channel.id,
                    channel.id, caption, public_media_url,
                    not_before=str(claim_value.get("created_at") or claim.get("created_at") or "") or None,
                )
                try:
                    recovery = coordinator.recover(claim_value, reconciliation)
                except Exception:
                    if platform == "facebook":
                        active_record = replace(active_record, facebook_status="UNKNOWN", facebook_failure_reason="BUFFER_PUBLICATION_RECONCILIATION_CHECKPOINT_FAILED")
                    else:
                        active_record = replace(active_record, instagram_status="UNKNOWN", instagram_failure_reason="BUFFER_PUBLICATION_RECONCILIATION_CHECKPOINT_FAILED")
                    continue
                if recovery == PUBLICATION_SUCCESS:
                    refreshed = state_store.get_buffer_publication_claim(active_record.run_id, platform, BUFFER_BACKEND, channel.id)
                    if refreshed:
                        claim, claim_value, claim_state = refreshed, (refreshed.get("value") or refreshed), str(refreshed.get("state"))
                        post_id = str(claim.get("buffer_post_id") or claim_value.get("buffer_post_id") or "")
                        if post_id:
                            if platform == "facebook":
                                active_record = replace(active_record, facebook_status="ACCEPTED", facebook_media_id=post_id, buffer_post_id=post_id, facebook_reel_url=claim.get("external_link") or claim_value.get("external_link"), buffer_post_status=claim.get("buffer_post_status") or claim_value.get("buffer_post_status"), buffer_accepted_at=claim.get("accepted_at") or claim_value.get("accepted_at"), facebook_failure_reason=None)
                            else:
                                active_record = replace(active_record, instagram_status="ACCEPTED", instagram_media_id=post_id, instagram_failure_reason=None)
                            continue
                elif recovery != PUBLICATION_DISPATCHING:
                    if platform == "facebook":
                        active_record = replace(active_record, facebook_status="UNKNOWN", facebook_failure_reason="BUFFER_PUBLISH_OUTCOME_UNKNOWN")
                    else:
                        active_record = replace(active_record, instagram_status="UNKNOWN", instagram_failure_reason="BUFFER_PUBLISH_OUTCOME_UNKNOWN")
                    continue

            try:
                if platform == "facebook" and publish_buffer_facebook_fn is not None:
                    pub_res = publish_buffer_facebook_fn(record=active_record, public_media_url=public_media_url, env=source_env)
                    if pub_res.facebook.status != "SUCCESS" or not pub_res.facebook.media_id:
                        raise BufferPublisherError("BUFFER_PUBLISH_FAILED", "Injected Buffer publisher did not confirm acceptance.")
                    post_id, external_link, buffer_post_status = pub_res.facebook.media_id, None, "accepted"
                else:
                    post = buffer_client.create_reel(channel.id, platform, BufferPublishRequest(
                        run_id=active_record.run_id, caption=caption, public_media_url=public_media_url, source=source, editorial_record=active_record
                    ))
                    post_id, external_link, buffer_post_status = post.id, post.external_link, post.status
            except BufferPublisherError as err:
                if err.possibly_dispatched:
                    coordinator.mark_unknown_outcome(active_record.run_id, err.code)
                    reason = f"BUFFER_PUBLISH_OUTCOME_UNKNOWN: {err.message}"
                    st = "UNKNOWN"
                else:
                    coordinator.mark_failed_before_dispatch(active_record.run_id, err.code)
                    reason = f"{err.code}: {err.message}"
                    st = "FAILED"
                if platform == "facebook":
                    active_record = replace(active_record, facebook_status=st, facebook_failure_reason=reason)
                else:
                    active_record = replace(active_record, instagram_status=st, instagram_failure_reason=reason)
                continue

            accepted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            try:
                coordinator.mark_success(
                    active_record.run_id, post_id=post_id, external_link=external_link,
                    accepted_at=accepted_at, buffer_post_status=buffer_post_status,
                )
            except Exception:
                if platform == "facebook":
                    active_record = replace(active_record, facebook_status="UNKNOWN", facebook_failure_reason="BUFFER_PUBLISH_SUCCESS_CHECKPOINT_PENDING")
                else:
                    active_record = replace(active_record, instagram_status="UNKNOWN", instagram_failure_reason="BUFFER_PUBLISH_SUCCESS_CHECKPOINT_PENDING")
                continue

            if platform == "facebook":
                active_record = replace(
                    active_record, stage="STAGE_BUFFER_PUBLISHING_ACCEPTED", publisher_backend=BUFFER_BACKEND,
                    facebook_status="ACCEPTED", facebook_media_id=post_id, buffer_post_id=post_id,
                    buffer_post_status=buffer_post_status, buffer_accepted_at=accepted_at,
                    facebook_reel_url=external_link, facebook_failure_reason=None,
                )
            else:
                active_record = replace(
                    active_record, stage="STAGE_BUFFER_PUBLISHING_ACCEPTED", publisher_backend=BUFFER_BACKEND,
                    instagram_status="ACCEPTED", instagram_media_id=post_id, instagram_failure_reason=None,
                )

        # Determine overall success / failure / warning according to Facebook-primary invariants:
        fb_st = active_record.facebook_status
        ig_st = active_record.instagram_status
        overall_status = "SUCCESS"
        overall_failure_reason: Optional[str] = None
        publishing_warning: Optional[str] = None

        if target_fb:
            if fb_st in ("SUCCESS", "ACCEPTED"):
                overall_status = "SUCCESS"
                if ig_st == "FAILED":
                    publishing_warning = f"Instagram publishing failed: {active_record.instagram_failure_reason or 'unknown error'}"
                elif ig_st == "UNKNOWN":
                    publishing_warning = "Instagram publishing status is UNKNOWN — manual verification in Buffer is recommended."
                elif ig_st == "NOT_CONFIGURED":
                    publishing_warning = "Instagram is not configured in Buffer."
            else:
                overall_status = "FAILED"
                overall_failure_reason = f"Primary publishing destination (Facebook) {fb_st}: {active_record.facebook_failure_reason or 'unknown error'}"
        else:
            # Facebook not targeted / explicitly disabled
            if target_ig:
                if ig_st in ("SUCCESS", "ACCEPTED"):
                    overall_status = "SUCCESS"
                else:
                    overall_status = "FAILED"
                    overall_failure_reason = f"Secondary publishing destination (Instagram) {ig_st}: {active_record.instagram_failure_reason or 'unknown error'}"
            else:
                overall_status = "SUCCESS"

        active_record = replace(
            active_record,
            status=overall_status,
            stage="STAGE_BUFFER_PUBLISHING_ACCEPTED" if overall_status == "SUCCESS" else "STAGE_BUFFER_PUBLISHING_FAILED",
            failure_reason=overall_failure_reason,
        )
        try:
            state_store.save_run(active_record)
        except Exception:
            pass

        return JobResult(
            status=overall_status,
            slot=request.slot,
            date=berlin_date,
            run_id=active_record.run_id,
            media_key=active_record.media_key,
            generation_result=gen_res,
            facebook_status=active_record.facebook_status,
            instagram_status=active_record.instagram_status,
            facebook_media_id=active_record.facebook_media_id,
            buffer_post_id=active_record.buffer_post_id,
            facebook_reel_url=active_record.facebook_reel_url,
            facebook_failure_reason=active_record.facebook_failure_reason,
            instagram_failure_reason=active_record.instagram_failure_reason,
            publishing_warning=publishing_warning,
            failure_reason=overall_failure_reason,
        )

    # 8B. Rights Confirmation Check
    rights_confirmed = check_reciter_reuse_rights(stored_config.reciter_reuse_confirmed, env=env)
    if not rights_confirmed:
        blocked_reason = (
            "Publishing is BLOCKED: Reciter reuse rights are not confirmed. "
            "Both StoredConfig.reciter_reuse_confirmed and RECITER_REUSE_CONFIRMED must be true."
        )
        blocked_record = replace(
            active_record,
            stage="STAGE_PUBLISHING_BLOCKED",
            facebook_status="BLOCKED" if target_fb else "NOT_ATTEMPTED",
            instagram_status="BLOCKED" if target_ig else "NOT_ATTEMPTED",
            facebook_failure_reason=blocked_reason if target_fb else None,
            instagram_failure_reason=blocked_reason if target_ig else None,
        )
        try:
            state_store.save_run(blocked_record)
        except Exception:
            pass

        return JobResult(
            status="SUCCESS",
            slot=request.slot,
            date=berlin_date,
            run_id=active_record.run_id,
            media_key=active_record.media_key,
            generation_result=gen_res,
            facebook_status="BLOCKED" if target_fb else "NOT_ATTEMPTED",
            instagram_status="BLOCKED" if target_ig else "NOT_ATTEMPTED",
            facebook_failure_reason=blocked_reason if target_fb else None,
            instagram_failure_reason=blocked_reason if target_ig else None,
            publishing_blocked=True,
        )

    # 8C. Load Meta Configuration
    try:
        encryption_key = env.get("META_CREDENTIAL_ENCRYPTION_KEY", "").strip() if env else ""
        if encryption_key:
            if authority == "supabase":
                from engine.meta_credentials import load_meta_config_from_envelope
                envelope = state_store.load_encrypted_meta_credentials()
                if not envelope:
                    raise PublishError("Encrypted Meta credentials not found in Supabase.")
                meta_cfg = load_meta_config_from_envelope(
                    envelope,
                    encryption_key,
                    api_version_override=env.get("META_API_VERSION", "").strip() if env else None,
                )
            else:
                meta_cfg = load_meta_config_from_b2(
                    s3_client,
                    config_b2,
                    encryption_key,
                    api_version_override=env.get("META_API_VERSION", "").strip() if env else None,
                )
        else:
            # Backward-compatible fallback for deployments not yet migrated to
            # Dashboard-managed encrypted credentials.
            meta_cfg = load_meta_config_from_env(env)
    except Exception as meta_err:
        meta_err_record = replace(
            active_record,
            stage="STAGE_META_CONFIG_ERROR",
            facebook_status="FAILED" if target_fb else "NOT_ATTEMPTED",
            instagram_status="FAILED" if target_ig else "NOT_ATTEMPTED",
            facebook_failure_reason=f"Meta configuration error: {meta_err}" if target_fb else None,
            instagram_failure_reason=f"Meta configuration error: {meta_err}" if target_ig else None,
            failure_reason=f"Meta configuration error: {meta_err}",
        )
        try:
            state_store.save_run(meta_err_record)
        except Exception:
            pass

        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=berlin_date,
            run_id=active_record.run_id,
            media_key=active_record.media_key,
            generation_result=gen_res,
            facebook_status="FAILED" if target_fb else "NOT_ATTEMPTED",
            instagram_status="FAILED" if target_ig else "NOT_ATTEMPTED",
            facebook_failure_reason=f"Meta configuration error: {meta_err}" if target_fb else None,
            instagram_failure_reason=f"Meta configuration error: {meta_err}" if target_ig else None,
            failure_reason=f"Meta configuration error: {meta_err}",
        )

    # 8D. Build Caption and Request
    caption = build_reel_caption_from_record(active_record)
    pub_req = PublishRequest(
        editorial_record=active_record,
        media_key=active_record.media_key,
        caption=caption,
        facebook_enabled=target_fb,
        instagram_enabled=target_ig,
        existing_facebook_status=active_record.facebook_status,
        existing_instagram_status=active_record.instagram_status,
        existing_facebook_media_id=active_record.facebook_media_id,
        existing_instagram_media_id=active_record.instagram_media_id,
    )

    pub_fn = publish_reel_fn or publish_reel

    # Checkpoint callback handler for per-platform immediate durable persistence
    def handle_checkpoint(platform: str, p_res: PlatformPublishResult) -> None:
        nonlocal active_record
        if platform == "facebook":
            active_record = replace(
                active_record,
                facebook_status=p_res.status,
                facebook_media_id=p_res.media_id,
                facebook_failure_reason=p_res.failure_reason,
                stage="STAGE_FACEBOOK_CHECKPOINT",
            )
        elif platform == "instagram":
            active_record = replace(
                active_record,
                instagram_status=p_res.status,
                instagram_media_id=p_res.media_id,
                instagram_failure_reason=p_res.failure_reason,
                stage="STAGE_INSTAGRAM_CHECKPOINT",
            )
        # Immediate durable checkpoint to active state store
        state_store.save_run(active_record)

    try:
        try:
            pub_res = pub_fn(
                request=pub_req,
                meta_config=meta_cfg,
                stored_config=stored_config,
                s3_client=s3_client,
                b2_config=config_b2,
                env=env,
                on_checkpoint=handle_checkpoint,
            )
        except TypeError as te:
            if "on_checkpoint" in str(te) or "b2_config" in str(te):
                pub_res = pub_fn(
                    request=pub_req,
                    meta_config=meta_cfg,
                    stored_config=stored_config,
                    s3_client=s3_client,
                    b2_config=config_b2,
                    env=env,
                )
            else:
                raise
    except Exception as checkpoint_err:
        return JobResult(
            status="FAILED",
            slot=request.slot,
            date=berlin_date,
            run_id=active_record.run_id,
            media_key=active_record.media_key,
            generation_result=gen_res,
            failure_reason=f"Durable state checkpoint write failed: {checkpoint_err}",
        )

    # 8E. Persist Final Publishing Results to active state store
    final_fb_status = pub_res.facebook.status
    final_ig_status = pub_res.instagram.status
    final_fb_media_id = pub_res.facebook.media_id or active_record.facebook_media_id
    final_ig_media_id = pub_res.instagram.media_id or active_record.instagram_media_id

    published_record = replace(
        active_record,
        stage="STAGE_PUBLISHING_COMPLETE",
        facebook_status=final_fb_status,
        instagram_status=final_ig_status,
        facebook_media_id=final_fb_media_id,
        instagram_media_id=final_ig_media_id,
        facebook_failure_reason=pub_res.facebook.failure_reason,
        instagram_failure_reason=pub_res.instagram.failure_reason,
    )

    try:
        state_store.save_run(published_record)
    except Exception:
        pass

    # Success / Warning / Failure Evaluation:
    # Rule 6A: Facebook is PRIMARY, Instagram is OPTIONAL
    if stored_config.facebook_enabled:
        if final_fb_status == "SUCCESS":
            # Primary succeeded! Determine if secondary (Instagram) has a warning
            warning_msg: Optional[str] = None
            if final_ig_status == "FAILED":
                warning_msg = f"Instagram publishing failed: {pub_res.instagram.failure_reason or 'unknown error'}"
            elif final_ig_status == "NOT_CONFIGURED":
                warning_msg = "Instagram is not configured (META_IG_USER_ID is not set)."
            elif final_ig_status == "UNKNOWN":
                warning_msg = "Instagram publishing status is UNKNOWN — manual verification in Meta Business Suite is recommended."

            return JobResult(
                status="SUCCESS",
                slot=request.slot,
                date=berlin_date,
                run_id=active_record.run_id,
                media_key=active_record.media_key,
                generation_result=gen_res,
                facebook_status=final_fb_status,
                instagram_status=final_ig_status,
                facebook_media_id=final_fb_media_id,
                instagram_media_id=final_ig_media_id,
                facebook_failure_reason=pub_res.facebook.failure_reason,
                instagram_failure_reason=pub_res.instagram.failure_reason,
                publishing_warning=warning_msg,
            )
        else:
            # Primary destination failed!
            return JobResult(
                status="FAILED",
                slot=request.slot,
                date=berlin_date,
                run_id=active_record.run_id,
                media_key=active_record.media_key,
                generation_result=gen_res,
                facebook_status=final_fb_status,
                instagram_status=final_ig_status,
                facebook_media_id=final_fb_media_id,
                instagram_media_id=final_ig_media_id,
                facebook_failure_reason=pub_res.facebook.failure_reason,
                instagram_failure_reason=pub_res.instagram.failure_reason,
                failure_reason=f"Primary publishing destination (Facebook) failed: {pub_res.facebook.failure_reason or 'unknown error'}",
            )
    else:
        # Facebook is explicitly disabled in stored configuration
        if stored_config.instagram_enabled:
            if final_ig_status == "SUCCESS":
                return JobResult(
                    status="SUCCESS",
                    slot=request.slot,
                    date=berlin_date,
                    run_id=active_record.run_id,
                    media_key=active_record.media_key,
                    generation_result=gen_res,
                    facebook_status=final_fb_status,
                    instagram_status=final_ig_status,
                    facebook_media_id=final_fb_media_id,
                    instagram_media_id=final_ig_media_id,
                    facebook_failure_reason=pub_res.facebook.failure_reason,
                    instagram_failure_reason=pub_res.instagram.failure_reason,
                )
            else:
                return JobResult(
                    status="FAILED",
                    slot=request.slot,
                    date=berlin_date,
                    run_id=active_record.run_id,
                    media_key=active_record.media_key,
                    generation_result=gen_res,
                    facebook_status=final_fb_status,
                    instagram_status=final_ig_status,
                    facebook_media_id=final_fb_media_id,
                    instagram_media_id=final_ig_media_id,
                    facebook_failure_reason=pub_res.facebook.failure_reason,
                    instagram_failure_reason=pub_res.instagram.failure_reason,
                    failure_reason=f"Instagram publishing failed: {pub_res.instagram.failure_reason or 'unknown error'}",
                )
        else:
            # Both disabled
            return JobResult(
                status="SUCCESS",
                slot=request.slot,
                date=berlin_date,
                run_id=active_record.run_id,
                media_key=active_record.media_key,
                generation_result=gen_res,
                facebook_status=final_fb_status,
                instagram_status=final_ig_status,
                facebook_media_id=final_fb_media_id,
                instagram_media_id=final_ig_media_id,
                facebook_failure_reason=pub_res.facebook.failure_reason,
                instagram_failure_reason=pub_res.instagram.failure_reason,
            )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for running the production or test Reel generation and publishing job."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description="Quran Reel Generation & Publishing Worker")
    parser.add_argument(
        "--slot",
        required=False,
        help="Legacy slot alias or stable scheduled slot ID",
    )
    parser.add_argument(
        "--run-kind",
        choices=["scheduled", "manual_production", "test"],
        required=False,
        help="Explicit run semantics",
    )
    parser.add_argument(
        "--schedule-slot-id",
        required=False,
        help="Stable ID for a scheduled run",
    )
    parser.add_argument(
        "--run-id",
        required=False,
        help="Caller-supplied idempotency ID for test/manual production",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Legacy test-only compatibility flag; production idempotency is never bypassed",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Deterministic random seed",
    )
    parser.add_argument(
        "--local-output",
        type=str,
        default=None,
        help="Optional local output file path for test runs",
    )
    parser.add_argument(
        "--test-verse-key",
        choices=list(TEST_VERIFIED_VERSE_KEYS),
        required=False,
        help="Optional test-only reviewed phrase fixture (currently 2:21)",
    )
    parser.add_argument("--publish-facebook", action=argparse.BooleanOptionalAction, default=True, help="Manual production target: Facebook")
    parser.add_argument("--publish-instagram", action=argparse.BooleanOptionalAction, default=True, help="Manual production target: Instagram")

    args = parser.parse_args(argv)

    if args.run_kind is None:
        if args.slot is None:
            parser.error("either --slot or --run-kind is required")
        resolved_slot = args.slot
        resolved_kind = None
        resolved_schedule_slot = None
    elif args.run_kind == "scheduled":
        resolved_schedule_slot = args.schedule_slot_id or args.slot
        if not resolved_schedule_slot:
            parser.error("--schedule-slot-id is required for scheduled runs")
        resolved_slot = resolved_schedule_slot
        resolved_kind = "scheduled"
    elif args.run_kind == "manual_production":
        resolved_slot = "manual-production"
        resolved_kind = "manual_production"
        resolved_schedule_slot = None
    else:
        resolved_slot = "test"
        resolved_kind = "test"
        resolved_schedule_slot = None

    req = JobRequest(
        slot=resolved_slot,
        run_kind=resolved_kind,
        schedule_slot_id=resolved_schedule_slot,
        requested_run_id=args.run_id,
        force=args.force,
        seed=args.seed,
        local_output=args.local_output,
        test_verse_key=args.test_verse_key,
        publish_facebook=args.publish_facebook,
        publish_instagram=args.publish_instagram,
    )

    result = run_job(req)

    if result.status == "SUCCESS":
        if result.publishing_blocked:
            print(
                f"[OK] Reel generation and upload succeeded (slot={result.slot}, date={result.date}, "
                f"run_id={result.run_id}, media_key={result.media_key}). "
                f"Social publishing is BLOCKED pending reciter rights confirmation."
            )
        elif result.publishing_warning:
            print(
                f"[OK with WARNING] Reel job succeeded (slot={result.slot}, date={result.date}, "
                f"run_id={result.run_id}, media_key={result.media_key}, "
                f"FB={result.facebook_status}, IG={result.instagram_status}). "
                f"WARNING: {result.publishing_warning}"
            )
        else:
            print(
                f"[OK] Reel job succeeded (slot={result.slot}, date={result.date}, "
                f"run_id={result.run_id}, media_key={result.media_key}, "
                f"FB={result.facebook_status}, IG={result.instagram_status})"
            )
        return 0
    elif result.status == "SKIPPED":
        print(
            f"[SKIPPED] Reel job skipped (slot={result.slot}, date={result.date}, "
            f"reason={result.skipped_reason})"
        )
        return 0
    else:
        print(
            f"[FAILED] Reel job failed (slot={result.slot}, date={result.date}, "
            f"error={result.failure_reason})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    # The executable worker entrypoint is GitHub Actions only. This avoids
    # accidental local video generation and upload, including test-mode jobs.
    # GITHUB_ACTIONS is an operational reminder, not a security credential;
    # Supabase/Buffer privileges and production gates remain authoritative.
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        print("[FAILED] REEL_WORKER_GITHUB_ACTIONS_ONLY", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
