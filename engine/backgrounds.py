"""Authentic moving-nature background video source providers.

Nature-query filtering reduces risk but does not verify absence of people or faces.
Production use requires engine/safety.py visual scanning.
"""

from __future__ import annotations

import json
import hashlib
import os
import random
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Union


PEXELS_API_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_API_VIDEO_URL = "https://api.pexels.com/videos/videos/{video_id}"
PIXABAY_API_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_API_CACHE_SECONDS = 24 * 60 * 60
DEFAULT_ORIENTATION = "portrait"
DEFAULT_SIZE = "medium"
DEFAULT_LOCALE = "en-US"
DEFAULT_PER_PAGE = 15
DEFAULT_MIN_SOURCE_DURATION = 4.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60.0
DEFAULT_USER_AGENT = "QuranReelsAutomation/1.0"
DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_SOURCE_DURATION_RATIO = 1.75
DEFAULT_MAX_SOURCE_DURATION_SLACK_SECONDS = 10.0
TARGET_VERTICAL_WIDTH = 1080
TARGET_VERTICAL_HEIGHT = 1920
PEXELS_LANDSCAPE_SEARCH_SIZE = "large"
PEXELS_DISCOVERY_ORIENTATION_CYCLE: tuple[str, ...] = (
    "portrait",
    "portrait",
    "portrait",
    "portrait",
    "square",
    "landscape",
)

# Target Reel aspect ratio (9:16)
TARGET_ASPECT_RATIO = 9.0 / 16.0  # 0.5625

# Controlled allow-list of nature categories designed to minimize human presence.
# Broad and specific terms are interleaved so either group receives useful
# exposure even when a discovery run reaches its candidate budget early.
CORE_NATURE_QUERIES: tuple[str, ...] = (
    "nature",
    "nature video",
    "landscape",
    "forest",
    "sky",
    "clouds",
    "rain",
    "ocean",
    "sea",
    "sun",
    "sunset",
    "sunrise",
    "waterfall",
    "river",
    "mountains",
    "mountain",
    "space",
    "stars",
    "fog",
    "mist",
    "flowers",
    "beach",
    "waves",
)

SPECIFIC_NATURE_QUERIES: tuple[str, ...] = (
    "moving clouds",
    "sky clouds",
    "rain window nature",
    "rainfall",
    "ocean waves",
    "sea waves",
    "waterfall",
    "river water",
    "mountain landscape",
    "mountain clouds",
    "sunset sky",
    "sunrise sky",
    "natural texture",
    "water texture",
    "abstract nature",
    "space stars",
    "forest landscape",
    "landscape nature",
    "flowers macro",
    "beach waves",
    "fog mist nature",
)


def interleave_nature_query_groups(
    core_queries: Sequence[str] = CORE_NATURE_QUERIES,
    specific_queries: Sequence[str] = SPECIFIC_NATURE_QUERIES,
) -> tuple[str, ...]:
    """Return normalized, deduplicated query groups in deterministic rotation order."""
    normalized_groups = [
        tuple(" ".join(str(query).strip().lower().split()) for query in group)
        for group in (core_queries, specific_queries)
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    max_group_size = max((len(group) for group in normalized_groups), default=0)
    for index in range(max_group_size):
        for group in normalized_groups:
            if index >= len(group):
                continue
            query = group[index]
            if query and query not in seen:
                ordered.append(query)
                seen.add(query)
    return tuple(ordered)


# One semantic owner per query. Equal category scheduling is independent of yield.
BACKGROUND_SEARCH_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("forest_trees_autumn", ("forest", "trees", "autumn", "autumn forest", "fall forest",
        "woodland", "leaves", "fall foliage", "forest path", "forest landscape",
        "autumn landscape", "autumn trees", "misty forest", "forest nature")),
    ("mountains", ("mountains", "mountain", "mountain landscape", "mountain clouds",
        "mountain mist", "mountain sunset", "mountain sunrise", "mountain forest",
        "alpine nature", "mountain nature")),
    ("sky_sunset_clouds", ("sky", "sunset", "sunrise", "clouds", "moving clouds",
        "sky clouds", "sunset sky", "sunrise sky", "sun", "cloudscape")),
    ("water_ocean_sea", ("water", "ocean", "sea", "beach", "waves", "ocean waves",
        "sea waves", "river", "river water", "waterfall", "water texture", "beach waves")),
    ("nature_landscape", ("nature", "nature video", "landscape", "landscape nature",
        "natural scenery", "natural texture", "abstract nature", "flowers", "flowers macro")),
    ("fog_mist", ("fog", "mist", "fog mist nature", "misty landscape", "rain",
        "rainfall", "rain window nature")),
    ("space_stars", ("space", "stars", "space stars")),
)

ALLOWED_NATURE_QUERIES: tuple[str, ...] = tuple(
    query for _, queries in BACKGROUND_SEARCH_CATEGORIES for query in queries
)

# Prohibited subject terms to proactively block queries likely featuring people or non-nature subjects
PROHIBITED_QUERY_TERMS: tuple[str, ...] = (
    "people",
    "person",
    "portrait",
    "family",
    "tourist",
    "hiker",
    "runner",
    "crowd",
    "street",
    "market",
    "city",
    "mosque",
    "kaaba",
    "pilgrims",
    "man",
    "woman",
    "child",
    "boy",
    "girl",
    "face",
)


class BackgroundSourceError(ValueError):
    """Raised when background search, candidate parsing, or downloading fails."""


@dataclass(frozen=True)
class BackgroundSearchConfig:
    """Configuration for querying Pexels moving nature video candidates."""

    allowed_queries: tuple[str, ...] = ALLOWED_NATURE_QUERIES
    per_page: int = DEFAULT_PER_PAGE
    orientation: str = DEFAULT_ORIENTATION
    size: str = DEFAULT_SIZE
    locale: str = DEFAULT_LOCALE
    minimum_source_duration_seconds: float = DEFAULT_MIN_SOURCE_DURATION
    min_width: int = 1080
    min_height: int = 1920


@dataclass(frozen=True)
class BackgroundCandidate:
    """A technically validated Pexels nature video candidate.

    NOTE: Nature-query filtering reduces risk but does NOT verify the absence of
    people or faces. Production use requires engine/safety.py visual scanning.
    """

    pexels_video_id: int
    query: str
    pexels_page_url: str
    videographer_name: str
    videographer_url: str
    duration_seconds: float
    source_width: int
    source_height: int
    video_file_id: Union[int, str]
    file_url: str
    file_type: str
    fps: Optional[float]
    file_width: int
    file_height: int
    requires_center_crop: bool
    human_safety_verified: bool = False
    declared_file_size_bytes: Optional[int] = None
    search_page: int = 1
    provider: str = "pexels"
    provider_variant_name: str = ""

    @property
    def provider_asset_id(self) -> str:
        return str(self.pexels_video_id)

    @property
    def provider_variant_id(self) -> str:
        if self.provider_variant_name:
            return self.provider_variant_name
        return str(self.video_file_id)

    @property
    def source_page_url(self) -> str:
        return self.pexels_page_url

    @property
    def author_name(self) -> str:
        return self.videographer_name

    @property
    def author_url(self) -> str:
        return self.videographer_url


@dataclass(frozen=True)
class DownloadedBackground:
    """A downloaded background video file on local disk ready for CV safety scanning."""

    candidate: BackgroundCandidate
    local_path: str
    file_size_bytes: int
    content_sha256: str = ""


@dataclass(frozen=True)
class MetadataIneligibleCandidate:
    provider: str
    provider_asset_id: str
    provider_variant_id: str
    query: str
    reason: str


@dataclass(frozen=True)
class BackgroundDiscoveryPage:
    provider: str
    query: str
    page: int
    discovered: int
    candidates: tuple[BackgroundCandidate, ...]
    metadata_ineligible: tuple[MetadataIneligibleCandidate, ...]


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def resolve_pexels_api_key(api_key: Optional[str] = None) -> str:
    """Resolve Pexels API key from argument or server environment (PEXELS_API_KEY / PEXELS_API_KEYS)."""
    if api_key and str(api_key).strip():
        return str(api_key).strip()

    key_from_env = os.getenv("PEXELS_API_KEY")
    if key_from_env and key_from_env.strip():
        return key_from_env.strip()

    # Backwards-compatible legacy fallback
    legacy_keys = os.getenv("PEXELS_API_KEYS")
    if legacy_keys and legacy_keys.strip():
        raw = legacy_keys.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for k in parsed:
                        if isinstance(k, str) and k.strip():
                            return k.strip()
            except Exception:
                pass
        parts = [k.strip() for k in raw.split(",") if k.strip()]
        if parts:
            return parts[0]

    raise BackgroundSourceError(
        "Missing Pexels API key. Set PEXELS_API_KEY environment variable."
    )


def resolve_pixabay_api_key(api_key: Optional[str] = None) -> str:
    """Resolve the server-only official Pixabay API key."""
    value = str(api_key or os.getenv("PIXABAY_API_KEY", "")).strip()
    if not value:
        raise BackgroundSourceError(
            "Missing Pixabay API key. Set PIXABAY_API_KEY environment variable."
        )
    return value


def validate_nature_query(
    query: str,
    allowed_queries: Sequence[str] = ALLOWED_NATURE_QUERIES,
) -> str:
    """Validate that the query conforms to the controlled nature allow-list and has no prohibited terms."""
    if not isinstance(query, str) or not query.strip():
        raise BackgroundSourceError("Background search query must be a non-empty string.")

    cleaned = " ".join(query.strip().lower().split())

    for term in PROHIBITED_QUERY_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", cleaned):
            raise BackgroundSourceError(
                f"Search query contains prohibited subject term '{term}'."
            )

    allowed_set = {q.strip().lower() for q in allowed_queries}
    if cleaned not in allowed_set:
        raise BackgroundSourceError(
            f"Query '{cleaned}' is not in the controlled nature query allow-list."
        )

    return cleaned


def can_produce_native_vertical_crop(
    width: int,
    height: int,
    target_width: int = TARGET_VERTICAL_WIDTH,
    target_height: int = TARGET_VERTICAL_HEIGHT,
) -> bool:
    """Return whether the raster contains the target crop without upscaling.

    The largest centered crop with the target aspect ratio uses the full source
    height when the source is wider, or the full source width when it is
    narrower.  Both resulting crop dimensions must meet the output raster.
    """
    values = (width, height, target_width, target_height)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in values
    ):
        return False
    target_aspect = target_width / target_height
    source_aspect = width / height
    if source_aspect >= target_aspect:
        crop_width = height * target_aspect
        crop_height = float(height)
    else:
        crop_width = float(width)
        crop_height = width / target_aspect
    return crop_width >= target_width and crop_height >= target_height


def _score_and_rank_video_file(
    file_item: dict,
    min_width: int = 1080,
    min_height: int = 1920,
) -> Optional[tuple[int, float, int, int, int, dict, bool]]:
    """Score a single video file dict. Lower rank score is better.

    Returns a quality/cost rank plus the file item, or None if ineligible.
    Portrait is preferred, followed by square, then sufficiently large
    landscape. Every accepted raster can yield a native target crop.
    """
    if not isinstance(file_item, dict):
        return None

    link = file_item.get("link")
    if not isinstance(link, str) or not link.startswith("https://"):
        return None

    file_type = file_item.get("file_type", "")
    if not isinstance(file_type, str) or file_type.lower() != "video/mp4":
        # Also accept .mp4 extension if file_type is missing/generic
        if not link.lower().endswith(".mp4") and "mp4" not in file_type.lower():
            return None

    try:
        width = int(file_item.get("width", 0))
        height = int(file_item.get("height", 0))
    except (TypeError, ValueError):
        return None

    if width <= 0 or height <= 0:
        return None

    if not can_produce_native_vertical_crop(width, height, min_width, min_height):
        return None

    aspect_ratio = width / height
    diff_from_target = abs(aspect_ratio - TARGET_ASPECT_RATIO)
    requires_crop = diff_from_target > 0.01

    is_portrait = height > width
    is_square = 0.90 <= aspect_ratio <= 1.10

    # Quality tiers are considered before decode/download cost. Within one
    # quality tier, prefer the smallest sufficient raster and then an API-
    # declared smaller file.
    if width == 1080 and height == 1920:
        tier = 0
    elif not requires_crop:
        tier = 1
    elif is_portrait:
        tier = 2
    elif is_square:
        tier = 3
    else:
        tier = 4

    declared_size = 0
    for key in ("file_size", "size"):
        raw_size = file_item.get(key)
        if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size > 0:
            declared_size = raw_size
            break
    pixel_area = width * height
    unknown_size = 1 if declared_size == 0 else 0

    return (
        tier,
        diff_from_target,
        pixel_area,
        unknown_size,
        declared_size,
        file_item,
        requires_crop,
    )


def _select_smallest_sufficient_variant(
    ranked_files: Sequence[tuple[int, float, int, int, int, dict, bool]],
    max_file_size_bytes: int = DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES,
) -> Optional[tuple[int, float, int, int, int, dict, bool]]:
    """Choose the cheapest variant in the best orientation/quality tier.

    A known in-limit encode is preferred over an oversized one. If every
    technically valid variant is oversized, return the cheapest one so the
    shared cost guard can record the precise metadata rejection reason.
    """
    if not ranked_files:
        return None
    ordered = sorted(
        ranked_files,
        key=lambda ranked: (
            ranked[0],  # orientation / quality tier
            ranked[2],  # pixel area
            ranked[3],  # known size before unknown size
            ranked[4],  # declared bytes
            ranked[1],  # aspect difference as final tie-breaker
        ),
    )
    within_limit = [
        ranked
        for ranked in ordered
        if ranked[4] == 0 or ranked[4] <= max_file_size_bytes
    ]
    return (within_limit or ordered)[0]


def background_candidate_runtime_rank(
    candidate: BackgroundCandidate,
    required_duration_seconds: float,
) -> tuple[int, int, float, int, int, int, float]:
    """Rank safe-to-scan candidates by fit, visual suitability, and cost."""
    duration_shortfall = max(0.0, required_duration_seconds - candidate.duration_seconds)
    duration_fit = 0 if duration_shortfall == 0.0 else 1
    aspect_diff = abs((candidate.file_width / candidate.file_height) - TARGET_ASPECT_RATIO)
    if candidate.file_width == 1080 and candidate.file_height == 1920:
        quality_tier = 0
    elif not candidate.requires_center_crop:
        quality_tier = 1
    elif candidate.file_height > candidate.file_width:
        quality_tier = 2
    elif 0.90 <= candidate.file_width / candidate.file_height <= 1.10:
        quality_tier = 3
    else:
        quality_tier = 4
    declared_size = candidate.declared_file_size_bytes or 0
    unknown_size = 1 if declared_size == 0 else 0
    pixel_area = candidate.file_width * candidate.file_height
    # Full source duration determines the mandatory 4-FPS scan workload.
    duration_cost = candidate.duration_seconds if duration_fit == 0 else duration_shortfall
    return (
        duration_fit,
        quality_tier,
        aspect_diff,
        pixel_area,
        unknown_size,
        declared_size,
        duration_cost,
    )


def maximum_practical_source_duration(required_duration_seconds: float) -> float:
    """Return the hard source-duration guard for one continuous Reel clip."""
    return max(
        required_duration_seconds * DEFAULT_MAX_SOURCE_DURATION_RATIO,
        required_duration_seconds + DEFAULT_MAX_SOURCE_DURATION_SLACK_SECONDS,
    )


def candidate_cost_rejection_reason(
    candidate: BackgroundCandidate,
    required_duration_seconds: float,
    max_file_size_bytes: int = DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES,
    require_sufficient_duration: bool = True,
    maximum_source_duration_seconds: Optional[float] = None,
) -> Optional[str]:
    """Reject metadata-proven pathological scan cost before any download."""
    file_type = str(candidate.file_type or "").lower()
    if file_type != "video/mp4" and not candidate.file_url.lower().endswith(".mp4"):
        return "unsupported_video_format"
    if not can_produce_native_vertical_crop(
        candidate.file_width,
        candidate.file_height,
    ):
        return "insufficient_native_crop_resolution"
    if require_sufficient_duration and candidate.duration_seconds < required_duration_seconds:
        return "insufficient_duration"
    maximum_duration = (
        float(maximum_source_duration_seconds)
        if maximum_source_duration_seconds is not None
        else maximum_practical_source_duration(required_duration_seconds)
    )
    if candidate.duration_seconds > maximum_duration:
        return "source_duration_too_long"
    if (
        candidate.declared_file_size_bytes is not None
        and candidate.declared_file_size_bytes > max_file_size_bytes
    ):
        return "source_file_too_large"
    return None


def parse_pexels_video_item(
    item: dict,
    query: str,
    minimum_source_duration: float = DEFAULT_MIN_SOURCE_DURATION,
    min_width: int = 1080,
    min_height: int = 1920,
    search_page: int = 1,
) -> Optional[BackgroundCandidate]:
    """Parse and validate a single Pexels video item from search response.

    Returns BackgroundCandidate if valid and suitable, or None if unsuitable.
    """
    if not isinstance(item, dict):
        return None

    video_id_val = item.get("id")
    if not isinstance(video_id_val, int) or isinstance(video_id_val, bool) or video_id_val <= 0:
        return None

    try:
        duration = float(item.get("duration", 0.0))
    except (TypeError, ValueError):
        return None

    if duration < minimum_source_duration:
        return None

    page_url = item.get("url")
    if not isinstance(page_url, str) or not page_url.startswith("https://"):
        return None

    source_width = int(item.get("width", 0)) if isinstance(item.get("width"), int) else 0
    source_height = int(item.get("height", 0)) if isinstance(item.get("height"), int) else 0

    user_info = item.get("user")
    videographer_name = ""
    videographer_url = ""
    if isinstance(user_info, dict):
        videographer_name = str(user_info.get("name", "")).strip()
        videographer_url = str(user_info.get("url", "")).strip()

    video_files = item.get("video_files")
    if not isinstance(video_files, list) or not video_files:
        return None

    ranked_files = []
    for f in video_files:
        ranked = _score_and_rank_video_file(f, min_width=min_width, min_height=min_height)
        if ranked is not None:
            ranked_files.append(ranked)

    if not ranked_files:
        return None

    selected = _select_smallest_sufficient_variant(ranked_files)
    assert selected is not None
    _, _, _, _, declared_size, best_file, requires_crop = selected

    fps_val = best_file.get("fps")
    try:
        fps = float(fps_val) if fps_val is not None else None
    except (TypeError, ValueError):
        fps = None

    return BackgroundCandidate(
        pexels_video_id=video_id_val,
        query=query,
        pexels_page_url=page_url,
        videographer_name=videographer_name,
        videographer_url=videographer_url,
        duration_seconds=duration,
        source_width=source_width,
        source_height=source_height,
        video_file_id=int(best_file.get("id", 0)),
        file_url=best_file["link"],
        file_type=best_file.get("file_type", "video/mp4"),
        fps=fps,
        file_width=int(best_file.get("width", 0)),
        file_height=int(best_file.get("height", 0)),
        requires_center_crop=requires_crop,
        human_safety_verified=False,
        declared_file_size_bytes=declared_size or None,
        search_page=search_page,
    )


def search_background_candidates(
    query: str,
    api_key: Optional[str] = None,
    config: Optional[BackgroundSearchConfig] = None,
    page: int = 1,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 1,
    include_discovery_accounting: bool = False,
) -> Union[tuple[BackgroundCandidate, ...], BackgroundDiscoveryPage]:
    """Search one controlled Pexels orientation lane for nature video candidates."""
    resolved_config = config or BackgroundSearchConfig()
    validated_query = validate_nature_query(query, resolved_config.allowed_queries)
    resolved_key = resolve_pexels_api_key(api_key)

    if page < 1:
        raise BackgroundSourceError(f"Page number {page} must be >= 1.")

    orientation = str(resolved_config.orientation).strip().lower()
    if orientation not in ("portrait", "square", "landscape"):
        raise BackgroundSourceError(f"Unsupported Pexels orientation '{orientation}'.")
    request_size = (
        PEXELS_LANDSCAPE_SEARCH_SIZE
        if orientation == "landscape"
        else resolved_config.size
    )
    params = {
        "query": validated_query,
        "orientation": orientation,
        "size": request_size,
        "locale": resolved_config.locale,
        "per_page": str(resolved_config.per_page),
        "page": str(page),
    }

    url = f"{PEXELS_API_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    ssl_context = _build_ssl_context()

    attempts = max(1, max_retries + 1)
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": resolved_key,
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                status_code = getattr(resp, "status", None) or resp.getcode()
                if status_code != 200:
                    raise BackgroundSourceError(f"Pexels search returned HTTP status {status_code}.")
                body_bytes = resp.read()
                break
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code == 429:
                raise BackgroundSourceError("Pexels API rate limit exceeded (HTTP 429).") from err
            if err.code in (401, 403):
                raise BackgroundSourceError(f"Pexels API authentication failed with HTTP {err.code}.") from err
            if err.code in (400, 404):
                raise BackgroundSourceError(f"Pexels API request failed with HTTP {err.code}: {err.reason}.") from err
            if err.code < 500 or attempt == attempts - 1:
                raise BackgroundSourceError(f"Pexels API HTTP error {err.code}: {err.reason}.") from err
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, ssl.SSLError) as err:
            last_error = err
            if attempt == attempts - 1:
                raise BackgroundSourceError(f"Network error searching Pexels: {err}") from err
        except Exception as err:
            raise BackgroundSourceError(f"Unexpected error searching Pexels: {err}") from err

        time.sleep(0.5)
    else:
        raise BackgroundSourceError(f"Failed to search Pexels at {url}: {last_error}")

    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except Exception as err:
        raise BackgroundSourceError(f"Malformed JSON response from Pexels: {err}") from err

    if not isinstance(data, dict):
        raise BackgroundSourceError("Pexels API response must be a JSON object.")

    videos_list = data.get("videos")
    if not isinstance(videos_list, list):
        raise BackgroundSourceError("Missing or invalid 'videos' list in Pexels API response.")

    candidates: list[BackgroundCandidate] = []
    ineligible: list[MetadataIneligibleCandidate] = []
    for item in videos_list:
        cand = parse_pexels_video_item(
            item,
            query=validated_query,
            minimum_source_duration=resolved_config.minimum_source_duration_seconds,
            min_width=resolved_config.min_width,
            min_height=resolved_config.min_height,
            search_page=page,
        )
        if cand is not None:
            candidates.append(cand)
        elif isinstance(item, dict) and isinstance(item.get("id"), int):
            try:
                duration = float(item.get("duration", 0.0))
            except (TypeError, ValueError):
                duration = 0.0
            reason = (
                "INSUFFICIENT_DURATION"
                if duration < resolved_config.minimum_source_duration_seconds
                else "INSUFFICIENT_NATIVE_CROP_RESOLUTION_OR_VARIANT"
            )
            ineligible.append(MetadataIneligibleCandidate(
                provider="pexels",
                provider_asset_id=str(item["id"]),
                provider_variant_id="metadata",
                query=validated_query,
                reason=reason,
            ))

    if include_discovery_accounting:
        return BackgroundDiscoveryPage(
            provider="pexels",
            query=validated_query,
            page=page,
            discovered=len(videos_list),
            candidates=tuple(candidates),
            metadata_ineligible=tuple(ineligible),
        )
    return tuple(candidates)


def fetch_pexels_video_asset(
    video_id: int,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict:
    """Fetch one current Pexels video asset by its stable provider ID."""
    if not isinstance(video_id, int) or isinstance(video_id, bool) or video_id <= 0:
        raise BackgroundSourceError("Pexels video_id must be a positive integer.")
    resolved_key = resolve_pexels_api_key(api_key)
    url = PEXELS_API_VIDEO_URL.format(video_id=video_id)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": resolved_key,
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_build_ssl_context()) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            if status_code != 200:
                raise BackgroundSourceError(
                    f"Pexels asset lookup returned HTTP status {status_code}."
                )
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise BackgroundSourceError(f"PEXELS_ASSET_MISSING:{video_id}") from err
        if err.code in (401, 403):
            raise BackgroundSourceError(
                f"Pexels API authentication failed with HTTP {err.code}."
            ) from err
        raise BackgroundSourceError(f"Pexels asset lookup failed with HTTP {err.code}.") from err
    except BackgroundSourceError:
        raise
    except Exception as err:
        raise BackgroundSourceError(f"Pexels asset lookup failed: {err}") from err

    if not isinstance(payload, dict) or payload.get("id") != video_id:
        raise BackgroundSourceError("Pexels asset response identity did not match the requested video ID.")
    return payload


def resolve_pexels_video_variant(
    asset: dict,
    video_file_id: int,
    *,
    query: str = "verified pool",
) -> BackgroundCandidate:
    """Resolve the exact current Pexels file variant previously safety-verified."""
    if not isinstance(asset, dict):
        raise BackgroundSourceError("Pexels asset must be an object.")
    files = asset.get("video_files")
    if not isinstance(files, list):
        raise BackgroundSourceError("Pexels asset is missing video_files.")
    exact = next(
        (
            item
            for item in files
            if isinstance(item, dict)
            and item.get("id") == video_file_id
        ),
        None,
    )
    if exact is None:
        raise BackgroundSourceError(f"PEXELS_VARIANT_MISSING:{video_file_id}")
    exact_asset = dict(asset)
    exact_asset["video_files"] = [exact]
    candidate = parse_pexels_video_item(
        exact_asset,
        query=query,
        minimum_source_duration=0.0,
    )
    if candidate is None or candidate.video_file_id != video_file_id:
        raise BackgroundSourceError(
            f"PEXELS_VARIANT_UNSUITABLE:{video_file_id}"
        )
    return candidate


def _pixabay_cache_path(cache_dir: str, request_identity: str) -> str:
    digest = hashlib.sha256(request_identity.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{digest}.json")


def _read_pixabay_cache(cache_path: str, max_age_seconds: int) -> Optional[dict]:
    try:
        if time.time() - os.path.getmtime(cache_path) > max_age_seconds:
            return None
        with open(cache_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_pixabay_cache(cache_path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=os.path.dirname(cache_path), prefix=".pixabay-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, cache_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def parse_pixabay_video_item(
    item: dict,
    query: str,
    minimum_source_duration: float = DEFAULT_MIN_SOURCE_DURATION,
    min_width: int = 1080,
    min_height: int = 1920,
    search_page: int = 1,
) -> Optional[BackgroundCandidate]:
    """Parse one official Pixabay video result into the shared candidate type."""
    if not isinstance(item, dict):
        return None
    asset_id = item.get("id")
    if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
        return None
    try:
        duration = float(item.get("duration", 0.0))
    except (TypeError, ValueError):
        return None
    if duration < minimum_source_duration:
        return None
    page_url = item.get("pageURL")
    if not isinstance(page_url, str) or not page_url.startswith("https://pixabay.com/"):
        return None
    videos = item.get("videos")
    if not isinstance(videos, dict):
        return None

    ranked: list[tuple[int, float, int, int, int, dict, bool, str]] = []
    source_width = 0
    source_height = 0
    for variant_name, raw in videos.items():
        if not isinstance(variant_name, str) or not isinstance(raw, dict):
            continue
        try:
            width = int(raw.get("width", 0))
            height = int(raw.get("height", 0))
        except (TypeError, ValueError):
            continue
        source_width = max(source_width, width)
        source_height = max(source_height, height)
        normalized = {
            "link": raw.get("url"),
            "width": width,
            "height": height,
            "size": raw.get("size"),
            "file_type": "video/mp4",
        }
        scored = _score_and_rank_video_file(normalized, min_width=min_width, min_height=min_height)
        if scored is not None:
            ranked.append((*scored, variant_name))
    if not ranked:
        return None
    selected = _select_smallest_sufficient_variant(
        tuple(value[:7] for value in ranked),
    )
    assert selected is not None
    selected_file = selected[5]
    selected_name = next(
        value[7]
        for value in ranked
        if value[5] is selected_file
    )
    _, _, _, _, declared_size, best, requires_crop = selected
    variant_name = selected_name
    width = int(best["width"])
    height = int(best["height"])
    variant_id = f"{variant_name}:{width}x{height}"
    author_name = str(item.get("user", "")).strip()
    author_id = item.get("user_id")
    author_url = ""
    if author_name and isinstance(author_id, int) and author_id > 0:
        slug = urllib.parse.quote(author_name.lower().replace(" ", "-"), safe="-")
        author_url = f"https://pixabay.com/users/{slug}-{author_id}/"
    return BackgroundCandidate(
        pexels_video_id=asset_id,
        query=query,
        pexels_page_url=page_url,
        videographer_name=author_name,
        videographer_url=author_url,
        duration_seconds=duration,
        source_width=source_width,
        source_height=source_height,
        video_file_id=variant_id,
        file_url=str(best["link"]),
        file_type="video/mp4",
        fps=None,
        file_width=width,
        file_height=height,
        requires_center_crop=requires_crop,
        human_safety_verified=False,
        declared_file_size_bytes=declared_size or None,
        search_page=search_page,
        provider="pixabay",
        provider_variant_name=variant_id,
    )


def search_pixabay_background_candidates(
    query: str,
    api_key: Optional[str] = None,
    config: Optional[BackgroundSearchConfig] = None,
    page: int = 1,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_dir: Optional[str] = None,
    include_discovery_accounting: bool = False,
) -> Union[tuple[BackgroundCandidate, ...], BackgroundDiscoveryPage]:
    """Search the official Pixabay video API with mandatory 24-hour caching."""
    resolved_config = config or BackgroundSearchConfig(per_page=30)
    validated_query = validate_nature_query(query, resolved_config.allowed_queries)
    if page < 1:
        raise BackgroundSourceError("Pixabay page must be >= 1.")
    resolved_key = resolve_pixabay_api_key(api_key)
    public_params = {
        "q": validated_query,
        "category": "nature",
        "lang": "en",
        "video_type": "all",
        "safesearch": "true",
        "order": "popular",
        "page": str(page),
        "per_page": str(max(3, min(200, resolved_config.per_page))),
        "min_width": str(resolved_config.min_width),
        "min_height": str(resolved_config.min_height),
    }
    request_identity = urllib.parse.urlencode(sorted(public_params.items()))
    resolved_cache_dir = cache_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".local", "pixabay-api-cache",
    )
    cache_path = _pixabay_cache_path(resolved_cache_dir, request_identity)
    payload = _read_pixabay_cache(cache_path, PIXABAY_API_CACHE_SECONDS)
    if payload is None:
        params = {"key": resolved_key, **public_params}
        request = urllib.request.Request(
            f"{PIXABAY_API_VIDEO_URL}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_build_ssl_context()) as response:
                if (getattr(response, "status", None) or response.getcode()) != 200:
                    raise BackgroundSourceError("Pixabay search returned a non-200 response.")
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 429:
                raise BackgroundSourceError("Pixabay API rate limit exceeded (HTTP 429).") from err
            if err.code in (400, 401, 403):
                raise BackgroundSourceError(f"Pixabay API authentication/request failed with HTTP {err.code}.") from err
            raise BackgroundSourceError(f"Pixabay API search failed with HTTP {err.code}.") from err
        except BackgroundSourceError:
            raise
        except Exception as err:
            raise BackgroundSourceError(
                f"Pixabay API search failed ({type(err).__name__})."
            ) from err
        if not isinstance(payload, dict):
            raise BackgroundSourceError("Pixabay API response must be a JSON object.")
        _write_pixabay_cache(cache_path, payload)
    hits = payload.get("hits")
    if not isinstance(hits, list):
        raise BackgroundSourceError("Pixabay API response is missing hits.")
    candidates = tuple(
        candidate
        for item in hits
        if (candidate := parse_pixabay_video_item(
            item,
            query=validated_query,
            minimum_source_duration=resolved_config.minimum_source_duration_seconds,
            min_width=resolved_config.min_width,
            min_height=resolved_config.min_height,
            search_page=page,
        )) is not None
    )
    if not include_discovery_accounting:
        return candidates
    eligible_ids = {candidate.provider_asset_id for candidate in candidates}
    ineligible: list[MetadataIneligibleCandidate] = []
    for item in hits:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        asset_id = str(item["id"])
        if asset_id in eligible_ids:
            continue
        try:
            duration = float(item.get("duration", 0.0))
        except (TypeError, ValueError):
            duration = 0.0

        if duration < resolved_config.minimum_source_duration_seconds:
            reason = "INSUFFICIENT_DURATION"
        else:
            reason = "INSUFFICIENT_NATIVE_CROP_RESOLUTION_OR_VARIANT"
        ineligible.append(MetadataIneligibleCandidate(
            provider="pixabay",
            provider_asset_id=asset_id,
            provider_variant_id="metadata",
            query=validated_query,
            reason=reason,
        ))
    return BackgroundDiscoveryPage(
        provider="pixabay",
        query=validated_query,
        page=page,
        discovered=len(hits),
        candidates=candidates,
        metadata_ineligible=tuple(ineligible),
    )


def fetch_pixabay_video_asset(
    asset_id: int,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_dir: Optional[str] = None,
) -> dict:
    """Fetch one current Pixabay video through the official API by stable ID."""
    if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
        raise BackgroundSourceError("Pixabay asset ID must be a positive integer.")
    resolved_key = resolve_pixabay_api_key(api_key)
    public_params = {"id": str(asset_id)}
    resolved_cache_dir = cache_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".local", "pixabay-api-cache",
    )
    cache_path = _pixabay_cache_path(
        resolved_cache_dir, "asset:" + urllib.parse.urlencode(public_params),
    )
    payload = _read_pixabay_cache(cache_path, PIXABAY_API_CACHE_SECONDS)
    params = {"key": resolved_key, **public_params}
    request = urllib.request.Request(
        f"{PIXABAY_API_VIDEO_URL}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    if payload is None:
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_build_ssl_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as err:
            raise BackgroundSourceError(
                f"Pixabay asset lookup failed ({type(err).__name__})."
            ) from err
        if not isinstance(payload, dict):
            raise BackgroundSourceError("Pixabay asset response must be a JSON object.")
        _write_pixabay_cache(cache_path, payload)
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list) or len(hits) != 1 or hits[0].get("id") != asset_id:
        raise BackgroundSourceError(f"PIXABAY_ASSET_MISSING:{asset_id}")
    return dict(hits[0])


def resolve_pixabay_video_variant(
    asset: dict,
    variant_id: str,
    *,
    query: str = "verified pool",
) -> BackgroundCandidate:
    """Resolve the exact Pixabay variant identity recorded by V2."""
    candidate = parse_pixabay_video_item(asset, query=query, minimum_source_duration=0.0)
    if candidate is not None and candidate.provider_variant_id == str(variant_id):
        return candidate
    videos = asset.get("videos") if isinstance(asset, dict) else None
    if not isinstance(videos, dict):
        raise BackgroundSourceError(f"PIXABAY_VARIANT_MISSING:{variant_id}")
    for name, raw in videos.items():
        if not isinstance(raw, dict):
            continue
        identity = f"{name}:{raw.get('width')}x{raw.get('height')}"
        if identity != str(variant_id):
            continue
        exact = dict(asset)
        exact["videos"] = {name: raw}
        resolved = parse_pixabay_video_item(exact, query=query, minimum_source_duration=0.0)
        if resolved is not None and resolved.provider_variant_id == str(variant_id):
            return resolved
    raise BackgroundSourceError(f"PIXABAY_VARIANT_MISSING:{variant_id}")


def select_background_candidate(
    candidates: Sequence[BackgroundCandidate],
    rng: Optional[random.Random] = None,
    recent_video_ids: Optional[Sequence[int]] = None,
) -> BackgroundCandidate:
    """Select a candidate deterministically using rng while excluding recently used video IDs."""
    if not candidates:
        raise BackgroundSourceError("No background candidates provided for selection.")

    recent_set = set(recent_video_ids or ())
    eligible = [c for c in candidates if c.pexels_video_id not in recent_set]

    if not eligible:
        raise BackgroundSourceError(
            f"All {len(candidates)} candidate(s) were excluded by recent video ID history."
        )

    chooser = rng or random
    return chooser.choice(eligible)


def download_background(
    candidate: BackgroundCandidate,
    target_dir: Optional[str] = None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    buffer_size: int = 64 * 1024,
    max_file_size_bytes: Optional[int] = DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES,
    abort_check: Optional[Callable[[], bool]] = None,
) -> DownloadedBackground:
    """Stream and download a background candidate video to local disk.

    Returns DownloadedBackground with local file path and byte size.
    """
    if not isinstance(candidate, BackgroundCandidate):
        raise BackgroundSourceError("Invalid candidate provided to download_background.")

    if not candidate.file_url.startswith("https://"):
        raise BackgroundSourceError("Background candidate download URL must be HTTPS.")
    if (
        max_file_size_bytes is not None
        and (
            isinstance(max_file_size_bytes, bool)
            or not isinstance(max_file_size_bytes, int)
            or max_file_size_bytes <= 0
        )
    ):
        raise BackgroundSourceError("max_file_size_bytes must be a positive integer or None.")

    dest_dir = target_dir or tempfile.gettempdir()
    os.makedirs(dest_dir, exist_ok=True)

    safe_variant = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate.provider_variant_id))
    filename = f"{candidate.provider}_{candidate.provider_asset_id}_{safe_variant}.mp4"
    dest_path = os.path.join(dest_dir, filename)

    ssl_context = _build_ssl_context()
    req = urllib.request.Request(
        candidate.file_url,
        headers={
            "User-Agent": user_agent,
        },
    )

    total_bytes = 0
    content_hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            status_code = getattr(resp, "status", None) or resp.getcode()
            if status_code != 200:
                raise BackgroundSourceError(f"Download HTTP request returned status {status_code}.")

            content_type = resp.headers.get("Content-Type", "").lower()
            if content_type and ("text/html" in content_type or "application/json" in content_type):
                raise BackgroundSourceError(f"Unexpected non-video Content-Type '{content_type}' on download.")

            content_length_raw = resp.headers.get("Content-Length")
            try:
                content_length = int(content_length_raw) if content_length_raw else None
            except (TypeError, ValueError):
                content_length = None
            if (
                max_file_size_bytes is not None
                and content_length is not None
                and content_length > max_file_size_bytes
            ):
                raise BackgroundSourceError(
                    "Background source exceeds the maximum permitted download size "
                    f"({content_length} > {max_file_size_bytes} bytes)."
                )

            with open(dest_path, "wb") as f_out:
                while True:
                    if abort_check is not None and abort_check():
                        raise BackgroundSourceError("BACKGROUND_DOWNLOAD_ABORTED")
                    chunk = resp.read(buffer_size)
                    if not chunk:
                        break
                    f_out.write(chunk)
                    total_bytes += len(chunk)
                    content_hasher.update(chunk)
                    if (
                        max_file_size_bytes is not None
                        and total_bytes > max_file_size_bytes
                    ):
                        raise BackgroundSourceError(
                            "Background source exceeded the maximum permitted download size "
                            f"while streaming ({total_bytes} > {max_file_size_bytes} bytes)."
                        )

        if total_bytes <= 0:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise BackgroundSourceError("Downloaded video file is empty (0 bytes).")

    except Exception as err:
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        if isinstance(err, BackgroundSourceError):
            raise
        raise BackgroundSourceError(f"Failed to download background video: {err}") from err

    return DownloadedBackground(
        candidate=candidate,
        local_path=dest_path,
        file_size_bytes=total_bytes,
        content_sha256=content_hasher.hexdigest(),
    )
