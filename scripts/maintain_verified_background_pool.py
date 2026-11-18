#!/usr/bin/env python3
"""Bounded non-interactive verified-pool maintenance command for manual CI runs."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.background_pool import (
    get_background_pool_repository,
    make_provider,
    verify_current_safety_model_files,
)
from engine.background_pool_builder import (
    AtomicStatusWriter,
    StopController,
    build_verified_pool,
    install_stop_signal_handlers,
)
from scripts.build_verified_background_pool import _validate_environment, auto_load_env_if_needed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain the verified background pool within fixed budgets.")
    parser.add_argument("--minimum-safe", type=int, default=100)
    parser.add_argument("--target-safe", type=int, default=150)
    parser.add_argument("--max-new-safe", type=int, default=1)
    parser.add_argument("--time-budget-seconds", type=float, default=600.0)
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--provider", choices=["both", "pexels", "pixabay"], default="both")
    args = parser.parse_args(argv)
    if args.minimum_safe < 0 or args.target_safe < args.minimum_safe or args.max_new_safe < 1:
        parser.error("Require 0 <= minimum-safe <= target-safe and max-new-safe >= 1.")

    try:
        auto_load_env_if_needed()
        _validate_environment(args.provider)
        verify_current_safety_model_files()
        repository = get_background_pool_repository()
        stats = repository.compute_stats()
        current = int(stats["verified_current_policy"])
        if current >= args.target_safe:
            print(f"POOL_HEALTHY current={current} target={args.target_safe}")
            return 0
        needed = min(args.max_new_safe, max(1, args.target_safe - current))
        stop = StopController()
        install_stop_signal_handlers(stop)
        result = build_verified_pool(
            repository=repository,
            provider=(
                {name: make_provider(name) for name in ("pexels", "pixabay")}
                if args.provider == "both"
                else make_provider(args.provider)
            ),
            add_safe=needed,
            provider_name=args.provider,
            max_candidates=args.max_candidates,
            time_budget_seconds=args.time_budget_seconds,
            status_writer=AtomicStatusWriter(None),
            stop_controller=stop,
        )
        print(
            f"POOL_MAINTENANCE state={result.state} added={result.found_safe} "
            f"processed={result.candidates_processed} current_before={current}"
        )
        return 0
    except Exception as err:
        print(f"POOL_MAINTENANCE_TECHNICAL_ERROR:{err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
