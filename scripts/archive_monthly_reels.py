#!/usr/bin/env python3
"""Plan a monthly production-Reel archive; live archive is deliberately gated."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.archive import build_monthly_archive_plan
from engine.supabase_state import SupabaseConfig, SupabaseStateStore


def auto_load_env_if_needed() -> None:
    """Load missing environment variables from standard local env files."""
    for env_path in [REPOSITORY_ROOT / ".env", REPOSITORY_ROOT / "apps" / "dashboard" / ".env.local"]:
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        if k not in os.environ:
                            os.environ[k] = v.strip().strip("\"'")
            except OSError:
                pass


def previous_berlin_month(now: datetime.datetime | None = None) -> tuple[int, int]:
    current = now or datetime.datetime.now(ZoneInfo("Europe/Berlin"))
    first = current.replace(day=1)
    previous = first - datetime.timedelta(days=1)
    return previous.year, previous.month


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if (args.year is None) != (args.month is None):
        parser.error("--year and --month must be supplied together.")
    year, month = (args.year, args.month) if args.year else previous_berlin_month()
    if not args.dry_run:
        parser.error("Live archive is disabled until a manual archive approval; use --dry-run.")

    auto_load_env_if_needed()
    store = SupabaseStateStore(SupabaseConfig.from_env())
    plan = build_monthly_archive_plan(year, month, store.list_successful_production_runs(year, month))
    print(json.dumps({"mode": "DRY_RUN", "month": plan.month_key, "archive_name": plan.archive_name, "eligible_reel_count": len(plan.entries), "run_ids": [record.run_id for record in plan.entries], "b2_objects": [record.media_key for record in plan.entries], "deletions": 0}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
