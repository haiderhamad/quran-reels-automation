"""Read-only verification of Quran Foundation production Content API v4 access and dataset integrity."""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional

from scripts.build_verified_background_pool import auto_load_env_if_needed

from engine.quran import (
    EXPECTED_SURAH_COUNT,
    EXPECTED_TOTAL_AYAH_COUNT,
    STANDARD_SURAH_AYAH_COUNTS,
    QuranData,
    QuranSourceError,
    fetch_quran_data,
)

# Sanitized error classifications
ERR_AUTH_FAILED = "QF_PRODUCTION_AUTH_FAILED"
ERR_ACCESS_DENIED = "QF_PRODUCTION_ACCESS_DENIED"
ERR_DATASET_INVALID = "QF_PRODUCTION_DATASET_INVALID"
ERR_NETWORK_ERROR = "QF_PRODUCTION_NETWORK_ERROR"


def classify_qf_error(exc: Exception) -> str:
    """Classify an exception into a sanitized, non-sensitive failure code.

    Never echoes raw credentials, tokens, or request payloads.
    """
    msg = str(exc).lower()
    if any(term in msg for term in ("401", "unauthorized", "missing quran foundation credentials", "invalid_client", "bad request")):
        return ERR_AUTH_FAILED
    if any(term in msg for term in ("403", "forbidden", "access_denied")):
        return ERR_ACCESS_DENIED
    if any(term in msg for term in ("network error", "timed out", "timeout", "connection", "ssl", "500", "502", "503", "504")):
        return ERR_NETWORK_ERROR
    return ERR_DATASET_INVALID


def verify_production_quran_dataset(
    quran_data: QuranData,
) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Verify structural canonical integrity of a retrieved QuranData instance.

    Verifies:
    1. source.environment == "production"
    2. total_surahs == 114
    3. total_ayahs == 6236
    4. actual ayah count == expected canonical ayah count for every Surah (1..114)
    """
    diagnostics: dict[str, Any] = {
        "environment": getattr(getattr(quran_data, "source", None), "environment", None),
        "surahs": getattr(quran_data, "total_surahs", 0),
        "ayahs": getattr(quran_data, "total_ayahs", 0),
        "canonical_counts": "FAIL",
    }

    if quran_data is None or not hasattr(quran_data, "source"):
        return False, f"{ERR_DATASET_INVALID}: missing or null Quran dataset", diagnostics

    env = quran_data.source.environment
    if env != "production":
        return (
            False,
            f"{ERR_DATASET_INVALID}: environment is '{env}', expected 'production'",
            diagnostics,
        )

    if quran_data.total_surahs != EXPECTED_SURAH_COUNT:
        return (
            False,
            f"{ERR_DATASET_INVALID}: Surah count {quran_data.total_surahs} != {EXPECTED_SURAH_COUNT}",
            diagnostics,
        )

    if quran_data.total_ayahs != EXPECTED_TOTAL_AYAH_COUNT:
        return (
            False,
            f"{ERR_DATASET_INVALID}: Ayah count {quran_data.total_ayahs} != {EXPECTED_TOTAL_AYAH_COUNT}",
            diagnostics,
        )

    for surah_num in range(1, EXPECTED_SURAH_COUNT + 1):
        try:
            surah = quran_data.get_surah(surah_num)
        except Exception:
            return (
                False,
                f"{ERR_DATASET_INVALID}: Surah {surah_num} cannot be retrieved",
                diagnostics,
            )
        expected_count = STANDARD_SURAH_AYAH_COUNTS.get(surah_num)
        actual_count = surah.number_of_ayahs
        if actual_count != expected_count:
            return (
                False,
                f"{ERR_DATASET_INVALID}: Surah {surah_num} has {actual_count} ayahs, expected canonical {expected_count}",
                diagnostics,
            )
        if len(surah.ayahs) != expected_count:
            return (
                False,
                f"{ERR_DATASET_INVALID}: Surah {surah_num} ayahs tuple length {len(surah.ayahs)} != canonical {expected_count}",
                diagnostics,
            )

    diagnostics["canonical_counts"] = "PASS"
    return True, None, diagnostics


def run_qf_production_verification(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    fetch_fn: Callable[..., QuranData] = fetch_quran_data,
) -> int:
    """Execute read-only Quran Foundation production source verification.

    Outputs only non-sensitive diagnostics. Never prints secrets, tokens,
    headers, or Quran text. Exits non-zero on any failure.
    """
    print("==================================================")
    print("Quran Foundation Production Source Verification (Read-Only)")
    print("==================================================")

    resolved_id = client_id or os.getenv("QF_CLIENT_ID")
    resolved_secret = client_secret or os.getenv("QF_CLIENT_SECRET")
    print(f"QF_CLIENT_ID: {'PRESENT' if resolved_id and resolved_id.strip() else 'MISSING'}")
    print(f"QF_CLIENT_SECRET: {'PRESENT' if resolved_secret and resolved_secret.strip() else 'MISSING'}")

    if not resolved_id or not resolved_secret:
        print(f"QF production OAuth: FAILED ({ERR_AUTH_FAILED})")
        print(f"Classification: {ERR_AUTH_FAILED}")
        print("Reason: QF_CLIENT_ID or QF_CLIENT_SECRET environment variable is missing.")
        return 1

    try:
        quran_data = fetch_fn(
            client_id=resolved_id,
            client_secret=resolved_secret,
            qf_env="production",
        )
    except Exception as exc:
        classification = classify_qf_error(exc)
        print(f"QF production OAuth: FAILED ({classification})")
        print(f"Classification: {classification}")
        print(f"Sanitized Error: {type(exc).__name__}")
        return 1

    print("QF production OAuth: SUCCESS")

    passed, error_msg, diagnostics = verify_production_quran_dataset(quran_data)

    print(f"environment: {diagnostics.get('environment')}")
    print(f"surahs: {diagnostics.get('surahs')}")
    print(f"ayahs: {diagnostics.get('ayahs')}")
    print(f"canonical per-Surah counts: {diagnostics.get('canonical_counts')}")

    if not passed:
        print(f"Classification: {ERR_DATASET_INVALID}")
        print(f"Reason: {error_msg}")
        return 1

    print("==================================================")
    print("VERIFICATION RESULT: PASS")
    print("QF production Content API access and dataset verified.")
    print("==================================================")
    return 0


if __name__ == "__main__":
    auto_load_env_if_needed()
    sys.exit(run_qf_production_verification())
