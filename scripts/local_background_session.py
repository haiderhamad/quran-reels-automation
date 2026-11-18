#!/usr/bin/env python3
"""Optional local Mac background discovery + qualification session orchestrator.

Coordinates bounded background discovery and OpenCV qualification for a chosen
session duration (e.g. 30, 60, 100, 120 minutes) using the shared Supabase queue.
Qualification dominates session time; discovery runs only as a low-water replenishment.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import signal
import sys
import time
from typing import Any, Callable, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.background_candidates import (
    DEFAULT_LEASE_SECONDS,
    local_worker_id,
)
from engine.background_pool import (
    get_background_pool_repository,
    verify_current_safety_model_files,
)
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from scripts.build_verified_background_pool import _validate_environment, auto_load_env_if_needed
from scripts.discover_background_candidates import run_discovery_step
from scripts.local_background_worker import qualify_single_candidate

LOCAL_DISCOVERY_LOW_WATER_PENDING = 10
LOCAL_DISCOVERY_COOLDOWN_MINUTES = 15.0
LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS = 60.0
LOCAL_DISCOVERY_BATCH_SIZE = 40
DEFAULT_SESSION_DURATION_MINUTES = 60.0
MINIMUM_REMAINING_FOR_JOB_SECONDS = 15.0
DEFAULT_TRANSIENT_RETRY_SECONDS = 5.0

TRANSIENT_NETWORK_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    TimeoutError,
    ConnectionError,
)


def run_local_background_session(
    duration_minutes: float,
    *,
    low_water_pending: int = LOCAL_DISCOVERY_LOW_WATER_PENDING,
    discovery_cooldown_minutes: float = LOCAL_DISCOVERY_COOLDOWN_MINUTES,
    successful_discovery_cooldown_seconds: float = LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS,
    max_jobs: Optional[int] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    provider: str = "both",
    store: Optional[SupabaseStateStore] = None,
    pool_repository: Any = None,
    worker_id: Optional[str] = None,
    work_dir: Optional[str | Path] = None,
    discovery_fn: Optional[Callable[..., dict[str, Any]]] = None,
    qualification_fn: Optional[Callable[..., Optional[dict[str, Any]]]] = None,
    transient_retry_seconds: float = DEFAULT_TRANSIENT_RETRY_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    logger: Callable[[str], None] = print,
    stop_check: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Orchestrate a bounded local qualification and replenishment session."""
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be a positive number.")
    if low_water_pending < 0:
        raise ValueError("low_water_pending must be non-negative.")
    if discovery_cooldown_minutes < 0:
        raise ValueError("discovery_cooldown_minutes must be non-negative.")
    if successful_discovery_cooldown_seconds < 0:
        raise ValueError("successful_discovery_cooldown_seconds must be non-negative.")
    if transient_retry_seconds < 0:
        raise ValueError("transient_retry_seconds must be non-negative.")

    auto_load_env_if_needed()
    _validate_environment(provider)
    verify_current_safety_model_files()

    state_store = store if store is not None else SupabaseStateStore(SupabaseConfig.from_env())
    repo = pool_repository if pool_repository is not None else get_background_pool_repository(supabase_store=state_store)
    active_worker_id = worker_id or local_worker_id()

    session_work_dir = Path(work_dir) if work_dir else ROOT / ".local" / "background-session" / "work"
    session_work_dir.mkdir(parents=True, exist_ok=True)

    session_start = monotonic()
    session_deadline = session_start + (duration_minutes * 60.0)
    empty_discovery_cooldown_seconds = discovery_cooldown_minutes * 60.0
    next_discovery_time = -float("inf")

    jobs_completed = 0
    discovery_runs = 0
    candidates_discovered = 0

    def _handle_transient_error(err: Exception) -> bool:
        """Log sanitized transient error, wait bounded delay, and return False if session should stop."""
        if stop_check is not None and stop_check():
            logger("[LOCAL SESSION] Stop requested. Stopping cleanly.")
            return False
        now = monotonic()
        remaining = session_deadline - now
        if remaining <= 0:
            logger(
                f"[LOCAL SESSION] Transient Supabase/network error: {type(err).__name__}. "
                "Session duration reached."
            )
            return False
        wait_seconds = max(0.1, min(transient_retry_seconds, remaining))
        logger(
            f"[LOCAL SESSION] Transient Supabase/network error: {type(err).__name__}. "
            f"Retrying after {wait_seconds:g}s."
        )
        sleep_fn(wait_seconds)
        if stop_check is not None and stop_check():
            logger("[LOCAL SESSION] Stop requested. Stopping cleanly.")
            return False
        return True

    logger(f"[LOCAL SESSION] Started. Duration: {duration_minutes:g} minutes")

    try:
        while True:
            if stop_check is not None and stop_check():
                logger("[LOCAL SESSION] Stop requested. Stopping cleanly.")
                break

            now = monotonic()
            remaining_seconds = session_deadline - now
            if remaining_seconds <= 0:
                logger("[LOCAL SESSION] Duration reached. Stopping cleanly.")
                break

            if max_jobs is not None and jobs_completed >= max_jobs:
                logger(f"[LOCAL SESSION] Max jobs ({max_jobs}) reached. Stopping cleanly.")
                break

            # Check queue depth
            try:
                summary = state_store.background_candidate_summary()
            except TRANSIENT_NETWORK_ERRORS as err:
                if not _handle_transient_error(err):
                    break
                continue
            pending_count = int(summary.get("pending", 0))
            logger(f"[QUEUE] Pending: {pending_count}")

            in_cooldown = now < next_discovery_time

            # Low-water discovery replenishment
            if pending_count < low_water_pending:
                if not in_cooldown:
                    logger(f"[DISCOVERY] Low-water threshold ({low_water_pending}) reached (pending={pending_count}).")
                    logger("[DISCOVERY] Starting bounded discovery ...")
                    disc_call = discovery_fn or (
                        lambda: run_discovery_step(
                            store=state_store,
                            provider=provider,
                            max_candidates=LOCAL_DISCOVERY_BATCH_SIZE,
                            logger=logger,
                        )
                    )
                    try:
                        disc_res = disc_call()
                    except TRANSIENT_NETWORK_ERRORS as err:
                        next_discovery_time = monotonic() + empty_discovery_cooldown_seconds
                        if not _handle_transient_error(err):
                            break
                        continue
                    discovery_runs += 1
                    added = int(disc_res.get("discovered", 0))
                    candidates_discovered += added
                    cooldown_after_run = (
                        successful_discovery_cooldown_seconds
                        if added > 0
                        else empty_discovery_cooldown_seconds
                    )
                    next_discovery_time = monotonic() + cooldown_after_run
                    in_cooldown = True
                    logger(f"[DISCOVERY] Added {added} new candidates.")

                    if added == 0 and pending_count == 0:
                        logger("[DISCOVERY] No new candidates found.")
                        logger("[LOCAL SESSION] Waiting for cooldown / queue replenishment.")
                        wait_seconds = min(15.0, max(1.0, session_deadline - monotonic()))
                        sleep_fn(wait_seconds)
                        continue
                else:
                    remaining_cd = next_discovery_time - now
                    if pending_count == 0:
                        logger(f"[DISCOVERY] Queue empty, discovery in cooldown ({remaining_cd:.1f}s remaining).")
                        logger("[LOCAL SESSION] Waiting for cooldown / queue replenishment.")
                        wait_seconds = min(remaining_cd, 15.0, max(1.0, session_deadline - monotonic()))
                        sleep_fn(wait_seconds)
                        continue

            # Ensure adequate time buffer for next job
            if (session_deadline - monotonic()) < MINIMUM_REMAINING_FOR_JOB_SECONDS:
                logger(
                    f"[LOCAL SESSION] Insufficient remaining session time (<{MINIMUM_REMAINING_FOR_JOB_SECONDS}s) "
                    "for new job. Stopping cleanly."
                )
                break

            # Qualification step
            logger("[WORKER] Claiming candidate ...")
            qual_call = qualification_fn or (
                lambda: qualify_single_candidate(
                    store=state_store,
                    pool_repository=repo,
                    worker_id=active_worker_id,
                    lease_seconds=lease_seconds,
                    work_dir=session_work_dir,
                    logger=logger,
                )
            )
            try:
                item = qual_call()
            except TRANSIENT_NETWORK_ERRORS as err:
                if not _handle_transient_error(err):
                    break
                continue

            if item is not None:
                jobs_completed += 1
                status = item.get("status", "UNKNOWN")
                cand_id = item.get("candidate_id", "N/A")
                logger(f"[WORKER] Qualified candidate status={status} candidate={cand_id}")
            else:
                logger("[WORKER] No claimable candidates available.")
                wait_seconds = min(15.0, max(1.0, session_deadline - monotonic()))
                sleep_fn(wait_seconds)
                continue

            now_after = monotonic()
            rem_minutes = max(0.0, (session_deadline - now_after) / 60.0)
            logger(f"[LOCAL SESSION] Remaining: {rem_minutes:.1f} minutes | Qualified: {jobs_completed}")

    finally:
        shutil.rmtree(session_work_dir, ignore_errors=True)

    elapsed_minutes = (monotonic() - session_start) / 60.0
    summary_result = {
        "status": "COMPLETED",
        "duration_minutes": duration_minutes,
        "elapsed_minutes": round(elapsed_minutes, 2),
        "jobs_completed": jobs_completed,
        "discovery_runs": discovery_runs,
        "candidates_discovered": candidates_discovered,
    }
    logger(
        f"[LOCAL SESSION] Session finished. elapsed={summary_result['elapsed_minutes']}m "
        f"jobs_qualified={jobs_completed} discovery_passes={discovery_runs} candidates_added={candidates_discovered}"
    )
    return summary_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run optional local Mac background discovery + qualification session for a chosen duration."
    )
    parser.add_argument(
        "--duration-minutes",
        type=float,
        required=True,
        help="Total session wall-clock duration in minutes (e.g. 30, 60, 100, 120).",
    )
    parser.add_argument(
        "--discovery-low-water",
        type=int,
        default=LOCAL_DISCOVERY_LOW_WATER_PENDING,
        help=f"Low-water mark for pending candidates to trigger discovery (default: {LOCAL_DISCOVERY_LOW_WATER_PENDING}).",
    )
    parser.add_argument(
        "--discovery-cooldown-minutes",
        type=float,
        default=LOCAL_DISCOVERY_COOLDOWN_MINUTES,
        help=(
            "Cooldown after zero-result or transiently failed discovery passes in minutes "
            f"(default: {LOCAL_DISCOVERY_COOLDOWN_MINUTES})."
        ),
    )
    parser.add_argument(
        "--successful-discovery-cooldown-seconds",
        type=float,
        default=LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS,
        help=(
            "Short replenishment guard after discovery adds useful candidates in seconds "
            f"(default: {LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Optional upper limit on number of candidate jobs to qualify before stopping.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_LEASE_SECONDS,
        help=f"Lease time in seconds per claimed candidate (default: {DEFAULT_LEASE_SECONDS}).",
    )
    parser.add_argument(
        "--provider",
        choices=("both", "pexels", "pixabay"),
        default="both",
        help="Discovery providers to query (default: both).",
    )
    args = parser.parse_args(argv)

    if args.duration_minutes <= 0:
        parser.error("--duration-minutes must be positive.")
    if args.discovery_low_water < 0:
        parser.error("--discovery-low-water must be non-negative.")
    if args.discovery_cooldown_minutes < 0:
        parser.error("--discovery-cooldown-minutes must be non-negative.")
    if args.successful_discovery_cooldown_seconds < 0:
        parser.error("--successful-discovery-cooldown-seconds must be non-negative.")

    stop_requested = False

    def handle_sigint(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        print("\n[LOCAL SESSION] Ctrl+C received. Finishing current step and exiting cleanly...", file=sys.stderr)

    original_handler = signal.signal(signal.SIGINT, handle_sigint)

    try:
        run_local_background_session(
            duration_minutes=args.duration_minutes,
            low_water_pending=args.discovery_low_water,
            discovery_cooldown_minutes=args.discovery_cooldown_minutes,
            successful_discovery_cooldown_seconds=args.successful_discovery_cooldown_seconds,
            max_jobs=args.max_jobs,
            lease_seconds=args.lease_seconds,
            provider=args.provider,
            stop_check=lambda: stop_requested,
        )
        return 0
    except KeyboardInterrupt:
        print("\n[LOCAL SESSION] Interrupted by user. Exiting cleanly.", file=sys.stderr)
        return 0
    except Exception as err:
        print(f"[LOCAL SESSION ERROR] {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, original_handler)


if __name__ == "__main__":
    raise SystemExit(main())
