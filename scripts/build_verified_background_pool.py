#!/usr/bin/env python3
"""Build verified background-pool metadata locally; source videos remain temporary."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
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
from engine.state_authority import STATE_AUTHORITY_SUPABASE, get_state_authority


REQUIRED_B2_ENV_NAMES = (
    "B2_ENDPOINT",
    "B2_REGION",
    "B2_APPLICATION_KEY_ID",
    "B2_APPLICATION_KEY",
    "B2_BUCKET_NAME",
)


def _default_local_path(name: str) -> str:
    return str(REPOSITORY_ROOT / ".local" / "background-pool-builder" / name)


_ENV_SENTINELS = frozenset({"[SENSITIVE]", "[REDACTED]", "<REDACTED>"})


def _usable_env_value(value: str) -> str | None:
    """Return a usable local env value, excluding redaction/placeholder markers."""
    normalized = value.strip().strip("\"'")
    if not normalized or normalized.upper() in {item.upper() for item in _ENV_SENTINELS}:
        return None
    lowered = normalized.lower()
    if lowered.startswith(("placeholder_", "your_", "change_me")):
        return None
    return normalized


def auto_load_env_if_needed(env_paths: Sequence[Path] | None = None) -> None:
    """Load missing usable variables from standard local env files.

    Values are copied only into the current process. Redaction and obvious
    placeholder markers are ignored so they cannot mask a valid default or
    create a misleading authentication configuration.
    """
    paths = list(env_paths) if env_paths is not None else [
        REPOSITORY_ROOT / ".env",
        REPOSITORY_ROOT / "apps" / "dashboard" / ".env.local",
    ]
    for env_path in paths:
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        value = _usable_env_value(v)
                        if k and k not in os.environ and value is not None:
                            os.environ[k] = value
            except OSError:
                pass


def _validate_environment(provider: str = "both") -> None:
    provider_names = {
        "pexels": ("PEXELS_API_KEY",),
        "pixabay": ("PIXABAY_API_KEY",),
        "both": ("PEXELS_API_KEY", "PIXABAY_API_KEY"),
    }
    authority = get_state_authority()
    missing: list[str] = []
    if authority == STATE_AUTHORITY_SUPABASE:
        if not os.environ.get("SUPABASE_URL", "").strip():
            missing.append("SUPABASE_URL")
        has_key = bool(
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.environ.get("SUPABASE_KEY", "").strip()
        )
        if not has_key:
            missing.append("SUPABASE_SERVICE_ROLE_KEY")
    else:
        missing.extend(
            name for name in REQUIRED_B2_ENV_NAMES if not os.environ.get(name, "").strip()
        )

    missing.extend(
        name for name in provider_names[provider] if not os.environ.get(name, "").strip()
    )
    if missing:
        raise RuntimeError(f"LOCAL_POOL_CONFIG_MISSING:{','.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add Pexels/Pixabay no-person/no-face/no-animal evidence to pool V2.")
    parser.add_argument("--add-safe", type=int, required=True, help="Number of NEW verified safe records to add.")
    parser.add_argument("--provider", choices=["both", "pexels", "pixabay"], default="both")
    parser.add_argument("--max-candidates", type=int, default=2000)
    parser.add_argument("--minimum-duration-seconds", type=float, default=15.0)
    parser.add_argument("--per-candidate-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--time-budget-seconds", type=float, default=None)
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Optional allowed nature query; repeat to run a bounded curated subset.",
    )
    parser.add_argument("--status-file", default=_default_local_path("status.json"))
    parser.add_argument("--stop-file", default=_default_local_path("stop.request"))
    parser.add_argument("--lock-file", default=_default_local_path("builder.lock"))
    parser.add_argument("--work-dir", default=_default_local_path("work"))
    args = parser.parse_args(argv)

    try:
        auto_load_env_if_needed()
        _validate_environment(args.provider)
        verify_current_safety_model_files()
        stop = StopController(args.stop_file)
        install_stop_signal_handlers(stop)
        if stop.stop_file is not None:
            try:
                stop.stop_file.unlink()
            except FileNotFoundError:
                pass
        repository = get_background_pool_repository()
        provider = (
            {name: make_provider(name) for name in ("pexels", "pixabay")}
            if args.provider == "both"
            else make_provider(args.provider)
        )
        try:
            result = build_verified_pool(
                repository=repository,
                provider=provider,
                add_safe=args.add_safe,
                provider_name=args.provider,
                max_candidates=args.max_candidates,
                minimum_duration_seconds=args.minimum_duration_seconds,
                per_candidate_timeout_seconds=args.per_candidate_timeout_seconds,
                time_budget_seconds=args.time_budget_seconds,
                query_subset=args.queries,
                status_writer=AtomicStatusWriter(args.status_file),
                stop_controller=stop,
                lock_file=args.lock_file,
                work_dir=args.work_dir,
            )
        finally:
            shutil.rmtree(args.work_dir, ignore_errors=True)
        print(
            f"[POOL] state={result.state} safe={result.found_safe}/{result.target_new_safe} "
            f"processed={result.candidates_processed} rejected={result.rejected} "
            f"transient={result.transient_failures}"
        )
        return 0
    except Exception as err:
        print(f"[POOL] technical_error={err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
