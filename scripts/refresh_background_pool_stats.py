#!/usr/bin/env python3
"""Reconcile verified-pool totals once without starting a media scan."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.background_pool import (
    get_background_pool_repository,
    utc_now_iso,
)
from scripts.build_verified_background_pool import auto_load_env_if_needed


def _write_status(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def refresh_authoritative_stats(
    repository: Any,
) -> dict[str, object]:
    """Perform the one explicit reconciliation used by manual Refresh."""
    stats = repository.compute_stats()
    repository.save_stats(stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Perform one authoritative pool totals reconciliation."
    )
    parser.add_argument("--status-file", required=True)
    args = parser.parse_args(argv)

    try:
        auto_load_env_if_needed()
        repository = get_background_pool_repository()
        stats = refresh_authoritative_stats(repository)
        status_path = Path(args.status_file).resolve()
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            status = raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            status = {}
        status.update(
            {
                "status_schema_version": max(
                    3, int(status.get("status_schema_version", 0) or 0)
                ),
                "stats": stats,
                "b2_operations": repository.operation_counts(),
                "message": "Pool totals refreshed from authoritative B2 records.",
                "updated_at": utc_now_iso(),
            }
        )
        status.setdefault("state", "idle")
        _write_status(status_path, status)
        print(json.dumps({"stats": stats, "b2_operations": repository.operation_counts()}))
        return 0
    except Exception as err:
        print(f"[POOL_REFRESH] technical_error={err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
