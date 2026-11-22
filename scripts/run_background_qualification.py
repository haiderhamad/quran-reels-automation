#!/usr/bin/env python3
"""Run bounded, cloud-compatible background qualification."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.background_candidates import (
    BACKGROUND_SELECTABLE_TARGET,
    DEFAULT_LEASE_SECONDS,
    cloud_worker_id,
    qualify_one_claimed_candidate,
)
from engine.background_pool import (
    get_background_pool_repository,
    verify_current_safety_model_files,
)
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from scripts.build_verified_background_pool import auto_load_env_if_needed


def _validate_qualification_env() -> None:
    missing: list[str] = []
    if not os.environ.get("SUPABASE_URL", "").strip():
        missing.append("SUPABASE_URL")
    if not (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.environ.get("SUPABASE_KEY", "").strip()
    ):
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claim and qualify queued background candidates with bounded execution."
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=3,
        help="Maximum candidates to qualify in this job run (default: 3).",
    )
    parser.add_argument(
        "--target-selectable",
        type=int,
        default=BACKGROUND_SELECTABLE_TARGET,
        help="Adaptive target for verified selectable assets (default: 100).",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_LEASE_SECONDS,
        help=f"Lease duration in seconds (default: {DEFAULT_LEASE_SECONDS}).",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=None,
        help="Optional custom worker identifier.",
    )
    parser.add_argument(
        "--work-dir",
        type=str,
        default=None,
        help="Optional scratch directory for candidate downloads.",
    )
    args = parser.parse_args(argv)

    if args.max_candidates < 1:
        parser.error("--max-candidates must be at least 1")
    if args.lease_seconds < 60:
        parser.error("--lease-seconds must be at least 60")

    auto_load_env_if_needed()
    _validate_qualification_env()
    verify_current_safety_model_files()

    store = SupabaseStateStore(SupabaseConfig.from_env())

    # Adaptive pool ceiling check
    selectable_count = store.count_selectable_background_assets()
    print(
        f"[BACKGROUND_QUALIFICATION] Current selectable pool: {selectable_count} "
        f"(target: {args.target_selectable})"
    )
    if selectable_count >= args.target_selectable:
        print(
            f"[BACKGROUND_QUALIFICATION] POOL_TARGET_MET: selectable {selectable_count} "
            f">= target {args.target_selectable}. Exiting successfully without downloading/scanning."
        )
        return 0

    worker = args.worker_id or cloud_worker_id()
    repository = get_background_pool_repository(supabase_store=store)

    scratch_base = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="bg-qual-"))
    scratch_base.mkdir(parents=True, exist_ok=True)
    custom_scratch = bool(args.work_dir)

    processed = 0
    try:
        while processed < args.max_candidates:
            # Check selectable pool before each candidate claim
            if store.count_selectable_background_assets() >= args.target_selectable:
                print(
                    f"[BACKGROUND_QUALIFICATION] POOL_TARGET_MET during run. "
                    f"Processed {processed} candidate(s)."
                )
                break

            result = qualify_one_claimed_candidate(
                store=store,
                pool_repository=repository,
                worker_id=worker,
                lease_seconds=args.lease_seconds,
                work_dir=str(scratch_base),
            )
            if result is None:
                print("[BACKGROUND_QUALIFICATION] QUEUE_EMPTY: No pending candidates to claim.")
                break

            processed += 1
            status = result.get("status")
            cid = result.get("candidate_id")
            err = result.get("last_error_code")
            print(
                f"[BACKGROUND_QUALIFICATION] Candidate {processed}/{args.max_candidates} "
                f"candidate_id={cid} final_status={status} error_code={err}"
            )
    finally:
        if not custom_scratch and scratch_base.exists():
            shutil.rmtree(scratch_base, ignore_errors=True)

    print(f"[BACKGROUND_QUALIFICATION] Finished. Candidates processed: {processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
