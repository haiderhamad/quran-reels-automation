"""Shared metadata-only background discovery and lease-backed qualification.

This module deliberately has one queue for GitHub and optional Mac workers.
It never stores source bytes: source safety evidence remains in the existing
verified background pool repository.
"""

from __future__ import annotations

import os
import socket
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from inspect import Parameter, signature
from typing import Any, Callable, Mapping, Optional, Sequence

from engine.background_pool import (
    CURRENT_SAFETY_POLICY_FINGERPRINT,
    MetadataIneligibleRecord,
    SafetyRejectedRecord,
    TransientFailureRecord,
    build_verified_record,
    compute_eligibility_fingerprint,
    utc_now_iso,
)
from engine.background_pool_builder import (
    DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS,
    DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES,
    DEFAULT_POOL_MAXIMUM_DURATION_SECONDS,
    DEFAULT_POOL_MINIMUM_DURATION_SECONDS,
    _rejection_reason,
)
from engine.backgrounds import (
    BACKGROUND_SEARCH_CATEGORIES,
    BackgroundCandidate,
    BackgroundDiscoveryPage,
    PEXELS_DISCOVERY_ORIENTATION_CYCLE,
    can_produce_native_vertical_crop,
    candidate_cost_rejection_reason,
    download_background,
)
from engine.safety import (
    ANIMAL_DETECTION_CLASSES,
    ANIMAL_DETECTOR_ID,
    DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD,
    SafetyConfig,
    scan_video,
)
from engine.supabase_state import SupabaseStateStore


DISCOVERY_MAX_CANDIDATES_PER_RUN = 20
DISCOVERY_VERIFIED_TARGET = 100
BACKGROUND_SELECTABLE_TARGET = 100
DEFAULT_LEASE_SECONDS = 900
DISCOVERY_MAX_PAGE = 5
DISCOVERY_MAX_API_REQUESTS_PER_RUN = 80
DISCOVERY_MAX_METADATA_ITEMS_PER_RUN = 1000
DISCOVERY_MAX_SEARCH_WORKERS = 4


DISCOVERY_ROTATION_STATE_ID = "background-discovery-rotation-v1"


def get_rotated_queries(offset: Optional[int] = None, category: Optional[str] = None) -> tuple[str, ...]:
    from engine.backgrounds import ALLOWED_NATURE_QUERIES
    queries = dict(BACKGROUND_SEARCH_CATEGORIES).get(category, ALLOWED_NATURE_QUERIES)
    offset = (offset or 0) % len(queries)
    return queries[offset:] + queries[:offset]


def _load_discovery_rotation(store: Any, logger: Callable[[str], None]) -> dict[str, Any]:
    """Shared JSON builder checkpoint; unavailable reads never overwrite old state."""
    if not callable(getattr(store, "get_builder_run", None)):
        return {}
    try:
        row = store.get_builder_run(DISCOVERY_ROTATION_STATE_ID)
        value = row.get("value", {}) if isinstance(row, Mapping) else {}
        if not isinstance(value, dict):
            raise ValueError("Invalid discovery checkpoint")
        return value
    except Exception as error:
        logger(f"[BACKGROUND_DISCOVERY_STATE_UNAVAILABLE] error={type(error).__name__}")
        return {"unavailable": True}


def discovery_rotation_offset() -> int:
    """Advance each GitHub workflow run; local runs rotate by UTC day.

    A time-slot modulo alone aliases with the five-hour discovery schedule.
    GITHUB_RUN_NUMBER is monotonic for this workflow and stable on retries.
    """
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    if run_number.isdigit() and int(run_number) > 0:
        return int(run_number) - 1
    return int(time.time() // 86400)


def get_rotated_pages(offset: int) -> tuple[int, ...]:
    pages = tuple(range(1, DISCOVERY_MAX_PAGE + 1))
    start = offset % len(pages)
    return pages[start:] + pages[:start]


def candidate_metadata(candidate: BackgroundCandidate) -> dict[str, Any]:
    """Serialize only non-secret provider metadata required for qualification."""
    return asdict(candidate)


def candidate_from_metadata(metadata: Mapping[str, Any]) -> BackgroundCandidate:
    allowed = {field.name for field in BackgroundCandidate.__dataclass_fields__.values()}
    values = {key: value for key, value in dict(metadata).items() if key in allowed}
    return BackgroundCandidate(**values)


def eligibility_fingerprint() -> str:
    return compute_eligibility_fingerprint({
        "minimum_duration_seconds": DEFAULT_POOL_MINIMUM_DURATION_SECONDS,
        "maximum_duration_seconds": DEFAULT_POOL_MAXIMUM_DURATION_SECONDS,
        "minimum_width": 1080,
        "minimum_height": 1920,
        "maximum_file_size_bytes": DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES,
        "variant_policy": "smallest-sufficient-native-1080x1920-v2",
    })


PEXELS_ORIENTATION_CYCLE = PEXELS_DISCOVERY_ORIENTATION_CYCLE


def _search_provider(
    provider_name: str,
    provider: Any,
    query: str,
    page_number: int,
    orientation: Optional[str],
) -> BackgroundDiscoveryPage:
    """Perform one metadata-only provider request.

    Signature inspection keeps compatibility with lightweight providers and
    test doubles that predate the optional Pexels orientation argument without
    issuing an uncounted retry after a provider-side ``TypeError``.
    """
    if provider_name == "pexels" and orientation:
        try:
            parameters = signature(provider.search).parameters.values()
            accepts_orientation = any(
                parameter.name == "orientation"
                or parameter.kind is Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_orientation = True
        if accepts_orientation:
            page = provider.search(query, page=page_number, orientation=orientation)
        else:
            page = provider.search(query, page=page_number)
    else:
        page = provider.search(query, page=page_number)
    if isinstance(page, BackgroundDiscoveryPage):
        return page
    return BackgroundDiscoveryPage(
        provider_name,
        query,
        page_number,
        len(page),
        tuple(page),
        (),
    )


def _candidate_shape(candidate: BackgroundCandidate) -> str:
    """Return the coarse source geometry used by discovery benchmark stats."""
    width = int(candidate.file_width)
    height = int(candidate.file_height)
    ratio = width / height
    if height > width:
        return "portrait"
    if 0.90 <= ratio <= 1.10:
        return "square"
    return "high_res_landscape"


def discover_candidates(
    *,
    store: SupabaseStateStore,
    providers: Mapping[str, Any],
    max_candidates: int = DISCOVERY_MAX_CANDIDATES_PER_RUN,
    target_pending: Optional[int] = None,
    verified_target: int = DISCOVERY_VERIFIED_TARGET,
    query_offset: Optional[int] = None,
    page_offset: Optional[int] = None,
    max_api_requests: int = DISCOVERY_MAX_API_REQUESTS_PER_RUN,
    max_metadata_items: int = DISCOVERY_MAX_METADATA_ITEMS_PER_RUN,
    max_search_workers: int = DISCOVERY_MAX_SEARCH_WORKERS,
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Search on every invocation, independent of pending backlog size.

    The per-run API, metadata and enqueue budgets remain bounded. An optional
    explicit target_pending may be supplied by a legacy caller, but the
    scheduled prescan and normal local discovery never impose a queue cap.
    Discovery performs no downloads or CV scans.
    """
    if max_candidates < 1:
        raise ValueError("BACKGROUND_DISCOVERY_MAX_CANDIDATES_PER_RUN must be positive.")
    if max_api_requests < 1:
        raise ValueError("BACKGROUND_DISCOVERY_MAX_API_REQUESTS_PER_RUN must be positive.")
    if max_metadata_items < 1:
        raise ValueError("BACKGROUND_DISCOVERY_MAX_METADATA_ITEMS_PER_RUN must be positive.")
    if max_search_workers < 1:
        raise ValueError("BACKGROUND_DISCOVERY_MAX_SEARCH_WORKERS must be positive.")
    max_api_requests = min(max_api_requests, DISCOVERY_MAX_API_REQUESTS_PER_RUN)
    max_metadata_items = min(max_metadata_items, DISCOVERY_MAX_METADATA_ITEMS_PER_RUN)
    max_search_workers = min(max_search_workers, DISCOVERY_MAX_SEARCH_WORKERS)
    # No implicit pending-queue limit: a full backlog must not suppress
    # the scheduled metadata-only prescan. Preserve an explicit opt-in
    # ceiling for existing callers, without the old hard-coded 100 cap.
    if target_pending is not None:
        if type(target_pending) is not int or target_pending < 1:
            raise ValueError("BACKGROUND_DISCOVERY_TARGET_PENDING must be positive.")
        summary = store.background_candidate_summary()
        pending_count = int(summary.get("pending", 0))
        if pending_count >= target_pending:
            logger(
                f"[BACKGROUND_DISCOVERY] Explicit queue target reached "
                f"({pending_count} >= {target_pending}). Skipping enqueue."
            )
            return {
                "status": "POOL_TARGET_REACHED",
                "discovered": 0,
                "eligible_pending": 0,
                "metadata_ineligible": 0,
                "metadata_records_examined": 0,
                "landscape_excluded": 0,
                "duplicates": 0,
                "provider_requests": 0,
                "provider_errors": 0,
                "pages_touched": [],
                "query_terms_touched": [],
                "portrait_candidates": 0,
                "square_candidates": 0,
                "high_res_landscape_candidates": 0,
                "summary": summary,
            }
        max_candidates = min(max_candidates, target_pending - pending_count)
    state = _load_discovery_rotation(store, logger)
    categories = tuple(name for name, _ in BACKGROUND_SEARCH_CATEGORIES)
    sequence = state.get("sequence", discovery_rotation_offset() if state.get("unavailable") else 0)
    if not isinstance(sequence, int) or sequence < 0:
        sequence = 0
    category = categories[sequence % len(categories)]
    cursors = dict(state.get("cursors", {})) if isinstance(state.get("cursors"), dict) else {}
    cursor = cursors.get(category, {})
    cursor = cursor if isinstance(cursor, dict) else {}
    query_cursor = cursor.get("query_cursor", 0)
    page_cursor = cursor.get("page_cursor", 0)
    query_cursor = query_cursor if isinstance(query_cursor, int) and query_cursor >= 0 else 0
    page_cursor = page_cursor if isinstance(page_cursor, int) and page_cursor >= 0 else 0
    if query_offset is not None:
        query_cursor = query_offset
    if page_offset is not None:
        page_cursor = page_offset
    rotated_queries = get_rotated_queries(query_cursor, category=category)
    rotation = sequence if page_offset is None else page_offset
    rotated_pages = get_rotated_pages(page_cursor)
    last_query_index = -1
    eligible_pending = 0
    metadata_ineligible = 0
    landscape_excluded = 0
    duplicate_count = 0
    provider_requests = 0
    provider_errors = 0
    metadata_records_examined = 0
    pages_touched: list[int] = []
    query_terms_touched: list[str] = []
    shape_counts = {
        "portrait_candidates": 0,
        "square_candidates": 0,
        "high_res_landscape_candidates": 0,
    }

    provider_names = list(providers.keys())
    if provider_names:
        start = rotation % len(provider_names)
        provider_names = provider_names[start:] + provider_names[:start]

    # At most four submitted requests, spanning multiple queries. A batch is
    # fully resolved before another is submitted; enqueue order is never timing-dependent.
    query_batch_size = max(1, max_search_workers // max(1, len(provider_names)))
    with ThreadPoolExecutor(max_workers=max_search_workers) as executor:
        for page_number in rotated_pages:
            for batch_start in range(0, len(rotated_queries), query_batch_size):
                if (eligible_pending >= max_candidates or provider_requests >= max_api_requests
                        or metadata_records_examined >= max_metadata_items or not provider_names):
                    break
                batch = []
                for query_idx in range(batch_start, min(batch_start + query_batch_size, len(rotated_queries))):
                    query = rotated_queries[query_idx]
                    requested_provider_names = provider_names[:max_api_requests - provider_requests]
                    if not requested_provider_names:
                        break
                    futures = {}
                    for p_name in requested_provider_names:
                        orientation = (PEXELS_ORIENTATION_CYCLE[query_idx % len(PEXELS_ORIENTATION_CYCLE)]
                                       if p_name == "pexels" else None)
                        futures[p_name] = executor.submit(
                            _search_provider, p_name, providers[p_name], query, page_number, orientation)
                        provider_requests += 1
                    batch.append((query_idx, query, requested_provider_names, futures))
                for query_idx, query, requested_provider_names, futures in batch:
                    if page_number not in pages_touched:
                        pages_touched.append(page_number)
                    if query not in query_terms_touched:
                        query_terms_touched.append(query)
                    if eligible_pending < max_candidates and metadata_records_examined < max_metadata_items:
                        last_query_index = query_idx
                    provider_pages: dict[str, BackgroundDiscoveryPage] = {}
                    # Resolve and log in request-plan order, independent of the
                    # completion order of concurrent network calls.
                    for p_name in requested_provider_names:
                        try:
                            discovery = futures[p_name].result()
                            provider_pages[p_name] = discovery
                            logger(
                                f"[BACKGROUND_DISCOVERY] provider={p_name} query={query!r} "
                                f"page={page_number} candidates={len(discovery.candidates)} "
                                f"metadata_ineligible={len(discovery.metadata_ineligible)}"
                            )
                        except Exception as search_err:
                            provider_errors += 1
                            logger(
                                f"[BACKGROUND_DISCOVERY] provider={p_name} query={query!r} "
                                f"page={page_number} error={type(search_err).__name__}"
                            )

                    # Interleave candidates from providers for this query to avoid
                    # provider starvation. Eligible metadata is examined before
                    # audit-only rejections at the same index so cheap failures do
                    # not consume the useful-candidate or metadata budget first.
                    max_items = max(
                        (max(len(p.candidates), len(p.metadata_ineligible)) for p in provider_pages.values()),
                        default=0,
                    )
                    for idx in range(max_items):
                        if (
                            eligible_pending >= max_candidates
                            or metadata_records_examined >= max_metadata_items
                        ):
                            break
                        for p_name in requested_provider_names:
                            if (
                                eligible_pending >= max_candidates
                                or metadata_records_examined >= max_metadata_items
                            ):
                                break
                            p_disc = provider_pages.get(p_name)
                            if not p_disc:
                                continue

                            if idx < len(p_disc.candidates):
                                item = p_disc.candidates[idx]
                                metadata_records_examined += 1
                                reason = candidate_cost_rejection_reason(
                                    item,
                                    DEFAULT_POOL_MINIMUM_DURATION_SECONDS,
                                    DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES,
                                    maximum_source_duration_seconds=DEFAULT_POOL_MAXIMUM_DURATION_SECONDS,
                                )
                                if reason is None and not can_produce_native_vertical_crop(
                                    item.file_width,
                                    item.file_height,
                                ):
                                    reason = "insufficient_native_crop_resolution"

                                status = "METADATA_INELIGIBLE" if reason else "PENDING"
                                metadata = candidate_metadata(item)
                                if reason:
                                    metadata["reason"] = reason
                                res = store.enqueue_background_candidate(
                                    item.provider,
                                    item.provider_asset_id,
                                    item.provider_variant_id,
                                    item.query,
                                    metadata,
                                    status,
                                )
                                if res and res.get("discovery_count", 1) > 1:
                                    duplicate_count += 1
                                elif reason:
                                    metadata_ineligible += 1
                                else:
                                    eligible_pending += 1
                                    shape = _candidate_shape(item)
                                    shape_counts[f"{shape}_candidates"] += 1

                            if (
                                eligible_pending >= max_candidates
                                or metadata_records_examined >= max_metadata_items
                            ):
                                break

                            if idx < len(p_disc.metadata_ineligible):
                                item = p_disc.metadata_ineligible[idx]
                                metadata_records_examined += 1
                                if getattr(item, "reason", "") == "ORIENTATION_LANDSCAPE_EXCLUDED":
                                    landscape_excluded += 1
                                    continue
                                res = store.enqueue_background_candidate(
                                    item.provider,
                                    item.provider_asset_id,
                                    item.provider_variant_id,
                                    item.query,
                                    {"reason": item.reason},
                                    "METADATA_INELIGIBLE",
                                )
                                if res and res.get("discovery_count", 1) > 1:
                                    duplicate_count += 1
                                else:
                                    metadata_ineligible += 1

                    logger(
                        f"[BACKGROUND_DISCOVERY] query={query!r} providers={list(provider_pages.keys())} "
                        f"eligible_pending={eligible_pending} metadata_ineligible={metadata_ineligible} "
                        f"duplicate_hits={duplicate_count} landscape_excluded={landscape_excluded}"
                    )

    next_query_cursor = (query_cursor + last_query_index + 1) % len(rotated_queries)
    # Rotate the starting page every appearance, even on an empty/full traversal.
    next_page_cursor = (page_cursor + 1) % DISCOVERY_MAX_PAGE
    rotation_state = {
        "category": category,
        "next_category": categories[(sequence + 1) % len(categories)],
        "completed_at": utc_now_iso(),
        "query_cursor": next_query_cursor,
        "page_cursor": next_page_cursor,
        "sequence": sequence + 1,
        "cursors": {**cursors, category: {"query_cursor": next_query_cursor, "page_cursor": next_page_cursor}},
    }
    if provider_requests and not state.get("unavailable") and callable(getattr(store, "save_builder_run", None)):
        store.save_builder_run(DISCOVERY_ROTATION_STATE_ID, "completed", rotation_state)

    return {
        "status": "COMPLETED",
        "rotation": rotation_state,
        "discovered": eligible_pending,
        "eligible_pending": eligible_pending,
        "metadata_ineligible": metadata_ineligible,
        "metadata_records_examined": metadata_records_examined,
        "landscape_excluded": landscape_excluded,
        "duplicates": duplicate_count,
        "provider_requests": provider_requests,
        "provider_errors": provider_errors,
        "pages_touched": pages_touched,
        "query_terms_touched": query_terms_touched,
        **shape_counts,
        "summary": store.background_candidate_summary(),
    }


def qualify_one_claimed_candidate(
    *,
    store: SupabaseStateStore,
    pool_repository: Any,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    work_dir: Optional[str] = None,
    download_fn: Callable[..., Any] = download_background,
    scan_fn: Callable[..., Any] = scan_video,
    monotonic: Callable[[], float] = time.monotonic,
    logger: Callable[[str], None] = print,
) -> Optional[dict[str, Any]]:
    """Claim and fully qualify one candidate, returning ``None`` when empty.

    Terminal queue state is conditioned on the lease token, so an expired
    worker cannot overwrite a later worker's result.
    """
    claim = store.claim_background_candidate(worker_id, lease_seconds)
    if not claim:
        return None
    candidate_id = str(claim["candidate_id"])
    lease_token = str(claim["lease_token"])
    metadata = claim.get("metadata") if isinstance(claim.get("metadata"), Mapping) else {}
    query = str(claim.get("search_query") or "")
    provider = str(claim.get("provider") or "pexels")
    asset_id = str(claim.get("provider_asset_id") or "")
    variant_id = str(claim.get("provider_variant_id") or "")
    logger(f"[QUALIFICATION] Claimed candidate_id={candidate_id} provider={provider} asset_id={asset_id} variant_id={variant_id}")
    started = monotonic()
    downloaded = None
    try:
        candidate = candidate_from_metadata(metadata)
        reason = candidate_cost_rejection_reason(candidate, DEFAULT_POOL_MINIMUM_DURATION_SECONDS, DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES, maximum_source_duration_seconds=DEFAULT_POOL_MAXIMUM_DURATION_SECONDS)
        if reason:
            logger(f"[QUALIFICATION] candidate_id={candidate_id} metadata_verdict=INELIGIBLE reason={reason}")
            pool_repository.save_metadata_ineligible(MetadataIneligibleRecord(candidate.provider, candidate.provider_asset_id, candidate.provider_variant_id, eligibility_fingerprint(), reason.upper(), utc_now_iso(), query))
            return store.complete_background_candidate(candidate_id, lease_token, "METADATA_INELIGIBLE", reason.upper(), {"reason": reason, "completed_by": worker_id})
        logger(f"[QUALIFICATION] candidate_id={candidate_id} metadata_verdict=VALID duration={candidate.duration_seconds}s {candidate.file_width}x{candidate.file_height}")
        target_dir = work_dir or tempfile.gettempdir()
        dl_start = monotonic()
        downloaded = download_fn(candidate, target_dir=target_dir, timeout=min(DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS, 60.0), max_file_size_bytes=DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES, abort_check=lambda: monotonic() - started >= DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS)
        dl_dur = monotonic() - dl_start
        logger(f"[QUALIFICATION] candidate_id={candidate_id} download_result=SUCCESS bytes={downloaded.file_size_bytes} duration={dl_dur:.2f}s sha256={downloaded.content_sha256[:16]}...")
        scan_start = monotonic()
        result = scan_fn(downloaded.local_path, config=SafetyConfig(animal_detection_required=True), abort_check=lambda: monotonic() - started >= DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS)
        scan_dur = monotonic() - scan_start
        p_count = len(result.person_detections)
        f_count = len(result.face_detections)
        a_count = len(getattr(result, "animal_detections", ()))
        p_conf = max((getattr(d, "confidence", 0.0) for d in result.person_detections), default=0.0)
        f_conf = max((getattr(d, "confidence", 0.0) for d in result.face_detections), default=0.0)
        logger(
            f"[QUALIFICATION] candidate_id={candidate_id} scan_frames={result.frames_scanned} scan_seconds={scan_dur:.2f} "
            f"person_detections={p_count} max_person_conf={p_conf:.4f} "
            f"face_detections={f_count} max_face_conf={f_conf:.4f} "
            f"animal_detections={a_count} animal_status={result.animal_policy_status}"
        )
        prohibited_subject_found = bool(
            result.person_detections or result.face_detections or result.animal_detections
        )
        # Positive findings already disqualify the source. A later deadline
        # must not turn that rejection into an immediately retryable failure.
        if not prohibited_subject_found and monotonic() - started >= DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS:
            raise TimeoutError("CANDIDATE_PROCESSING_TIMEOUT")
        if not prohibited_subject_found and result.safe and result.animal_policy_status == "CLEAR":
            pool_repository.save_verified(build_verified_record(candidate, content_sha256=downloaded.content_sha256, downloaded_file_size=downloaded.file_size_bytes, scanned_frames=result.frames_scanned, animal_detector=ANIMAL_DETECTOR_ID, animal_classes=ANIMAL_DETECTION_CLASSES, animal_threshold=DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD, animal_verdict="CLEAR"))
            terminal = "VERIFIED"
            error_code = None
            logger(f"[QUALIFICATION] candidate_id={candidate_id} FINAL_STATUS=VERIFIED added_to_verified_pool=TRUE")
        else:
            error_code = _rejection_reason(result)
            pool_repository.save_safety_rejected(SafetyRejectedRecord(candidate.provider, candidate.provider_asset_id, candidate.provider_variant_id, CURRENT_SAFETY_POLICY_FINGERPRINT, error_code, utc_now_iso(), query))
            terminal = "SAFETY_REJECTED"
            logger(f"[QUALIFICATION] candidate_id={candidate_id} FINAL_STATUS=SAFETY_REJECTED reason={error_code}")
        return store.complete_background_candidate(candidate_id, lease_token, terminal, error_code, {"completed_by": worker_id, "frames_scanned": result.frames_scanned, "duration_seconds": max(0.0, monotonic() - started)})
    except Exception as err:
        code = "CANDIDATE_PROCESSING_TIMEOUT" if isinstance(err, TimeoutError) else type(err).__name__
        logger(f"[QUALIFICATION] candidate_id={candidate_id} FINAL_STATUS=TRANSIENT_FAILURE error={code} details={err}")
        provider = str(claim.get("provider") or "pexels")
        pool_repository.save_transient(TransientFailureRecord(provider, str(claim.get("provider_asset_id") or ""), str(claim.get("provider_variant_id") or ""), code, utc_now_iso()))
        return store.complete_background_candidate(candidate_id, lease_token, "TRANSIENT_FAILURE", code, {"completed_by": worker_id})
    finally:
        if downloaded is not None:
            try:
                os.remove(downloaded.local_path)
            except FileNotFoundError:
                pass


def local_worker_id() -> str:
    return f"local:{socket.gethostname()}:{uuid.uuid4().hex[:12]}"


def cloud_worker_id(prefix: str = "github-actions") -> str:
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    return f"{prefix}:{run_id}:{run_attempt}:{uuid.uuid4().hex[:8]}"
