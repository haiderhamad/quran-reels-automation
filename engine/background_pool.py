"""Durable verified-background metadata pool backed by Backblaze B2.

The pool stores JSON evidence only. Source video bytes are always temporary and
must never be written below the background_pool prefix.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from engine.backgrounds import (
    BackgroundSearchConfig,
    BackgroundDiscoveryPage,
    BackgroundCandidate,
    fetch_pixabay_video_asset,
    fetch_pexels_video_asset,
    resolve_pixabay_video_variant,
    resolve_pexels_video_variant,
    search_pixabay_background_candidates,
    search_background_candidates,
)
from engine.safety import (
    ANIMAL_DETECTION_CLASSES,
    ANIMAL_DETECTOR_ID,
    ANIMAL_POLICY_VERSION,
    DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD,
    DEFAULT_ANIMAL_MODEL_FILENAME,
    DEFAULT_ANIMAL_NMS_THRESHOLD,
    DEFAULT_FACE_CONFIDENCE_THRESHOLD,
    DEFAULT_FACE_MODEL_FILENAME,
    DEFAULT_PERSON_CONFIDENCE_THRESHOLD,
    DEFAULT_PERSON_MODEL_FILENAME,
    DEFAULT_SAMPLE_FPS,
    DEFAULT_CACHE_DIR,
)
from engine.state_authority import (
    STATE_AUTHORITY_B2,
    STATE_AUTHORITY_SUPABASE,
    get_state_authority,
)
from engine.storage import B2Config, StorageError, create_b2_client, load_b2_config_from_env
from engine.supabase_state import SupabaseConfig, SupabaseStateStore


BACKGROUND_POOL_SCHEMA_VERSION = 2
BACKGROUND_POOL_DATA_VERSION = "v2"
BACKGROUND_POOL_PREFIX = f"background_pool/{BACKGROUND_POOL_DATA_VERSION}/"
LEGACY_BACKGROUND_POOL_PREFIX = "background_pool/v1/"
SAFETY_SCANNER_VERSION = "opencv-zoo-person-face-animal-v2"
# v3 binds the corrected MP-PersonDet float normalization and zero letterbox.
SAFETY_PREPROCESSING_VERSION = "native-frame-sequential-sampling-v3"
VERIFICATION_TOOL_VERSION = "verified-background-pool-v2"
PERSON_MODEL_SHA256 = "47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f"
FACE_MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
ANIMAL_MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
ANIMAL_POLICY = "explicit_detector_evidence_required"
VALID_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VALID_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi")
VALID_VERIFIED_PROVIDERS = frozenset(("pexels", "pixabay"))


class BackgroundPoolError(ValueError):
    """Raised when pool metadata or persistence fails closed."""


@dataclass(frozen=True)
class BackgroundPoolReconciliation:
    """One authoritative pool snapshot used to initialize a builder run."""

    stats: Mapping[str, Any]
    verified_records: tuple["VerifiedBackgroundRecord", ...]
    stale_record_ids: frozenset[str]
    safety_rejected_identities: frozenset[tuple[str, str, str]]
    metadata_ineligible_identities: frozenset[tuple[str, str, str]]


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalized_safety_policy(
    *,
    animal_detector: str,
    animal_classes: Sequence[str],
    animal_threshold: float,
) -> dict[str, Any]:
    """Return the exact policy inputs that determine source-verification trust."""
    return {
        "scanner_version": SAFETY_SCANNER_VERSION,
        "preprocessing_version": SAFETY_PREPROCESSING_VERSION,
        "person_detector": {
            "model": DEFAULT_PERSON_MODEL_FILENAME,
            "sha256": PERSON_MODEL_SHA256,
        },
        "face_detector": {
            "model": DEFAULT_FACE_MODEL_FILENAME,
            "sha256": FACE_MODEL_SHA256,
        },
        "person_threshold": DEFAULT_PERSON_CONFIDENCE_THRESHOLD,
        "face_threshold": DEFAULT_FACE_CONFIDENCE_THRESHOLD,
        "sampling_fps": DEFAULT_SAMPLE_FPS,
        "max_frames": None,
        "first_and_last_frame_required": True,
        "full_source_scan_required": True,
        "fail_closed": True,
        "animal_policy": {
            "mode": "REQUIRED",
            "detector": str(animal_detector),
            "model": DEFAULT_ANIMAL_MODEL_FILENAME,
            "sha256": ANIMAL_MODEL_SHA256,
            "policy_version": ANIMAL_POLICY_VERSION,
            "preprocessing": "rgb-top-left-letterbox-640-v1",
            "classes": sorted({str(item).strip().lower() for item in animal_classes if str(item).strip()}),
            "threshold": float(animal_threshold),
            "nms_threshold": DEFAULT_ANIMAL_NMS_THRESHOLD,
            "verdict_required": "CLEAR",
        },
    }


def compute_safety_policy_fingerprint(policy: Mapping[str, Any]) -> str:
    payload = dict(policy)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_eligibility_fingerprint(policy: Mapping[str, Any]) -> str:
    """Hash non-safety metadata eligibility independently from detector policy."""
    encoded = json.dumps(
        dict(policy), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_safety_policy() -> dict[str, Any]:
    """Build the V2 policy from the installed safety module without assuming optional names."""
    from engine import safety as safety_module

    detector = str(getattr(safety_module, "ANIMAL_DETECTOR_ID", "")).strip()
    classes = tuple(getattr(safety_module, "ANIMAL_DETECTION_CLASSES", ()))
    threshold = getattr(safety_module, "DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD", None)
    if not detector or not classes or threshold is None:
        # The model layer remains importable while the scanner is being upgraded,
        # but no record can be constructed as verified without explicit animal
        # evidence. This sentinel policy is intentionally never sufficient for
        # build_verified_record().
        detector = detector or "UNAVAILABLE"
        classes = classes or ("UNAVAILABLE",)
        threshold = float(threshold) if threshold is not None else 1.0
    return normalized_safety_policy(
        animal_detector=detector,
        animal_classes=classes,
        animal_threshold=float(threshold),
    )


CURRENT_SAFETY_POLICY = current_safety_policy()
CURRENT_SAFETY_POLICY_FINGERPRINT = compute_safety_policy_fingerprint(CURRENT_SAFETY_POLICY)


def _validate_provider(provider: str) -> str:
    cleaned = str(provider).strip().lower()
    if not VALID_PROVIDER_RE.fullmatch(cleaned):
        raise BackgroundPoolError("provider must be a safe lowercase identifier.")
    return cleaned


def _validate_identity(value: str | int, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise BackgroundPoolError(f"{field_name} must be a safe non-empty identifier.")
    return cleaned


def _validate_sha256(value: str, field_name: str = "content_sha256") -> str:
    cleaned = str(value).strip().lower()
    if not VALID_SHA256_RE.fullmatch(cleaned):
        raise BackgroundPoolError(f"{field_name} must be a 64-character lowercase SHA-256 digest.")
    return cleaned


@dataclass(frozen=True)
class SafetyEvidence:
    scanner_version: str
    policy_fingerprint: str
    person_threshold: float
    face_threshold: float
    sampling_fps: float
    person_result: str
    face_result: str
    animal_detector: str
    animal_classes: Sequence[str]
    animal_threshold: float
    animal_verdict: str
    scanned_frames: int
    full_source_scan: bool
    verdict: str

    def __post_init__(self) -> None:
        if not self.scanner_version:
            raise BackgroundPoolError("Safety evidence scanner_version is required.")
        _validate_sha256(self.policy_fingerprint, "policy_fingerprint")
        if (
            self.person_threshold <= 0
            or self.face_threshold <= 0
            or self.sampling_fps <= 0
            or self.animal_threshold <= 0
        ):
            raise BackgroundPoolError("Safety evidence thresholds and sampling FPS must be positive.")
        if self.scanned_frames < 1:
            raise BackgroundPoolError("Safety evidence must include at least one scanned frame.")
        if self.full_source_scan is not True:
            raise BackgroundPoolError("Verified safety evidence requires a complete source scan.")
        if self.verdict not in ("SAFE", "REJECT"):
            raise BackgroundPoolError("Safety evidence verdict must be SAFE or REJECT.")
        object.__setattr__(
            self,
            "animal_classes",
            tuple(sorted({str(item).strip().lower() for item in self.animal_classes if str(item).strip()})),
        )
        if not self.animal_detector or not self.animal_classes:
            raise BackgroundPoolError("Animal detector and evaluated classes are required.")
        if self.animal_verdict not in ("CLEAR", "REJECT"):
            raise BackgroundPoolError("Animal verdict must be CLEAR or REJECT.")


@dataclass(frozen=True)
class VerifiedBackgroundRecord:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    source_page_url: str
    width: int
    height: int
    duration_seconds: float
    fps: Optional[float]
    mime_type: str
    declared_file_size: Optional[int]
    downloaded_file_size: int
    content_sha256: str
    verified_at: str
    verification_tool_version: str
    safety_policy: Mapping[str, Any]
    safety: SafetyEvidence
    query: str = ""
    status: str = "verified"
    active: bool = True
    render_robustness: Optional[Mapping[str, Any]] = None
    operational_state: Optional[Mapping[str, Any]] = None
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION:
            raise BackgroundPoolError("Unsupported verified background schema_version.")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        if self.provider not in VALID_VERIFIED_PROVIDERS:
            raise BackgroundPoolError("V2 verified records support pexels and pixabay only.")
        object.__setattr__(self, "provider_asset_id", _validate_identity(self.provider_asset_id, "provider_asset_id"))
        object.__setattr__(self, "provider_variant_id", _validate_identity(self.provider_variant_id, "provider_variant_id"))
        object.__setattr__(self, "content_sha256", _validate_sha256(self.content_sha256))
        if self.status != "verified" or not isinstance(self.active, bool):
            raise BackgroundPoolError("Verified record status/active fields are invalid.")
        if self.width < 1080 or self.height < 1920:
            raise BackgroundPoolError("Verified source cannot provide a native 1080x1920 crop.")
        if self.duration_seconds <= 0 or self.downloaded_file_size <= 0:
            raise BackgroundPoolError("Verified source duration and file size must be positive.")
        if self.declared_file_size is not None and self.declared_file_size <= 0:
            raise BackgroundPoolError("declared_file_size must be positive when present.")
        if self.mime_type.lower() != "video/mp4":
            raise BackgroundPoolError("V2 verified pool accepts MP4 sources only.")
        if self.verification_tool_version != VERIFICATION_TOOL_VERSION:
            raise BackgroundPoolError("Unknown verification tool version.")
        if compute_safety_policy_fingerprint(self.safety_policy) != self.safety.policy_fingerprint:
            raise BackgroundPoolError("Stored safety policy does not match its fingerprint.")
        animal_policy = self.safety_policy.get("animal_policy", {})
        if not isinstance(animal_policy, Mapping):
            raise BackgroundPoolError("Stored animal safety policy is invalid.")
        if (
            self.safety.scanner_version != self.safety_policy.get("scanner_version")
            or self.safety.person_threshold != self.safety_policy.get("person_threshold")
            or self.safety.face_threshold != self.safety_policy.get("face_threshold")
            or self.safety.sampling_fps != self.safety_policy.get("sampling_fps")
            or self.safety.animal_detector != animal_policy.get("detector")
            or tuple(self.safety.animal_classes) != tuple(animal_policy.get("classes", ()))
            or self.safety.animal_threshold != animal_policy.get("threshold")
        ):
            raise BackgroundPoolError("Stored safety evidence does not match its policy inputs.")
        if (
            self.safety.verdict != "SAFE"
            or self.safety.person_result != "CLEAR"
            or self.safety.face_result != "CLEAR"
            or self.safety.animal_verdict != "CLEAR"
        ):
            raise BackgroundPoolError("Verified records require complete CLEAR person/face/animal evidence.")

    @property
    def provider_file_id(self) -> str:
        """Compatibility alias for call sites migrating from V1 terminology."""
        return self.provider_variant_id

    @property
    def record_id(self) -> str:
        return (
            f"{self.provider}:{self.provider_asset_id}:{self.provider_variant_id}:"
            f"{self.content_sha256}:{self.safety.policy_fingerprint}"
        )

    @property
    def is_current_policy(self) -> bool:
        return self.active and self.safety.policy_fingerprint == CURRENT_SAFETY_POLICY_FINGERPRINT


@dataclass(frozen=True)
class SafetyRejectedRecord:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    policy_fingerprint: str
    reason: str
    rejected_at: str
    query: str = ""
    status: str = "rejected"
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION or self.status != "rejected":
            raise BackgroundPoolError("Invalid safety-rejected record schema/status.")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        object.__setattr__(self, "provider_asset_id", _validate_identity(self.provider_asset_id, "provider_asset_id"))
        object.__setattr__(self, "provider_variant_id", _validate_identity(self.provider_variant_id, "provider_variant_id"))
        object.__setattr__(self, "policy_fingerprint", _validate_sha256(self.policy_fingerprint, "policy_fingerprint"))
        if not self.reason:
            raise BackgroundPoolError("Safety-rejected record requires a reason.")

    @property
    def provider_file_id(self) -> str:
        return self.provider_variant_id


@dataclass(frozen=True)
class MetadataIneligibleRecord:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    eligibility_fingerprint: str
    reason: str
    recorded_at: str
    query: str = ""
    status: str = "metadata_ineligible"
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION or self.status != "metadata_ineligible":
            raise BackgroundPoolError("Invalid metadata-ineligible record schema/status.")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        object.__setattr__(self, "provider_asset_id", _validate_identity(self.provider_asset_id, "provider_asset_id"))
        object.__setattr__(self, "provider_variant_id", _validate_identity(self.provider_variant_id, "provider_variant_id"))
        object.__setattr__(self, "eligibility_fingerprint", _validate_sha256(self.eligibility_fingerprint, "eligibility_fingerprint"))
        if not self.reason:
            raise BackgroundPoolError("Metadata-ineligible record requires a reason.")

    @property
    def provider_file_id(self) -> str:
        return self.provider_variant_id


@dataclass(frozen=True)
class TransientFailureRecord:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    reason: str
    failed_at: str
    retryable: bool = True
    status: str = "transient_failure"
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION or self.status != "transient_failure":
            raise BackgroundPoolError("Invalid transient failure record schema/status.")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        object.__setattr__(self, "provider_asset_id", _validate_identity(self.provider_asset_id, "provider_asset_id"))
        object.__setattr__(self, "provider_variant_id", _validate_identity(self.provider_variant_id, "provider_variant_id"))
        if self.retryable is not True or not self.reason:
            raise BackgroundPoolError("Transient failures must remain explicitly retryable with a reason.")

    @property
    def provider_file_id(self) -> str:
        return self.provider_variant_id


@dataclass(frozen=True)
class StaleBackgroundRecord:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    verified_record_id: str
    reason: str
    detected_at: str
    expected_sha256: str
    actual_sha256: Optional[str] = None
    status: str = "stale"
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION or self.status != "stale":
            raise BackgroundPoolError("Invalid stale record schema/status.")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        object.__setattr__(self, "provider_asset_id", _validate_identity(self.provider_asset_id, "provider_asset_id"))
        object.__setattr__(self, "provider_variant_id", _validate_identity(self.provider_variant_id, "provider_variant_id"))
        object.__setattr__(self, "expected_sha256", _validate_sha256(self.expected_sha256, "expected_sha256"))
        if self.actual_sha256 is not None:
            object.__setattr__(self, "actual_sha256", _validate_sha256(self.actual_sha256, "actual_sha256"))

    @property
    def provider_file_id(self) -> str:
        return self.provider_variant_id


@dataclass(frozen=True)
class BackgroundUsageEvent:
    run_id: str
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    verified_record_id: str
    used_at: str
    reel_duration_seconds: float
    source_mode: str = "VERIFIED_POOL"
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION or self.source_mode != "VERIFIED_POOL":
            raise BackgroundPoolError("Invalid background usage event.")
        _validate_identity(self.run_id, "run_id")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        object.__setattr__(self, "provider_asset_id", _validate_identity(self.provider_asset_id, "provider_asset_id"))
        object.__setattr__(self, "provider_variant_id", _validate_identity(self.provider_variant_id, "provider_variant_id"))
        if self.reel_duration_seconds <= 0:
            raise BackgroundPoolError("Usage reel duration must be positive.")

    @property
    def provider_file_id(self) -> str:
        return self.provider_variant_id


@dataclass(frozen=True)
class ProviderQueryMetrics:
    """Append-only evidence about one provider/query within one builder run."""

    builder_run_id: str
    provider: str
    query: str
    api_requests: int = 0
    pages_attempted: int = 0
    candidates_returned: int = 0
    unique_variants_seen: int = 0
    known_verified_skips: int = 0
    known_safety_rejected_skips: int = 0
    known_metadata_ineligible_skips: int = 0
    duplicate_asset_skips: int = 0
    duplicate_content_sha_skips: int = 0
    metadata_ineligible: int = 0
    downloads_attempted: int = 0
    downloaded_bytes: int = 0
    scans_attempted: int = 0
    scanned_frames: int = 0
    verified_safe: int = 0
    safety_rejected: int = 0
    transient_failures: int = 0
    download_seconds: float = 0.0
    scan_seconds: float = 0.0
    updated_at: str = ""
    schema_version: int = BACKGROUND_POOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BACKGROUND_POOL_SCHEMA_VERSION:
            raise BackgroundPoolError("Unsupported query-metrics schema_version.")
        _validate_identity(self.builder_run_id, "builder_run_id")
        object.__setattr__(self, "provider", _validate_provider(self.provider))
        if not self.query.strip():
            raise BackgroundPoolError("Query metrics require a non-empty query.")
        integer_fields = (
            "api_requests", "pages_attempted", "candidates_returned", "unique_variants_seen",
            "known_verified_skips", "known_safety_rejected_skips",
            "known_metadata_ineligible_skips", "duplicate_asset_skips",
            "duplicate_content_sha_skips",
            "metadata_ineligible", "downloads_attempted",
            "downloaded_bytes", "scans_attempted", "scanned_frames", "verified_safe",
            "safety_rejected", "transient_failures",
        )
        if any(getattr(self, name) < 0 for name in integer_fields):
            raise BackgroundPoolError("Query metric counters cannot be negative.")
        if self.download_seconds < 0 or self.scan_seconds < 0:
            raise BackgroundPoolError("Query metric durations cannot be negative.")


def verified_record_from_dict(data: Mapping[str, Any]) -> VerifiedBackgroundRecord:
    if int(data.get("schema_version", -1)) != BACKGROUND_POOL_SCHEMA_VERSION:
        raise BackgroundPoolError("Unsupported verified background schema_version.")
    safety_raw = data.get("safety")
    if not isinstance(safety_raw, Mapping):
        raise BackgroundPoolError("Verified record is missing safety evidence.")
    if not isinstance(data.get("active", True), bool):
        raise BackgroundPoolError("Verified record active must be a JSON boolean.")
    return VerifiedBackgroundRecord(
        schema_version=int(data["schema_version"]),
        provider=str(data["provider"]),
        provider_asset_id=str(data["provider_asset_id"]),
        provider_variant_id=str(data["provider_variant_id"]),
        source_page_url=str(data.get("source_page_url", "")),
        width=int(data["width"]),
        height=int(data["height"]),
        duration_seconds=float(data["duration_seconds"]),
        fps=float(data["fps"]) if data.get("fps") is not None else None,
        mime_type=str(data["mime_type"]),
        declared_file_size=int(data["declared_file_size"]) if data.get("declared_file_size") is not None else None,
        downloaded_file_size=int(data["downloaded_file_size"]),
        content_sha256=str(data["content_sha256"]),
        verified_at=str(data["verified_at"]),
        verification_tool_version=str(data["verification_tool_version"]),
        safety_policy=dict(data.get("safety_policy", {})),
        safety=SafetyEvidence(**dict(safety_raw)),
        query=str(data.get("query", "")),
        status=str(data.get("status", "verified")),
        active=data.get("active", True),
        render_robustness=dict(data["render_robustness"]) if isinstance(data.get("render_robustness"), Mapping) else (
            dict(data["value"]["render_robustness"]) if isinstance(data.get("value"), Mapping) and isinstance(data["value"].get("render_robustness"), Mapping) else None
        ),
        operational_state=dict(data["operational_state"]) if isinstance(data.get("operational_state"), Mapping) else (
            dict(data["value"]["operational_state"]) if isinstance(data.get("value"), Mapping) and isinstance(data["value"].get("operational_state"), Mapping) else None
        ),
    )


# Risk tier constants
RISK_TIER_ROBUST = "ROBUST"
RISK_TIER_LOW_RISK = "LOW_RISK"
RISK_TIER_UNKNOWN_RISK = "UNKNOWN_RISK"
RISK_TIER_MEDIUM_RISK = "MEDIUM_RISK"
RISK_TIER_HIGH_RISK = "HIGH_RISK"

RISK_TIER_PRIORITY_ORDER = (
    RISK_TIER_ROBUST,
    RISK_TIER_LOW_RISK,
    RISK_TIER_UNKNOWN_RISK,
    RISK_TIER_MEDIUM_RISK,
)

RISK_TIER_RANK_MAP = {
    RISK_TIER_ROBUST: 0,
    RISK_TIER_LOW_RISK: 1,
    RISK_TIER_UNKNOWN_RISK: 2,
    RISK_TIER_MEDIUM_RISK: 3,
    RISK_TIER_HIGH_RISK: 4,
}


def compute_render_config_fingerprint(
    render_config: Any,
    fixture: str = "english_rwwad:2:21",
    fixture_version: str = "1.0.19",
    unique_overlay_states: int = 11,
    schema_version: int = 2,
    strategy: str = "whole_source_x_overlay_states_v1",
) -> str:
    """Compute deterministic fingerprint for visual render configuration."""
    sub = getattr(render_config, "subtitle_config", None)
    canonical_dict = {
        "alignment_fixture": fixture,
        "alignment_version": fixture_version,
        "arabic_font_size": int(getattr(sub, "arabic_font_size", 170)),
        "arabic_line_spacing": float(getattr(sub, "arabic_line_spacing", 1.15)),
        "arabic_translation_gap_px": int(getattr(sub, "arabic_translation_gap_px", 10)),
        "ayah_reference_position": str(getattr(sub, "ayah_reference_position", "top_header")),
        "color_range_policy": "limited_tv_yuv420p",
        "header_backing_intensity": str(getattr(sub, "header_backing_intensity", "low")),
        "header_content": str(getattr(sub, "header_content", "arabic_translation_ayah_range")),
        "header_style": str(getattr(sub, "header_style", "minimal_capsule")),
        "output_geometry": f"{getattr(render_config, 'width', 1080)}x{getattr(render_config, 'height', 1920)}@{getattr(render_config, 'fps', 30)}fps",
        "pixel_format": str(getattr(render_config, "pixel_format", "yuv420p")),
        "qualification_schema_version": int(schema_version),
        "qualification_strategy": str(strategy),
        "safety_policy_fingerprint": CURRENT_SAFETY_POLICY_FINGERPRINT,
        "subtitle_backing": str(getattr(sub, "subtitle_backing", "shadow_only")),
        "subtitle_lead_ms": int(getattr(sub, "subtitle_lead_ms", 0)),
        "subtitle_mode": str(getattr(sub, "subtitle_mode", "progressive_phrase_sync")),
        "text_outline": str(getattr(sub, "text_outline", "minimal")),
        "text_shadow": str(getattr(sub, "text_shadow", "very_soft")),
        "transition_duration_ms": int(getattr(sub, "transition_duration_ms", 100)),
        "translation_font_size": int(getattr(sub, "translation_font_size", 50)),
        "translation_language": "en",
        "translation_line_spacing": float(getattr(sub, "translation_line_spacing", 1.25)),
        "translation_stack_gap_px": int(getattr(sub, "translation_stack_gap_px", 30)),
        "unique_overlay_states_count": int(unique_overlay_states),
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def get_render_robustness(record: Any) -> Optional[dict[str, Any]]:
    """Extract render_robustness dictionary from record or dictionary value."""
    if isinstance(record, Mapping):
        val = record.get("value")
        if isinstance(val, Mapping) and isinstance(val.get("render_robustness"), Mapping):
            return dict(val["render_robustness"])
        rob = record.get("render_robustness")
        return dict(rob) if isinstance(rob, Mapping) else None

    if hasattr(record, "render_robustness") and isinstance(record.render_robustness, Mapping):
        return dict(record.render_robustness)
    return None


def get_render_risk_tier(record: Any, current_fingerprint: Optional[str] = None) -> str:
    """Classify record risk tier based on failed_evaluation_rate and current visual fingerprint match."""
    rob = get_render_robustness(record)
    if rob is None:
        return RISK_TIER_UNKNOWN_RISK

    if current_fingerprint is not None:
        stored_fp = rob.get("fixture_config_fingerprint")
        if not stored_fp or stored_fp != current_fingerprint:
            return RISK_TIER_UNKNOWN_RISK

    if "failed_evaluation_rate" in rob:
        try:
            rate = float(rob["failed_evaluation_rate"])
            if rate == 0.0:
                return RISK_TIER_ROBUST
            elif rate <= 0.05:
                return RISK_TIER_LOW_RISK
            elif rate <= 0.15:
                return RISK_TIER_MEDIUM_RISK
            else:
                return RISK_TIER_HIGH_RISK
        except (ValueError, TypeError):
            pass

    tier_str = str(rob.get("risk_tier", "")).upper()
    if tier_str in RISK_TIER_RANK_MAP:
        return tier_str

    return RISK_TIER_UNKNOWN_RISK


def is_source_safe(record: Any, current_safety_policy_fingerprint: Optional[str] = None) -> bool:
    """Return True if raw source safety evidence is completely SAFE and policy matches."""
    fp = current_safety_policy_fingerprint or CURRENT_SAFETY_POLICY_FINGERPRINT
    if isinstance(record, Mapping):
        val = record.get("value")
        safety = val.get("safety") if isinstance(val, Mapping) else record.get("safety")
        if not isinstance(safety, Mapping):
            return False
        return (
            safety.get("verdict") == "SAFE"
            and safety.get("person_result") == "CLEAR"
            and safety.get("face_result") == "CLEAR"
            and safety.get("animal_verdict") == "CLEAR"
            and safety.get("full_source_scan") is True
            and safety.get("policy_fingerprint") == fp
        )
    if hasattr(record, "safety") and hasattr(record.safety, "verdict"):
        return (
            record.safety.verdict == "SAFE"
            and record.safety.person_result == "CLEAR"
            and record.safety.face_result == "CLEAR"
            and record.safety.animal_verdict == "CLEAR"
            and record.safety.full_source_scan is True
            and record.safety.policy_fingerprint == fp
        )
    return False


def is_runtime_eligible(record: Any, current_safety_policy_fingerprint: Optional[str] = None) -> bool:
    """Return True if record is verified, active, and has valid raw source safety."""
    status = getattr(record, "status", None) if not isinstance(record, Mapping) else record.get("status")
    if status != "verified":
        return False
    active = getattr(record, "active", True) if not isinstance(record, Mapping) else record.get("active", True)
    if not active:
        return False
    return is_source_safe(record, current_safety_policy_fingerprint)


def usage_event_from_dict(data: Mapping[str, Any]) -> BackgroundUsageEvent:
    return BackgroundUsageEvent(**dict(data))


def verified_record_key(record: VerifiedBackgroundRecord, prefix: str = "") -> str:
    return (
        f"{prefix}{BACKGROUND_POOL_PREFIX}verified/{record.provider}/{record.provider_asset_id}/"
        f"{record.provider_variant_id}/{record.content_sha256}--{record.safety.policy_fingerprint}.json"
    )


def safety_rejected_record_key(record: SafetyRejectedRecord, prefix: str = "") -> str:
    return f"{prefix}{BACKGROUND_POOL_PREFIX}safety_rejected/{record.policy_fingerprint}/{record.provider}/{record.provider_asset_id}/{record.provider_variant_id}.json"


def metadata_ineligible_record_key(record: MetadataIneligibleRecord, prefix: str = "") -> str:
    return f"{prefix}{BACKGROUND_POOL_PREFIX}metadata_ineligible/{record.eligibility_fingerprint}/{record.provider}/{record.provider_asset_id}/{record.provider_variant_id}.json"


def transient_record_key(record: TransientFailureRecord, prefix: str = "") -> str:
    parsed = datetime.datetime.fromisoformat(record.failed_at.replace("Z", "+00:00"))
    event = hashlib.sha256(
        f"{record.provider}:{record.provider_asset_id}:{record.provider_variant_id}:{record.failed_at}:{record.reason}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}{BACKGROUND_POOL_PREFIX}transient_failures/{parsed:%Y/%m}/{record.provider}/{record.provider_asset_id}/{record.provider_variant_id}/{event}.json"


def stale_record_key(record: StaleBackgroundRecord, prefix: str = "") -> str:
    record_digest = hashlib.sha256(record.verified_record_id.encode("utf-8")).hexdigest()
    event = hashlib.sha256(f"{record.detected_at}:{record.reason}:{record.actual_sha256 or ''}".encode("utf-8")).hexdigest()[:20]
    return f"{prefix}{BACKGROUND_POOL_PREFIX}stale/{record_digest}/{event}.json"


def usage_event_key(event: BackgroundUsageEvent, prefix: str = "") -> str:
    parsed = datetime.datetime.fromisoformat(event.used_at.replace("Z", "+00:00"))
    record_digest = hashlib.sha256(event.verified_record_id.encode("utf-8")).hexdigest()
    return f"{prefix}{BACKGROUND_POOL_PREFIX}usage/{parsed:%Y/%m}/{event.run_id}/{record_digest}.json"


def query_metrics_key(metrics: ProviderQueryMetrics, prefix: str = "") -> str:
    query_digest = hashlib.sha256(metrics.query.strip().lower().encode("utf-8")).hexdigest()
    return f"{prefix}{BACKGROUND_POOL_PREFIX}query_metrics/{metrics.provider}/{query_digest}/{metrics.builder_run_id}.json"


class BackgroundPoolRepository:
    """Small JSON-only B2 repository for durable pool evidence."""

    def __init__(self, client: Any, b2_config: B2Config):
        self.client = client
        self.config = b2_config
        self._operation_counts = {
            "list_operations": 0,
            "metadata_reads": 0,
            "writes": 0,
        }

    def operation_counts(self) -> dict[str, int]:
        """Return non-secret attempted B2 operation counts for diagnostics."""
        return dict(self._operation_counts)

    def _put_json(self, key: str, data: Mapping[str, Any]) -> str:
        if not isinstance(data, Mapping):
            raise StorageError("Background pool storage accepts JSON object metadata only.")
        required_prefix = f"{self.config.prefix}{BACKGROUND_POOL_PREFIX}"
        if not key.startswith(required_prefix) or not key.endswith(".json"):
            raise StorageError(
                f"Background pool writes are restricted to JSON metadata below {BACKGROUND_POOL_PREFIX}."
            )
        lowered = key.lower()
        if any(ext in lowered for ext in VIDEO_EXTENSIONS):
            raise StorageError("Source video objects are forbidden below the background_pool prefix.")
        payload = json.dumps(dict(data), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        if len(payload) > 1_000_000:
            raise StorageError("Background pool metadata object exceeds the 1 MB safety limit.")
        try:
            self._operation_counts["writes"] += 1
            self.client.put_object(
                Bucket=self.config.bucket_name,
                Key=key,
                Body=payload,
                ContentType="application/json",
            )
        except Exception as err:
            raise StorageError(f"Failed to write background pool metadata: {err}") from err
        return key

    def _get_json(self, key: str) -> Optional[dict[str, Any]]:
        try:
            self._operation_counts["metadata_reads"] += 1
            response = self.client.get_object(Bucket=self.config.bucket_name, Key=key)
            parsed = json.loads(response["Body"].read().decode("utf-8"))
            if not isinstance(parsed, dict):
                raise StorageError("Background pool metadata must be a JSON object.")
            return parsed
        except Exception as err:
            error_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise StorageError(f"Failed to read background pool metadata: {err}") from err

    def _list_keys(self, suffix: str) -> tuple[str, ...]:
        full_prefix = f"{self.config.prefix}{BACKGROUND_POOL_PREFIX}{suffix.lstrip('/')}"
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            keys: list[str] = []
            first_page = True
            self._operation_counts["list_operations"] += 1
            for page in paginator.paginate(Bucket=self.config.bucket_name, Prefix=full_prefix):
                if first_page:
                    first_page = False
                else:
                    self._operation_counts["list_operations"] += 1
                keys.extend(
                    item["Key"]
                    for item in page.get("Contents", [])
                    if str(item.get("Key", "")).endswith(".json")
                )
            return tuple(sorted(keys))
        except Exception as err:
            raise StorageError(f"Failed to list background pool metadata: {err}") from err

    def save_verified(self, record: VerifiedBackgroundRecord) -> str:
        return self._put_json(verified_record_key(record, self.config.prefix), asdict(record))

    def save_safety_rejected(self, record: SafetyRejectedRecord) -> str:
        return self._put_json(safety_rejected_record_key(record, self.config.prefix), asdict(record))

    def save_metadata_ineligible(self, record: MetadataIneligibleRecord) -> str:
        return self._put_json(metadata_ineligible_record_key(record, self.config.prefix), asdict(record))

    def save_transient(self, record: TransientFailureRecord) -> str:
        return self._put_json(transient_record_key(record, self.config.prefix), asdict(record))

    def save_stale(self, record: StaleBackgroundRecord) -> str:
        return self._put_json(stale_record_key(record, self.config.prefix), asdict(record))

    def save_usage(self, event: BackgroundUsageEvent) -> str:
        return self._put_json(usage_event_key(event, self.config.prefix), asdict(event))

    def record_render_safety_failure(self, *args: Any, **kwargs: Any) -> str:
        # B2 pool repository does not record telemetry events.
        return ""

    def save_query_metrics(self, metrics: ProviderQueryMetrics) -> str:
        return self._put_json(query_metrics_key(metrics, self.config.prefix), asdict(metrics))

    def save_builder_run(self, builder_run_id: str, data: Mapping[str, Any]) -> str:
        run_id = _validate_identity(builder_run_id, "builder_run_id")
        key = f"{self.config.prefix}{BACKGROUND_POOL_PREFIX}builder_runs/{run_id}.json"
        return self._put_json(key, data)

    def save_stats(self, data: Mapping[str, Any]) -> str:
        key = f"{self.config.prefix}{BACKGROUND_POOL_PREFIX}stats/latest.json"
        return self._put_json(key, data)

    def list_verified(self) -> tuple[VerifiedBackgroundRecord, ...]:
        records: list[VerifiedBackgroundRecord] = []
        for key in self._list_keys("verified/"):
            raw = self._get_json(key)
            if raw is None:
                continue
            try:
                records.append(verified_record_from_dict(raw))
            except BackgroundPoolError:
                continue
        return tuple(records)

    def list_usage(self) -> tuple[BackgroundUsageEvent, ...]:
        events: list[BackgroundUsageEvent] = []
        for key in self._list_keys("usage/"):
            raw = self._get_json(key)
            if raw is None:
                continue
            try:
                events.append(usage_event_from_dict(raw))
            except (BackgroundPoolError, TypeError, KeyError, ValueError):
                continue
        return tuple(events)

    def current_safety_rejected_identities(self) -> set[tuple[str, str, str]]:
        identities: set[tuple[str, str, str]] = set()
        fingerprint = CURRENT_SAFETY_POLICY_FINGERPRINT
        for key in self._list_keys(f"safety_rejected/{fingerprint}/"):
            raw = self._get_json(key)
            if raw and raw.get("policy_fingerprint") == fingerprint:
                identities.add(
                    (
                        str(raw.get("provider")),
                        str(raw.get("provider_asset_id")),
                        str(raw.get("provider_variant_id")),
                    )
                )
        return identities

    def current_metadata_ineligible_identities(
        self,
        eligibility_fingerprint: str,
    ) -> set[tuple[str, str, str]]:
        fingerprint = _validate_sha256(eligibility_fingerprint, "eligibility_fingerprint")
        identities: set[tuple[str, str, str]] = set()
        for key in self._list_keys(f"metadata_ineligible/{fingerprint}/"):
            raw = self._get_json(key)
            if raw and raw.get("eligibility_fingerprint") == fingerprint:
                identities.add(
                    (
                        str(raw.get("provider")),
                        str(raw.get("provider_asset_id")),
                        str(raw.get("provider_variant_id")),
                    )
                )
        return identities

    def stale_record_ids(self) -> set[str]:
        record_ids: set[str] = set()
        for key in self._list_keys("stale/"):
            raw = self._get_json(key)
            if raw and raw.get("verified_record_id"):
                record_ids.add(str(raw["verified_record_id"]))
        return record_ids

    def transient_count(self) -> int:
        return len(self._list_keys("transient_failures/"))

    def metadata_ineligible_count(self) -> int:
        return len(self._list_keys("metadata_ineligible/"))

    def legacy_v1_verified_count(self) -> int:
        """Count immutable V1 evidence without ever parsing it as V2-eligible."""
        legacy_prefix = f"{self.config.prefix}{LEGACY_BACKGROUND_POOL_PREFIX}verified/"
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            count = 0
            first_page = True
            self._operation_counts["list_operations"] += 1
            for page in paginator.paginate(Bucket=self.config.bucket_name, Prefix=legacy_prefix):
                if first_page:
                    first_page = False
                else:
                    self._operation_counts["list_operations"] += 1
                count += sum(
                    1
                    for item in page.get("Contents", [])
                    if str(item.get("Key", "")).endswith(".json")
                )
            return count
        except Exception as err:
            raise StorageError(f"Failed to count legacy V1 pool metadata: {err}") from err

    def query_metrics_rollup(self) -> dict[str, dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        additive_fields = (
            "api_requests", "pages_attempted", "candidates_returned", "unique_variants_seen",
            "known_verified_skips", "known_safety_rejected_skips",
            "known_metadata_ineligible_skips", "duplicate_asset_skips",
            "duplicate_content_sha_skips",
            "metadata_ineligible", "downloads_attempted",
            "downloaded_bytes", "scans_attempted", "scanned_frames", "verified_safe",
            "safety_rejected", "transient_failures", "download_seconds", "scan_seconds",
        )
        for key in self._list_keys("query_metrics/"):
            raw = self._get_json(key)
            if raw is None:
                continue
            try:
                metrics = ProviderQueryMetrics(**raw)
            except (BackgroundPoolError, TypeError, KeyError, ValueError):
                continue
            identity = f"{metrics.provider}:{metrics.query}"
            aggregate = totals.setdefault(
                identity,
                {"provider": metrics.provider, "query": metrics.query, **{name: 0 for name in additive_fields}},
            )
            for name in additive_fields:
                aggregate[name] += getattr(metrics, name)
        for aggregate in totals.values():
            discovered = int(aggregate["candidates_returned"])
            scanned = int(aggregate["scans_attempted"])
            safe = int(aggregate["verified_safe"])
            aggregate["eligibility_rate"] = (
                max(0, discovered - int(aggregate["metadata_ineligible"])) / discovered
                if discovered else 0.0
            )
            aggregate["safe_rate_among_scanned"] = safe / scanned if scanned else 0.0
            aggregate["overall_safe_yield"] = safe / discovered if discovered else 0.0
        return totals

    def reconcile_pool_state(
        self,
        eligibility_fingerprint: Optional[str] = None,
    ) -> BackgroundPoolReconciliation:
        """Read authoritative pool namespaces once for one reconciliation.

        Candidate processing uses the returned identity sets and run-local
        counters. Calling this per candidate would reintroduce Class-B read
        amplification.
        """
        verified = self.list_verified()
        stale = self.stale_record_ids()
        current = tuple(
            record for record in verified
            if record.is_current_policy and record.record_id not in stale
        )
        stale_policy = tuple(record for record in verified if not record.is_current_policy)
        legacy_v1_stale = self.legacy_v1_verified_count()
        rejected = self.current_safety_rejected_identities()
        usage = self.list_usage()
        metadata_keys = self._list_keys("metadata_ineligible/")
        ineligible: set[tuple[str, str, str]] = set()
        if eligibility_fingerprint is not None:
            fingerprint = _validate_sha256(
                eligibility_fingerprint, "eligibility_fingerprint"
            )
            expected_fragment = f"metadata_ineligible/{fingerprint}/"
            for key in metadata_keys:
                if expected_fragment not in key:
                    continue
                raw = self._get_json(key)
                if raw and raw.get("eligibility_fingerprint") == fingerprint:
                    ineligible.add(
                        (
                            str(raw.get("provider")),
                            str(raw.get("provider_asset_id")),
                            str(raw.get("provider_variant_id")),
                        )
                    )
        used_ids = {event.verified_record_id for event in usage}
        current_ids = {record.record_id for record in current}
        stats = {
            "schema_version": BACKGROUND_POOL_SCHEMA_VERSION,
            "data_version": BACKGROUND_POOL_DATA_VERSION,
            "computed_at": utc_now_iso(),
            "policy_fingerprint": CURRENT_SAFETY_POLICY_FINGERPRINT,
            "verified_current_policy": len(current),
            "verified_stale_policy": len(stale_policy) + legacy_v1_stale,
            "known_safety_rejected": len(rejected),
            "known_rejected": len(rejected),
            "metadata_ineligible": len(metadata_keys),
            "transient_failures": self.transient_count(),
            "production_used_unique": len(current_ids & used_ids),
            "production_unused": len(current_ids - used_ids),
            "missing_or_stale": len(stale),
            "legacy_v1_stale": legacy_v1_stale,
            "pexels_verified_current_policy": sum(1 for record in current if record.provider == "pexels"),
            "pixabay_verified_current_policy": sum(1 for record in current if record.provider == "pixabay"),
            "query_metrics": self.query_metrics_rollup(),
        }
        return BackgroundPoolReconciliation(
            stats=stats,
            verified_records=verified,
            stale_record_ids=frozenset(stale),
            safety_rejected_identities=frozenset(rejected),
            metadata_ineligible_identities=frozenset(ineligible),
        )

    def compute_stats(self) -> dict[str, Any]:
        """Perform one full authoritative reconciliation and return its totals."""
        return dict(self.reconcile_pool_state().stats)


class SupabaseBackgroundPoolRepository:
    """Supabase PostgreSQL-backed background pool repository.

    Performs zero B2 operations and persists pool state strictly in Supabase.
    """

    def __init__(self, store: SupabaseStateStore):
        self.store = store

    def operation_counts(self) -> dict[str, int]:
        """Return B2 operation counts (always 0 in Supabase mode)."""
        return {
            "list_operations": 0,
            "metadata_reads": 0,
            "writes": 0,
        }

    def save_verified(self, record: VerifiedBackgroundRecord) -> str:
        self.store._request(
            "POST",
            "background_assets",
            payload={
                "provider": record.provider,
                "provider_asset_id": str(record.provider_asset_id),
                "provider_variant_id": str(record.provider_variant_id),
                "policy_fingerprint": record.safety.policy_fingerprint,
                "content_sha256": record.content_sha256,
                "status": "verified",
                "verified_at": record.verified_at,
                "value": asdict(record),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return record.record_id

    def save_safety_rejected(self, record: SafetyRejectedRecord) -> str:
        return self.store.record_background_event(
            event_type="safety_rejected",
            provider=record.provider,
            provider_asset_id=str(record.provider_asset_id),
            provider_variant_id=str(record.provider_variant_id),
            fingerprint=record.policy_fingerprint,
            value=asdict(record),
            occurred_at=record.rejected_at,
        )

    def save_metadata_ineligible(self, record: MetadataIneligibleRecord) -> str:
        return self.store.record_background_event(
            event_type="metadata_ineligible",
            provider=record.provider,
            provider_asset_id=str(record.provider_asset_id),
            provider_variant_id=str(record.provider_variant_id),
            fingerprint=record.eligibility_fingerprint,
            value=asdict(record),
            occurred_at=record.recorded_at,
        )

    def save_transient(self, record: TransientFailureRecord) -> str:
        return self.store.record_background_event(
            event_type="transient_failure",
            provider=record.provider,
            provider_asset_id=str(record.provider_asset_id),
            provider_variant_id=str(record.provider_variant_id),
            fingerprint=record.reason,
            value=asdict(record),
            occurred_at=record.failed_at,
        )

    def save_stale(self, record: StaleBackgroundRecord) -> str:
        self.store._request(
            "PATCH",
            "background_assets",
            params={
                "provider": f"eq.{record.provider}",
                "provider_asset_id": f"eq.{record.provider_asset_id}",
                "provider_variant_id": f"eq.{record.provider_variant_id}",
            },
            payload={"status": "stale"},
            prefer="return=minimal",
        )
        return self.store.record_background_event(
            event_type="stale",
            provider=record.provider,
            provider_asset_id=str(record.provider_asset_id),
            provider_variant_id=str(record.provider_variant_id),
            fingerprint=record.policy_fingerprint,
            value=asdict(record),
            occurred_at=record.quarantined_at,
        )

    def save_usage(self, event: BackgroundUsageEvent) -> str:
        parts = event.verified_record_id.split(":")
        sha = parts[3] if len(parts) >= 4 else event.verified_record_id
        self.store.record_background_usage(
            provider=event.provider,
            provider_asset_id=str(event.provider_asset_id),
            provider_variant_id=str(event.provider_variant_id),
            content_sha256=sha,
            run_id=event.run_id,
            value=asdict(event),
        )
        return event.verified_record_id

    def record_render_safety_failure(
        self,
        provider: str,
        provider_asset_id: str,
        provider_variant_id: str,
        content_sha256: str,
        run_id: str,
        attempt_number: int,
        start_offset_seconds: float,
        verse_keys: Sequence[str],
        render_config_fingerprint: str,
        failure_reason: str,
        detector_counts: Mapping[str, int],
        background_plan: Optional[Sequence[Mapping[str, Any]]] = None,
        occurred_at: Optional[str] = None,
    ) -> str:
        return self.store.record_render_safety_failure(
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
            occurred_at=occurred_at,
        )

    def save_query_metrics(self, metrics: ProviderQueryMetrics) -> str:
        self.store.save_query_metrics(
            builder_run_id=metrics.builder_run_id,
            provider=metrics.provider,
            query=metrics.query,
            value=asdict(metrics),
        )
        return f"{metrics.provider}:{metrics.query}"

    def save_builder_run(self, builder_run_id: str, data: Mapping[str, Any]) -> str:
        self.store.save_builder_run(
            builder_run_id=builder_run_id,
            status=str(data.get("state", "running")),
            value=dict(data),
        )
        return builder_run_id

    def save_stats(self, data: Mapping[str, Any]) -> str:
        return ""

    def list_verified(self) -> tuple[VerifiedBackgroundRecord, ...]:
        rows = self.store._request(
            "GET",
            "background_assets",
            params={"select": "value"},
        )
        records: list[VerifiedBackgroundRecord] = []
        for r in rows:
            val = r.get("value")
            if isinstance(val, Mapping):
                try:
                    records.append(verified_record_from_dict(val))
                except Exception:
                    continue
        return tuple(records)

    def list_usage(self) -> tuple[BackgroundUsageEvent, ...]:
        rows = self.store._request(
            "GET",
            "background_events",
            params={"event_type": "eq.usage", "select": "value"},
        )
        events: list[BackgroundUsageEvent] = []
        for r in rows:
            val = r.get("value")
            if isinstance(val, Mapping):
                try:
                    events.append(usage_event_from_dict(val))
                except Exception:
                    continue
        return tuple(events)

    def current_safety_rejected_identities(self) -> set[tuple[str, str, str]]:
        rows = self.store._request(
            "GET",
            "background_events",
            params={
                "event_type": "eq.safety_rejected",
                "fingerprint": f"eq.{CURRENT_SAFETY_POLICY_FINGERPRINT}",
                "select": "provider,provider_asset_id,provider_variant_id",
            },
        )
        return {
            (
                str(r["provider"]),
                str(r["provider_asset_id"]),
                str(r["provider_variant_id"]),
            )
            for r in rows
            if "provider" in r and "provider_asset_id" in r and "provider_variant_id" in r
        }

    def current_metadata_ineligible_identities(
        self, eligibility_fingerprint: str
    ) -> set[tuple[str, str, str]]:
        fingerprint = _validate_sha256(eligibility_fingerprint, "eligibility_fingerprint")
        rows = self.store._request(
            "GET",
            "background_events",
            params={
                "event_type": "eq.metadata_ineligible",
                "fingerprint": f"eq.{fingerprint}",
                "select": "provider,provider_asset_id,provider_variant_id",
            },
        )
        return {
            (
                str(r["provider"]),
                str(r["provider_asset_id"]),
                str(r["provider_variant_id"]),
            )
            for r in rows
            if "provider" in r and "provider_asset_id" in r and "provider_variant_id" in r
        }

    def stale_record_ids(self) -> set[str]:
        rows = self.store._request(
            "GET",
            "background_assets",
            params={
                "status": "eq.stale",
                "select": "provider,provider_asset_id,provider_variant_id,content_sha256,policy_fingerprint,value",
            },
        )
        stale_ids: set[str] = set()
        for r in rows:
            stale_ids.add(
                f"{r.get('provider')}:{r.get('provider_asset_id')}:{r.get('provider_variant_id')}:{r.get('content_sha256')}:{r.get('policy_fingerprint')}"
            )
            stale_ids.add(
                f"{r.get('provider')}:{r.get('provider_asset_id')}:{r.get('provider_variant_id')}:{r.get('content_sha256')}--{r.get('policy_fingerprint')}"
            )
            val = r.get("value")
            if isinstance(val, Mapping):
                rec_id = val.get("record_id")
                if rec_id:
                    stale_ids.add(str(rec_id))
        stale_events = self.store._request(
            "GET",
            "background_events",
            params={"event_type": "eq.stale", "select": "value"},
        )
        for ev in stale_events:
            val = ev.get("value", {})
            if isinstance(val, Mapping) and val.get("verified_record_id"):
                stale_ids.add(str(val["verified_record_id"]))
        return stale_ids

    def transient_count(self) -> int:
        rows = self.store._request(
            "GET",
            "background_events",
            params={"event_type": "eq.transient_failure", "select": "event_id"},
        )
        return len(rows)

    def metadata_ineligible_count(self) -> int:
        rows = self.store._request(
            "GET",
            "background_events",
            params={"event_type": "eq.metadata_ineligible", "select": "event_id"},
        )
        return len(rows)

    def legacy_v1_verified_count(self) -> int:
        return 0

    def query_metrics_rollup(self) -> dict[str, dict[str, Any]]:
        rows = self.store._request(
            "GET",
            "background_query_metrics",
            params={"select": "value"},
        )
        totals: dict[str, dict[str, Any]] = {}
        additive_fields = (
            "api_requests", "pages_attempted", "candidates_returned", "unique_variants_seen",
            "known_verified_skips", "known_safety_rejected_skips",
            "known_metadata_ineligible_skips", "duplicate_asset_skips",
            "metadata_ineligible", "downloads_attempted",
            "downloaded_bytes", "scans_attempted", "scanned_frames", "verified_safe",
            "safety_rejected", "transient_failures", "download_seconds", "scan_seconds",
        )
        for r in rows:
            raw = r.get("value")
            if not isinstance(raw, Mapping):
                continue
            try:
                metrics = ProviderQueryMetrics(**dict(raw))
            except Exception:
                continue
            identity = f"{metrics.provider}:{metrics.query}"
            aggregate = totals.setdefault(
                identity,
                {"provider": metrics.provider, "query": metrics.query, **{name: 0 for name in additive_fields}},
            )
            for name in additive_fields:
                aggregate[name] += getattr(metrics, name, 0)
        for aggregate in totals.values():
            discovered = int(aggregate["candidates_returned"])
            scanned = int(aggregate["scans_attempted"])
            safe = int(aggregate["verified_safe"])
            aggregate["eligibility_rate"] = (
                max(0, discovered - int(aggregate["metadata_ineligible"])) / discovered
                if discovered else 0.0
            )
            aggregate["safe_rate_among_scanned"] = safe / scanned if scanned else 0.0
            aggregate["overall_safe_yield"] = safe / discovered if discovered else 0.0
        return totals

    def reconcile_pool_state(
        self,
        eligibility_fingerprint: Optional[str] = None,
    ) -> BackgroundPoolReconciliation:
        verified = self.list_verified()
        stale = self.stale_record_ids()
        current = tuple(
            record for record in verified
            if record.is_current_policy and record.record_id not in stale
        )
        stale_policy = tuple(record for record in verified if not record.is_current_policy)
        rejected = self.current_safety_rejected_identities()
        ineligible = (
            self.current_metadata_ineligible_identities(eligibility_fingerprint)
            if eligibility_fingerprint is not None
            else set()
        )
        usage = self.list_usage()
        used_ids = {event.verified_record_id for event in usage}
        current_ids = {record.record_id for record in current}

        stats = {
            "schema_version": BACKGROUND_POOL_SCHEMA_VERSION,
            "data_version": BACKGROUND_POOL_DATA_VERSION,
            "computed_at": utc_now_iso(),
            "policy_fingerprint": CURRENT_SAFETY_POLICY_FINGERPRINT,
            "verified_current_policy": len(current),
            "verified_stale_policy": len(stale_policy),
            "known_safety_rejected": len(rejected),
            "known_rejected": len(rejected),
            "metadata_ineligible": self.metadata_ineligible_count(),
            "transient_failures": self.transient_count(),
            "production_used_unique": len(current_ids & used_ids),
            "production_unused": len(current_ids - used_ids),
            "missing_or_stale": len(stale),
            "legacy_v1_stale": 0,
            "pexels_verified_current_policy": sum(1 for record in current if record.provider == "pexels"),
            "pixabay_verified_current_policy": sum(1 for record in current if record.provider == "pixabay"),
            "query_metrics": self.query_metrics_rollup(),
        }
        return BackgroundPoolReconciliation(
            stats=stats,
            verified_records=verified,
            stale_record_ids=frozenset(stale),
            safety_rejected_identities=frozenset(rejected),
            metadata_ineligible_identities=frozenset(ineligible),
        )

    def compute_stats(self) -> dict[str, Any]:
        return dict(self.reconcile_pool_state().stats)


def get_background_pool_repository(
    env: Optional[Mapping[str, str]] = None,
    *,
    b2_client: Optional[Any] = None,
    b2_config: Optional[B2Config] = None,
    supabase_store: Optional[SupabaseStateStore] = None,
) -> BackgroundPoolRepository | SupabaseBackgroundPoolRepository:
    """Return the active background pool repository based on STATE_AUTHORITY."""
    authority = get_state_authority(env)
    if authority == STATE_AUTHORITY_SUPABASE:
        if supabase_store is not None:
            return SupabaseBackgroundPoolRepository(supabase_store)
        sb_config = SupabaseConfig.from_env(env)
        return SupabaseBackgroundPoolRepository(SupabaseStateStore(sb_config))

    cfg = b2_config if b2_config is not None else load_b2_config_from_env(env)
    client = b2_client if b2_client is not None else create_b2_client(cfg)
    return BackgroundPoolRepository(client, cfg)


class BackgroundProvider(Protocol):
    name: str

    def search(self, query: str, page: int = 1) -> BackgroundDiscoveryPage: ...

    def get_asset(self, asset_id: str) -> Mapping[str, Any]: ...

    def resolve_variant(self, asset: Mapping[str, Any], file_id: str) -> BackgroundCandidate: ...


class PexelsProvider:
    name = "pexels"

    def __init__(self, api_key: Optional[str] = None, orientation: str = "portrait"):
        self.api_key = api_key
        self.orientation = orientation

    def search(self, query: str, page: int = 1, orientation: Optional[str] = None) -> BackgroundDiscoveryPage:
        selected_orientation = orientation or self.orientation
        cfg = BackgroundSearchConfig(orientation=selected_orientation)
        result = search_background_candidates(
            query, api_key=self.api_key, page=page, config=cfg, include_discovery_accounting=True,
        )
        assert isinstance(result, BackgroundDiscoveryPage)
        return result

    def get_asset(self, asset_id: str) -> Mapping[str, Any]:
        return fetch_pexels_video_asset(int(asset_id), api_key=self.api_key)

    def resolve_variant(self, asset: Mapping[str, Any], file_id: str) -> BackgroundCandidate:
        return resolve_pexels_video_variant(dict(asset), int(file_id))


class PixabayProvider:
    name = "pixabay"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    def search(self, query: str, page: int = 1) -> BackgroundDiscoveryPage:
        result = search_pixabay_background_candidates(
            query, api_key=self.api_key, page=page, include_discovery_accounting=True,
        )
        assert isinstance(result, BackgroundDiscoveryPage)
        return result

    def get_asset(self, asset_id: str) -> Mapping[str, Any]:
        return fetch_pixabay_video_asset(int(asset_id), api_key=self.api_key)

    def resolve_variant(self, asset: Mapping[str, Any], file_id: str) -> BackgroundCandidate:
        return resolve_pixabay_video_variant(dict(asset), str(file_id))


def make_provider(name: str, *, api_key: Optional[str] = None) -> BackgroundProvider:
    provider = _validate_provider(name)
    if provider == "pexels":
        return PexelsProvider(api_key=api_key)
    if provider == "pixabay":
        return PixabayProvider(api_key=api_key)
    raise BackgroundPoolError(f"Unsupported background provider '{provider}'.")


def select_verified_pool_records(
    records: Sequence[VerifiedBackgroundRecord],
    usage_events: Sequence[BackgroundUsageEvent],
    *,
    required_duration_seconds: float,
    recent_provider_identities: Sequence[tuple[str, str]] = (),
    recent_asset_ids: Sequence[int] = (),
    excluded_provider_identities: Sequence[tuple[str, str]] = (),
    excluded_asset_ids: Sequence[int] = (),
    render_config_fingerprint: Optional[str] = None,
    allow_high_risk: bool = False,
    seed: Optional[int] = None,
) -> tuple[VerifiedBackgroundRecord, ...]:
    """Attempt never-used provider assets first, then least-recently-used assets."""
    recent = {
        (_validate_provider(provider), _validate_identity(asset_id, "provider_asset_id"))
        for provider, asset_id in recent_provider_identities
    }
    # Compatibility for legacy RunRequest history, which is explicitly Pexels-only.
    recent.update(("pexels", str(value)) for value in recent_asset_ids)

    excluded = {
        (_validate_provider(provider), _validate_identity(asset_id, "provider_asset_id"))
        for provider, asset_id in excluded_provider_identities
    }
    excluded.update(("pexels", str(value)) for value in excluded_asset_ids)

    candidates = [
        record
        for record in records
        if is_runtime_eligible(record)
        and record.is_current_policy
        and record.duration_seconds >= required_duration_seconds
        and record.width >= 1080
        and record.height >= 1920
    ]

    if not allow_high_risk:
        candidates = [
            record
            for record in candidates
            if get_render_risk_tier(record, render_config_fingerprint) != RISK_TIER_HIGH_RISK
        ]

    # One current record per provider-qualified variant/policy. If source bytes
    # were reverified, the newest evidence supersedes the older hash rather than
    # making both variants appear independently unused.
    newest: dict[tuple[str, str, str, str], VerifiedBackgroundRecord] = {}
    for record in candidates:
        identity = (
            record.provider,
            record.provider_asset_id,
            record.provider_variant_id,
            record.safety.policy_fingerprint,
        )
        previous = newest.get(identity)
        if previous is None or record.verified_at > previous.verified_at:
            newest[identity] = record
    eligible = list(newest.values())

    # Usage belongs to the actual provider video, across variants, reverified
    # hashes, and policy versions. Recent production history is also evidence of
    # use even when its original pool usage event is unavailable.
    last_used_by_asset: dict[tuple[str, str], float] = {}
    for event in usage_events:
        identity = (event.provider, event.provider_asset_id)
        used_at = datetime.datetime.fromisoformat(event.used_at.replace("Z", "+00:00")).timestamp()
        last_used_by_asset[identity] = max(last_used_by_asset.get(identity, float("-inf")), used_at)

    rng = random.Random(seed)
    tie_breakers = {record.record_id: rng.random() for record in eligible}

    def rank(record: VerifiedBackgroundRecord) -> tuple[int, float, int, float, str]:
        tier = get_render_risk_tier(record, render_config_fingerprint)
        tier_rank = RISK_TIER_RANK_MAP.get(tier, RISK_TIER_RANK_MAP[RISK_TIER_UNKNOWN_RISK])
        identity = (record.provider, record.provider_asset_id)
        used = identity in last_used_by_asset or identity in recent
        # Undated recent history is conservatively placed after dated usage.
        # Risk orders unused records and breaks equal-recency reuse ties only.
        last_used = last_used_by_asset.get(identity, float("inf") if used else float("-inf"))
        return (int(used), last_used, tier_rank, tie_breakers[record.record_id], record.record_id)

    available = [
        record
        for record in eligible
        if (record.provider, record.provider_asset_id) not in excluded
    ]
    failed = [
        record
        for record in eligible
        if (record.provider, record.provider_asset_id) in excluded
    ]

    # Runtime-rejected identities remain a last resort after every unfailed
    # option, retaining the existing bounded final-safety retry behavior.
    return tuple(sorted(available, key=rank) + sorted(failed, key=rank))


def background_reuse_warnings(
    selected_records: Sequence[VerifiedBackgroundRecord],
    selectable_records: Sequence[VerifiedBackgroundRecord],
    usage_events: Sequence[BackgroundUsageEvent],
    *,
    recent_provider_identities: Sequence[tuple[str, str]] = (),
) -> tuple[dict[str, Any], ...]:
    """Describe actual successful selections, never acquisition or retry attempts.

    Counts are distinct provider assets remaining runtime-suitable for this
    render; selected fresh clips count toward the available fresh inventory.
    """
    last_used: dict[tuple[str, str], str] = {}
    for event in usage_events:
        identity = (event.provider, event.provider_asset_id)
        previous = last_used.get(identity)
        used_at = datetime.datetime.fromisoformat(event.used_at.replace("Z", "+00:00")).timestamp()
        previous_at = (
            datetime.datetime.fromisoformat(previous.replace("Z", "+00:00")).timestamp()
            if previous is not None else float("-inf")
        )
        if used_at > previous_at:
            last_used[identity] = event.used_at
    used = set(last_used) | set(recent_provider_identities)
    selectable = {(r.provider, r.provider_asset_id) for r in selectable_records}
    selected = {(r.provider, r.provider_asset_id) for r in selected_records}
    selectable |= selected
    reused = sorted(selected & used)
    return tuple({
        "event": "BACKGROUND_REUSE_FALLBACK",
        "provider": provider,
        "provider_asset_id": asset_id,
        "provider_variant_id": next(r.provider_variant_id for r in selected_records
                                    if (r.provider, r.provider_asset_id) == (provider, asset_id)),
        "last_used_at": last_used.get((provider, asset_id)),
        "required_background_count": len(selected),
        "unused_selectable_count": len(selectable - used),
        "selectable_count": len(selectable),
        "reuse_count_for_this_render": len(reused),
        "reason": "UNUSED_POOL_EXHAUSTED",
    } for provider, asset_id in reused)


def build_verified_record(
    candidate: Any,
    *,
    content_sha256: str,
    downloaded_file_size: int,
    scanned_frames: int,
    animal_detector: str,
    animal_classes: Sequence[str],
    animal_threshold: float,
    animal_verdict: str,
    verified_at: Optional[str] = None,
) -> VerifiedBackgroundRecord:
    """Build V2 evidence from a provider-neutral candidate and explicit scan facts."""
    provider = _validate_provider(getattr(candidate, "provider"))
    provider_asset_id = _validate_identity(
        getattr(candidate, "provider_asset_id"), "provider_asset_id"
    )
    provider_variant_id = _validate_identity(
        getattr(candidate, "provider_variant_id"), "provider_variant_id"
    )
    normalized_classes = tuple(
        sorted({str(item).strip().lower() for item in animal_classes if str(item).strip()})
    )
    if (
        not animal_detector.strip()
        or animal_detector == "UNAVAILABLE"
        or not normalized_classes
        or animal_threshold <= 0
        or animal_verdict != "CLEAR"
    ):
        raise BackgroundPoolError("ANIMAL_SAFETY_EVIDENCE_REQUIRED")
    policy = normalized_safety_policy(
        animal_detector=animal_detector,
        animal_classes=normalized_classes,
        animal_threshold=animal_threshold,
    )
    policy_fingerprint = compute_safety_policy_fingerprint(policy)
    return VerifiedBackgroundRecord(
        provider=provider,
        provider_asset_id=provider_asset_id,
        provider_variant_id=provider_variant_id,
        source_page_url=str(candidate.source_page_url),
        query=candidate.query,
        width=candidate.file_width,
        height=candidate.file_height,
        duration_seconds=candidate.duration_seconds,
        fps=candidate.fps,
        mime_type=candidate.file_type,
        declared_file_size=candidate.declared_file_size_bytes,
        downloaded_file_size=downloaded_file_size,
        content_sha256=content_sha256,
        verified_at=verified_at or utc_now_iso(),
        verification_tool_version=VERIFICATION_TOOL_VERSION,
        safety_policy=policy,
        safety=SafetyEvidence(
            scanner_version=SAFETY_SCANNER_VERSION,
            policy_fingerprint=policy_fingerprint,
            person_threshold=DEFAULT_PERSON_CONFIDENCE_THRESHOLD,
            face_threshold=DEFAULT_FACE_CONFIDENCE_THRESHOLD,
            sampling_fps=DEFAULT_SAMPLE_FPS,
            person_result="CLEAR",
            face_result="CLEAR",
            animal_detector=animal_detector,
            animal_classes=normalized_classes,
            animal_threshold=animal_threshold,
            animal_verdict=animal_verdict,
            scanned_frames=scanned_frames,
            full_source_scan=True,
            verdict="SAFE",
        ),
    )


def verify_current_safety_model_files(models_dir: Optional[str] = None) -> None:
    """Fail closed unless the locally loaded detector bytes match pinned policy hashes."""
    target = models_dir or DEFAULT_CACHE_DIR
    expected = {
        DEFAULT_PERSON_MODEL_FILENAME: PERSON_MODEL_SHA256,
        DEFAULT_FACE_MODEL_FILENAME: FACE_MODEL_SHA256,
        DEFAULT_ANIMAL_MODEL_FILENAME: ANIMAL_MODEL_SHA256,
    }
    for filename, expected_sha256 in expected.items():
        path = os.path.join(target, filename)
        if not os.path.isfile(path):
            raise BackgroundPoolError(f"SAFETY_MODEL_MISSING:{filename}")
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != expected_sha256:
            raise BackgroundPoolError(f"SAFETY_MODEL_HASH_MISMATCH:{filename}")
