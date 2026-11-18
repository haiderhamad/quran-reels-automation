"""Resumable provider-neutral verified background pool V2 builder."""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import signal
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from engine.background_pool import (
    CURRENT_SAFETY_POLICY_FINGERPRINT,
    BackgroundPoolRepository,
    MetadataIneligibleRecord,
    ProviderQueryMetrics,
    SafetyRejectedRecord,
    TransientFailureRecord,
    build_verified_record,
    compute_eligibility_fingerprint,
    utc_now_iso,
)
from engine.backgrounds import (
    ALLOWED_NATURE_QUERIES,
    BACKGROUND_SEARCH_CATEGORIES,
    PEXELS_DISCOVERY_ORIENTATION_CYCLE,
    BackgroundDiscoveryPage,
    DownloadedBackground,
    candidate_cost_rejection_reason,
    download_background,
)
from engine.safety import (
    ANIMAL_DETECTION_CLASSES,
    ANIMAL_DETECTOR_ID,
    ANIMAL_POLICY_NOT_READY,
    ANIMAL_POLICY_READY,
    DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD,
    SafetyConfig,
    SafetyScanResult,
    scan_video,
)

DEFAULT_POOL_MINIMUM_DURATION_SECONDS = 15.0
DEFAULT_POOL_MAXIMUM_DURATION_SECONDS = 45.0
DEFAULT_BUILDER_MAX_CANDIDATES = 2000
DEFAULT_BUILDER_MAX_PAGES_PER_QUERY = 5
DEFAULT_BUILDER_MAX_SAFETY_SCANS = 500
DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS = 90.0
DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES = 64 * 1024 * 1024


class PoolBuilderError(RuntimeError):
    pass


@dataclass(frozen=True)
class PoolBuilderStatus:
    state: str
    builder_run_id: str
    provider: str
    target_new_safe: int
    status_schema_version: int = 3
    found_safe: int = 0
    discovered: int = 0
    metadata_skipped: int = 0
    duplicate_content_sha: int = 0
    downloads: int = 0
    safety_scanned: int = 0
    safety_rejected: int = 0
    transient_failures: int = 0
    current_candidate: Optional[dict[str, str]] = None
    elapsed_seconds: float = 0.0
    policy_fingerprint: str = CURRENT_SAFETY_POLICY_FINGERPRINT
    stats: Mapping[str, Any] = None  # type: ignore[assignment]
    provider_query_metrics: Sequence[Mapping[str, Any]] = ()
    b2_operations: Mapping[str, int] = None  # type: ignore[assignment]
    message: Optional[str] = None
    error_code: Optional[str] = None
    updated_at: str = ""
    pid: Optional[int] = None

    def __post_init__(self) -> None:
        if self.stats is None:
            object.__setattr__(self, "stats", {})
        if self.b2_operations is None:
            object.__setattr__(self, "b2_operations", {})

    @property
    def candidates_processed(self) -> int:
        return self.metadata_skipped + self.downloads

    @property
    def rejected(self) -> int:
        return self.safety_rejected


class AtomicStatusWriter:
    def __init__(self, path: Optional[str]):
        self.path = Path(path).resolve() if path else None

    def write(self, status: PoolBuilderStatus) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(asdict(status), ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)


class LocalBuilderLock:
    def __init__(self, path: Optional[str]):
        self.path = Path(path).resolve() if path else None
        self.handle: Optional[Any] = None

    def __enter__(self) -> "LocalBuilderLock":
        if self.path is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            handle.close()
            raise PoolBuilderError("LOCAL_POOL_BUILDER_ALREADY_RUNNING") from err
        os.chmod(self.path, 0o600)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self.handle = handle
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class StopController:
    def __init__(self, stop_file: Optional[str] = None):
        self.stop_file = Path(stop_file).resolve() if stop_file else None
        self.signal_requested = False

    def request(self, *_args: Any) -> None:
        self.signal_requested = True

    def requested(self) -> bool:
        return self.signal_requested or bool(self.stop_file and self.stop_file.exists())


def _rejection_reason(result: SafetyScanResult) -> str:
    flags = []
    if result.person_detections:
        flags.append("PERSON")
    if result.face_detections:
        flags.append("FACE")
    if result.animal_detections:
        flags.append("ANIMAL")
    return "REJECT_" + "_".join(flags) if flags else "REJECT_SCAN_INCOMPLETE"


def balanced_nature_queries(offset: int = 0) -> tuple[str, ...]:
    """Interleave equal category turns; historical yield never sets priority.

    The direct legacy builder rotates by UTC day. Queue discovery owns its
    separate durable progress, shared by scheduled and local replenishment.
    """
    categories = BACKGROUND_SEARCH_CATEGORIES
    start = offset % len(categories)
    categories = categories[start:] + categories[:start]
    rotated_queries = []
    for _category, queries in categories:
        query_start = offset % len(queries)
        rotated_queries.append(queries[query_start:] + queries[:query_start])
    return tuple(
        queries[index]
        for index in range(max(len(queries) for queries in rotated_queries))
        for queries in rotated_queries
        if index < len(queries)
    )


def _provider_items(provider: Any, provider_name: str) -> tuple[tuple[str, Any], ...]:
    items = tuple((str(name), value) for name, value in provider.items()) if isinstance(provider, Mapping) else ((provider_name, provider),)
    valid = tuple((name, value) for name, value in items if name in ("pexels", "pixabay"))
    if not valid:
        raise PoolBuilderError("LOCAL_POOL_INVALID_PROVIDER")
    return valid


def _bound_discovery_page(
    discovery: BackgroundDiscoveryPage,
    remaining_discoveries: int,
) -> BackgroundDiscoveryPage:
    """Cap a provider page before any persistence or media processing occurs.

    Providers return pages atomically, so the old loop could increment the
    run-level discovery counter beyond ``max_candidates`` by one full page.
    The bounded builder contract is stricter: process only the remaining
    candidate slots and never record a discovery count above the caller cap.
    Candidates are retained before metadata skips so a small top-up still has
    the opportunity to find a verified-safe source.
    """
    if remaining_discoveries <= 0:
        return BackgroundDiscoveryPage(
            discovery.provider,
            discovery.query,
            discovery.page,
            0,
            (),
            (),
        )
    if discovery.discovered <= remaining_discoveries:
        return discovery
    candidates = tuple(discovery.candidates[:remaining_discoveries])
    remaining = max(0, remaining_discoveries - len(candidates))
    metadata_ineligible = tuple(discovery.metadata_ineligible[:remaining])
    return BackgroundDiscoveryPage(
        discovery.provider,
        discovery.query,
        discovery.page,
        remaining_discoveries,
        candidates,
        metadata_ineligible,
    )


def build_verified_pool(
    *,
    repository: BackgroundPoolRepository,
    provider: Any,
    add_safe: int,
    provider_name: str = "pexels",
    max_candidates: int = DEFAULT_BUILDER_MAX_CANDIDATES,
    max_pages_per_query: int = DEFAULT_BUILDER_MAX_PAGES_PER_QUERY,
    max_safety_scans: int = DEFAULT_BUILDER_MAX_SAFETY_SCANS,
    minimum_duration_seconds: float = DEFAULT_POOL_MINIMUM_DURATION_SECONDS,
    maximum_duration_seconds: float = DEFAULT_POOL_MAXIMUM_DURATION_SECONDS,
    per_candidate_timeout_seconds: float = DEFAULT_BUILDER_CANDIDATE_TIMEOUT_SECONDS,
    time_budget_seconds: Optional[float] = None,
    query_subset: Optional[Sequence[str]] = None,
    status_writer: Optional[AtomicStatusWriter] = None,
    stop_controller: Optional[StopController] = None,
    lock_file: Optional[str] = None,
    work_dir: Optional[str] = None,
    download_fn: Callable[..., DownloadedBackground] = download_background,
    scan_fn: Callable[..., SafetyScanResult] = scan_video,
    logger: Callable[[str], None] = print,
    monotonic: Callable[[], float] = time.monotonic,
) -> PoolBuilderStatus:
    if min(add_safe, max_candidates, max_pages_per_query, max_safety_scans) < 1:
        raise PoolBuilderError("Pool builder budgets must be positive.")
    if not ANIMAL_POLICY_READY:
        raise PoolBuilderError(ANIMAL_POLICY_NOT_READY)
    providers = _provider_items(provider, provider_name)
    writer = status_writer or AtomicStatusWriter(None)
    stopper = stop_controller or StopController()
    run_id = f"pool_{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    started = monotonic()
    resolved_work_dir = work_dir or tempfile.gettempdir()
    os.makedirs(resolved_work_dir, exist_ok=True)
    eligibility_fingerprint = compute_eligibility_fingerprint({
        "minimum_duration_seconds": minimum_duration_seconds,
        "maximum_duration_seconds": maximum_duration_seconds,
        "minimum_width": 1080,
        "minimum_height": 1920,
        "maximum_file_size_bytes": DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES,
        "variant_policy": "smallest-sufficient-native-1080x1920-v2",
    })
    reconciliation = repository.reconcile_pool_state(eligibility_fingerprint)
    stale_ids = set(reconciliation.stale_record_ids)
    known_verified = {
        (record.provider, record.provider_asset_id, record.provider_variant_id)
        for record in reconciliation.verified_records
        if record.is_current_policy and record.record_id not in stale_ids
    }
    # Provider IDs are not a content identity.  Preserve the existing source
    # record as authoritative and never add an alternate provider/variant row
    # whose downloaded bytes are already represented in the pool.
    known_content_sha256 = {
        record.content_sha256
        for record in reconciliation.verified_records
    }
    known_rejected = set(reconciliation.safety_rejected_identities)
    known_ineligible = set(reconciliation.metadata_ineligible_identities)
    known_assets = {
        (provider_name_value, asset_id)
        for provider_name_value, asset_id, _variant_id in (
            set(known_verified) | set(known_rejected) | set(known_ineligible)
        )
    }
    seen_assets: set[tuple[str, str]] = set()
    metric_map: dict[tuple[str, str], ProviderQueryMetrics] = {}
    pool_stats = dict(reconciliation.stats)
    status = PoolBuilderStatus(
        state="starting",
        builder_run_id=run_id,
        provider=provider_name,
        target_new_safe=add_safe,
        stats=pool_stats,
        b2_operations=repository.operation_counts(),
        pid=os.getpid(),
    )

    def public_metrics() -> tuple[dict[str, Any], ...]:
        return tuple({"provider": m.provider, "query": m.query, "scanned": m.scans_attempted, "safe": m.verified_safe} for m in metric_map.values())

    def persist(next_status: PoolBuilderStatus) -> PoolBuilderStatus:
        current = replace(
            next_status,
            elapsed_seconds=max(0.0, monotonic() - started),
            updated_at=utc_now_iso(),
            pid=os.getpid(),
            provider_query_metrics=public_metrics(),
            b2_operations=repository.operation_counts(),
        )
        writer.write(current)
        repository.save_builder_run(run_id, asdict(current))
        current = replace(current, b2_operations=repository.operation_counts())
        writer.write(current)
        return current

    def increment_pool_stat(key: str, amount: int = 1) -> dict[str, Any]:
        pool_stats[key] = max(0, int(pool_stats.get(key, 0)) + amount)
        return dict(pool_stats)

    def finalize(next_status: PoolBuilderStatus) -> PoolBuilderStatus:
        final_stats = repository.compute_stats()
        repository.save_stats(final_stats)
        return persist(replace(next_status, stats=final_stats, current_candidate=None))

    def terminal(current: PoolBuilderStatus) -> Optional[PoolBuilderStatus]:
        if stopper.requested():
            return finalize(replace(current, state="stopped", message="Stopped safely by user."))
        if time_budget_seconds is not None and monotonic() - started >= time_budget_seconds:
            return finalize(replace(current, state="time_budget_exhausted", error_code="POOL_MAINTENANCE_TIME_BUDGET_EXHAUSTED", message="Maintenance time budget exhausted."))
        return None

    with LocalBuilderLock(lock_file):
        status = persist(replace(status, state="scanning"))
        if query_subset:
            requested = tuple(dict.fromkeys(str(query).strip() for query in query_subset))
            invalid = tuple(query for query in requested if query not in ALLOWED_NATURE_QUERIES)
            if invalid:
                raise PoolBuilderError(
                    "LOCAL_POOL_INVALID_QUERY:" + ",".join(invalid)
                )
            ordered_queries = requested
        else:
            ordered_queries = balanced_nature_queries(
                datetime.datetime.now(datetime.timezone.utc).date().toordinal()
            )
        for page in range(1, max_pages_per_query + 1):
            for query_index, query in enumerate(ordered_queries):
                for name, source in providers:
                    if status.found_safe >= add_safe or status.discovered >= max_candidates or status.safety_scanned >= max_safety_scans:
                        break
                    done = terminal(status)
                    if done is not None:
                        return done
                    metric = metric_map.get((name, query), ProviderQueryMetrics(builder_run_id=run_id, provider=name, query=query))
                    try:
                        if name == "pexels":
                            orientation = PEXELS_DISCOVERY_ORIENTATION_CYCLE[
                                query_index % len(PEXELS_DISCOVERY_ORIENTATION_CYCLE)
                            ]
                            try:
                                parameters = signature(source.search).parameters.values()
                                accepts_orientation = any(
                                    parameter.name == "orientation"
                                    or parameter.kind is Parameter.VAR_KEYWORD
                                    for parameter in parameters
                                )
                            except (TypeError, ValueError):
                                accepts_orientation = True
                            if accepts_orientation:
                                page_value = source.search(
                                    query,
                                    page=page,
                                    orientation=orientation,
                                )
                            else:
                                page_value = source.search(query, page=page)
                        else:
                            page_value = source.search(query, page=page)
                        discovery = page_value if isinstance(page_value, BackgroundDiscoveryPage) else BackgroundDiscoveryPage(name, query, page, len(page_value), tuple(page_value), ())
                        discovery = _bound_discovery_page(
                            discovery,
                            max_candidates - status.discovered,
                        )
                        metric = replace(metric, api_requests=metric.api_requests + 1, pages_attempted=metric.pages_attempted + 1, candidates_returned=metric.candidates_returned + discovery.discovered, updated_at=utc_now_iso())
                    except Exception as err:
                        metric = replace(metric, transient_failures=metric.transient_failures + 1, updated_at=utc_now_iso())
                        metric_map[(name, query)] = metric
                        repository.save_query_metrics(metric)
                        repository.save_transient(TransientFailureRecord(name, "search", f"page-{page}", type(err).__name__, utc_now_iso()))
                        status = persist(replace(
                            status,
                            transient_failures=status.transient_failures + 1,
                            stats=increment_pool_stat("transient_failures"),
                            message=f"{name} search temporarily failed.",
                        ))
                        continue
                    metric_map[(name, query)] = metric
                    status = persist(replace(status, discovered=status.discovered + discovery.discovered))
                    for item in discovery.metadata_ineligible:
                        identity = (item.provider, item.provider_asset_id, item.provider_variant_id)
                        asset_identity = (item.provider, item.provider_asset_id)
                        if asset_identity in known_assets:
                            if any(existing[:2] == asset_identity for existing in known_verified):
                                metric = replace(metric, known_verified_skips=metric.known_verified_skips + 1)
                            elif any(existing[:2] == asset_identity for existing in known_rejected):
                                metric = replace(metric, known_safety_rejected_skips=metric.known_safety_rejected_skips + 1)
                            else:
                                metric = replace(metric, known_metadata_ineligible_skips=metric.known_metadata_ineligible_skips + 1)
                            continue
                        if asset_identity in seen_assets:
                            metric = replace(metric, duplicate_asset_skips=metric.duplicate_asset_skips + 1)
                            continue
                        seen_assets.add(asset_identity)
                        repository.save_metadata_ineligible(MetadataIneligibleRecord(item.provider, item.provider_asset_id, item.provider_variant_id, eligibility_fingerprint, item.reason, utc_now_iso(), item.query))
                        known_ineligible.add(identity)
                        known_assets.add(asset_identity)
                        metric = replace(metric, metadata_ineligible=metric.metadata_ineligible + 1)
                        status = persist(replace(
                            status,
                            metadata_skipped=status.metadata_skipped + 1,
                            stats=increment_pool_stat("metadata_ineligible"),
                        ))
                    candidates = sorted(discovery.candidates, key=lambda c: (abs(c.duration_seconds - minimum_duration_seconds), c.declared_file_size_bytes or 2**63, c.provider_asset_id))
                    for candidate in candidates:
                        if status.found_safe >= add_safe or status.safety_scanned >= max_safety_scans:
                            break
                        identity = (name, candidate.provider_asset_id, candidate.provider_variant_id)
                        asset_identity = (name, candidate.provider_asset_id)
                        metric = replace(metric, unique_variants_seen=metric.unique_variants_seen + 1)
                        if asset_identity in known_assets:
                            if any(item[:2] == asset_identity for item in known_verified):
                                metric = replace(metric, known_verified_skips=metric.known_verified_skips + 1)
                            elif any(item[:2] == asset_identity for item in known_rejected):
                                metric = replace(metric, known_safety_rejected_skips=metric.known_safety_rejected_skips + 1)
                            else:
                                metric = replace(metric, known_metadata_ineligible_skips=metric.known_metadata_ineligible_skips + 1)
                            continue
                        if asset_identity in seen_assets:
                            metric = replace(metric, duplicate_asset_skips=metric.duplicate_asset_skips + 1)
                            continue
                        seen_assets.add(asset_identity)
                        done = terminal(status)
                        if done is not None:
                            return done
                        status = persist(replace(status, current_candidate={"provider": name, "query": query, "asset_id": candidate.provider_asset_id}))
                        reason = candidate_cost_rejection_reason(
                            candidate,
                            minimum_duration_seconds,
                            DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES,
                            maximum_source_duration_seconds=maximum_duration_seconds,
                        )
                        if reason:
                            repository.save_metadata_ineligible(MetadataIneligibleRecord(name, candidate.provider_asset_id, candidate.provider_variant_id, eligibility_fingerprint, reason.upper(), utc_now_iso(), query))
                            known_ineligible.add(identity)
                            known_assets.add(asset_identity)
                            metric = replace(metric, metadata_ineligible=metric.metadata_ineligible + 1)
                            status = persist(replace(
                                status,
                                metadata_skipped=status.metadata_skipped + 1,
                                current_candidate=None,
                                stats=increment_pool_stat("metadata_ineligible"),
                            ))
                            continue
                        downloaded: Optional[DownloadedBackground] = None
                        candidate_started = monotonic()
                        try:
                            download_started = monotonic()
                            downloaded = download_fn(candidate, target_dir=resolved_work_dir, timeout=min(per_candidate_timeout_seconds, 60.0), max_file_size_bytes=DEFAULT_BUILDER_MAX_FILE_SIZE_BYTES, abort_check=lambda: stopper.requested() or monotonic() - candidate_started >= per_candidate_timeout_seconds)
                            download_seconds = monotonic() - download_started
                            status = persist(replace(status, downloads=status.downloads + 1))
                            metric = replace(metric, downloads_attempted=metric.downloads_attempted + 1, downloaded_bytes=metric.downloaded_bytes + downloaded.file_size_bytes, download_seconds=metric.download_seconds + download_seconds)
                            if downloaded.content_sha256 in known_content_sha256:
                                repository.save_metadata_ineligible(MetadataIneligibleRecord(
                                    name,
                                    candidate.provider_asset_id,
                                    candidate.provider_variant_id,
                                    eligibility_fingerprint,
                                    "DUPLICATE_CONTENT_SHA256",
                                    utc_now_iso(),
                                    query,
                                ))
                                known_ineligible.add(identity)
                                known_assets.add(asset_identity)
                                metric = replace(
                                    metric,
                                    metadata_ineligible=metric.metadata_ineligible + 1,
                                    duplicate_content_sha_skips=metric.duplicate_content_sha_skips + 1,
                                )
                                status = persist(replace(
                                    status,
                                    metadata_skipped=status.metadata_skipped + 1,
                                    duplicate_content_sha=status.duplicate_content_sha + 1,
                                    current_candidate=None,
                                    stats=increment_pool_stat("metadata_ineligible"),
                                ))
                                continue
                            scan_started = monotonic()
                            scan_result = scan_fn(downloaded.local_path, config=SafetyConfig(animal_detection_required=True), abort_check=lambda: stopper.requested() or monotonic() - candidate_started >= per_candidate_timeout_seconds)
                            scan_seconds = monotonic() - scan_started
                            if stopper.requested():
                                status = finalize(replace(status, state="stopped", message="Stopped safely by user; interrupted candidate was not trusted."))
                                return status
                            metric = replace(metric, scans_attempted=metric.scans_attempted + 1, scanned_frames=metric.scanned_frames + scan_result.frames_scanned, scan_seconds=metric.scan_seconds + scan_seconds)
                            status = persist(replace(status, safety_scanned=status.safety_scanned + 1))
                            if monotonic() - candidate_started >= per_candidate_timeout_seconds:
                                raise TimeoutError("CANDIDATE_PROCESSING_TIMEOUT")
                            if scan_result.safe and scan_result.animal_policy_status == "CLEAR":
                                repository.save_verified(build_verified_record(candidate, content_sha256=downloaded.content_sha256, downloaded_file_size=downloaded.file_size_bytes, scanned_frames=scan_result.frames_scanned, animal_detector=ANIMAL_DETECTOR_ID, animal_classes=ANIMAL_DETECTION_CLASSES, animal_threshold=DEFAULT_ANIMAL_CONFIDENCE_THRESHOLD, animal_verdict="CLEAR"))
                                known_verified.add(identity)
                                known_content_sha256.add(downloaded.content_sha256)
                                known_assets.add(asset_identity)
                                metric = replace(metric, verified_safe=metric.verified_safe + 1)
                                increment_pool_stat("verified_current_policy")
                                increment_pool_stat("production_unused")
                                increment_pool_stat(f"{name}_verified_current_policy")
                                status = persist(replace(
                                    status,
                                    found_safe=status.found_safe + 1,
                                    current_candidate=None,
                                    stats=dict(pool_stats),
                                ))
                            else:
                                rejection = _rejection_reason(scan_result)
                                repository.save_safety_rejected(SafetyRejectedRecord(name, candidate.provider_asset_id, candidate.provider_variant_id, CURRENT_SAFETY_POLICY_FINGERPRINT, rejection, utc_now_iso(), query))
                                known_rejected.add(identity)
                                known_assets.add(asset_identity)
                                metric = replace(metric, safety_rejected=metric.safety_rejected + 1)
                                increment_pool_stat("known_safety_rejected")
                                increment_pool_stat("known_rejected")
                                status = persist(replace(
                                    status,
                                    safety_rejected=status.safety_rejected + 1,
                                    current_candidate=None,
                                    stats=dict(pool_stats),
                                ))
                        except Exception as err:
                            failure = "CANDIDATE_PROCESSING_TIMEOUT" if isinstance(err, TimeoutError) or monotonic() - candidate_started >= per_candidate_timeout_seconds else type(err).__name__
                            repository.save_transient(TransientFailureRecord(name, candidate.provider_asset_id, candidate.provider_variant_id, failure, utc_now_iso()))
                            metric = replace(metric, transient_failures=metric.transient_failures + 1)
                            status = persist(replace(
                                status,
                                transient_failures=status.transient_failures + 1,
                                current_candidate=None,
                                stats=increment_pool_stat("transient_failures"),
                                message=f"Candidate failed transiently ({failure}).",
                            ))
                        finally:
                            if downloaded is not None:
                                try:
                                    os.remove(downloaded.local_path)
                                except FileNotFoundError:
                                    pass
                            metric = replace(metric, updated_at=utc_now_iso())
                            metric_map[(name, query)] = metric
                            repository.save_query_metrics(metric)
                    repository.save_query_metrics(metric)
                if status.found_safe >= add_safe:
                    break
            if status.found_safe >= add_safe:
                break
        status = finalize(replace(status, state="completed" if status.found_safe >= add_safe else "candidate_exhausted", message=f"Added {status.found_safe} new verified safe clips." if status.found_safe >= add_safe else "Bounded discovery exhausted before target.", error_code=None if status.found_safe >= add_safe else "POOL_CANDIDATES_EXHAUSTED"))
        return status


def install_stop_signal_handlers(controller: StopController) -> None:
    signal.signal(signal.SIGINT, controller.request)
    signal.signal(signal.SIGTERM, controller.request)
