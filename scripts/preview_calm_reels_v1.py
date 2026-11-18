#!/usr/bin/env python3
"""Read-only owner v1 publication progress; never generate, reserve, or publish.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the PRIVATE operator
environment. The script does not print keys, server URL or raw receipt IDs.
Do not invoke with public CI credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from engine.calm_reels_cycle import FreshCycleError, preview_next_owner_ayah_from_store

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ENV_PATHS = (ROOT / ".env", ROOT / "apps" / "dashboard" / ".env.local")
SUPABASE_ENV_KEYS = frozenset(("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"))
MAX_ENV_FILE_BYTES = 128 * 1024


class PreviewConfigError(ValueError):
    """A missing or invalid local-only configuration (no credential values)."""


def _local_supabase_environment(
    *, env: Mapping[str, str] | None = None,
    paths: Sequence[Path] | None = None,
) -> dict[str, str]:
    """Read ONLY the two required settings, without modifying process env.

    Existing nonempty process variables win; missing values are read from
    project-local private env files, never from public templates. The files
    must be regular, non-symlink, reasonably small and unreadable errors stay
    sanitized. No credential content is ever printed or serialized.
    """
    values = dict(os.environ if env is None else env)
    missing = {name for name in SUPABASE_ENV_KEYS if not values.get(name, "").strip()}
    if missing:
        for path in PRIVATE_ENV_PATHS if paths is None else paths:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ENV_FILE_BYTES:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw = line.split("=", 1)
                key = key.strip()
                if key not in missing:
                    continue
                value = raw.strip()
                if value.startswith(('"', "'")) and value.endswith(value[:1]):
                    value = value[1:-1]
                if not value or value.upper() in {"[REDACTED]", "[SENSITIVE]"}:
                    continue
                if value.lower().startswith(("your_", "placeholder_", "change_me")):
                    continue
                values[key] = value
                missing.discard(key)
            if not missing:
                break
    if missing:
        raise PreviewConfigError("LOCAL_SUPABASE_CONFIG_MISSING")
    if not values["SUPABASE_URL"].startswith("https://"):
        raise PreviewConfigError("LOCAL_SUPABASE_URL_INVALID")
    return values


def _safe_error_code(error: Exception) -> str:
    """Stable diagnostic classes, never raw provider messages."""
    from engine.supabase_state import SupabaseStateError

    if isinstance(error, PreviewConfigError):
        return str(error)
    if isinstance(error, FreshCycleError):
        return "FRESH_CYCLE_INVALID"
    if isinstance(error, SupabaseStateError):
        # The raw exception may include sensitive transport details.
        text = str(error)
        match = re.search(r"HTTP (4[0-9]{2}|5[0-9]{2})(?![0-9])", text)
        if match:
            status = int(match.group(1))
            if status in (401, 403):
                return "SUPABASE_AUTH_FAILED"
            if status == 404:
                return "SUPABASE_RESOURCE_NOT_FOUND"
            return "SUPABASE_HTTP_FAILURE"
        return "SUPABASE_REQUEST_FAILED"
    return "PREVIEW_UNEXPECTED_FAILURE"


def preview_payload(store: object) -> dict[str, object]:
    decision = preview_next_owner_ayah_from_store(store)
    return {
        "catalog": "calm-reels-editorial-shortlist-v1",
        "state": decision.state,
        "cycle_number": decision.cycle_number,
        "confirmed": decision.confirmed_count,
        "total": 2211,
        "next_ayah_key": decision.next_ayah_key,
        "publication_enabled": False,
        "writes_performed": False,
    }


def main(argv: Sequence[str] | None = None, *, store: object | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        if store is None:
            from engine.supabase_state import SupabaseConfig, SupabaseStateStore

            store = SupabaseStateStore(
                SupabaseConfig.from_env(_local_supabase_environment())
            )
        data = preview_payload(store)
    except Exception as error:
        # Provider exceptions may include tokens, URLs or sensitive response
        # bodies: output only a fixed, non-sensitive error classification.
        print(json.dumps({
            "state": "UNAVAILABLE",
            "error_code": _safe_error_code(error),
            "publication_enabled": False,
            "writes_performed": False,
        }, sort_keys=True), file=sys.stdout)
        return 2
    print(json.dumps(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
