#!/usr/bin/env python3
"""Optional outbound-only Mac worker for shared background-candidate leases."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.background_candidates import DEFAULT_LEASE_SECONDS, local_worker_id, qualify_one_claimed_candidate
from engine.background_pool import get_background_pool_repository, verify_current_safety_model_files
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from scripts.build_verified_background_pool import _validate_environment, auto_load_env_if_needed


def qualify_single_candidate(
    *,
    store: Optional[SupabaseStateStore] = None,
    pool_repository: Any = None,
    worker_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    work_dir: Optional[str | Path] = None,
    download_fn: Any = None,
    scan_fn: Any = None,
    logger: Callable[[str], None] = print,
) -> Optional[dict[str, Any]]:
    """Claim and qualify exactly one candidate, returning None when queue has no claimable items."""
    auto_load_env_if_needed()
    _validate_environment("both")
    verify_current_safety_model_files()
    state_store = store if store is not None else SupabaseStateStore(SupabaseConfig.from_env())
    repo = pool_repository if pool_repository is not None else get_background_pool_repository(supabase_store=state_store)
    worker = worker_id or local_worker_id()
    target_work_dir = Path(work_dir) if work_dir else ROOT / ".local" / "background-worker" / "work"
    target_work_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "store": state_store,
        "pool_repository": repo,
        "worker_id": worker,
        "lease_seconds": lease_seconds,
        "work_dir": str(target_work_dir),
        "logger": logger,
    }
    if download_fn is not None:
        kwargs["download_fn"] = download_fn
    if scan_fn is not None:
        kwargs["scan_fn"] = scan_fn
    return qualify_one_claimed_candidate(**kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify queued background candidates without opening an inbound port.")
    parser.add_argument("--duration-minutes", type=float, default=60.0)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--max-jobs", type=int, default=None, help="Optional bounded validation limit.")
    args = parser.parse_args(argv)
    if args.duration_minutes <= 0 or args.lease_seconds < 60:
        parser.error("duration-minutes must be positive and lease-seconds must be at least 60.")
    try:
        auto_load_env_if_needed()
        _validate_environment("both")
        verify_current_safety_model_files()
        store = SupabaseStateStore(SupabaseConfig.from_env())
        repository = get_background_pool_repository(supabase_store=store)
        worker = local_worker_id()
        deadline = time.monotonic() + args.duration_minutes * 60
        jobs = 0
        work_dir = ROOT / ".local" / "background-worker" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        while time.monotonic() < deadline and (args.max_jobs is None or jobs < args.max_jobs):
            item = qualify_single_candidate(
                store=store,
                pool_repository=repository,
                worker_id=worker,
                lease_seconds=args.lease_seconds,
                work_dir=work_dir,
            )
            if item is None:
                print("BACKGROUND_WORKER_QUEUE_EMPTY")
                break
            jobs += 1
            print(f"BACKGROUND_WORKER_COMPLETED status={item.get('status')} candidate={item.get('candidate_id')}")
        print(f"BACKGROUND_WORKER_STOPPED jobs={jobs} worker={worker}")
        return 0
    except Exception as err:
        print(f"LOCAL_WORKER_ERROR:{type(err).__name__}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(ROOT / ".local" / "background-worker" / "work", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
