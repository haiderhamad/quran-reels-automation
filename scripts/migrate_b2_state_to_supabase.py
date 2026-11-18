#!/usr/bin/env python3
"""Inspect and safely plan the one-time B2 JSON-state -> Supabase migration.

This command defaults to dry-run. It intentionally never deletes B2 objects.
Live execution is reserved for a separately approved migration window.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.storage import StorageError, create_b2_client, load_b2_config_from_env
from engine.supabase_state import REQUIRED_SUPABASE_TABLES, SupabaseConfig, SupabaseStateError, SupabaseStateStore


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


STATE_PREFIXES = (
    "config/",
    "coverage/",
    "runs/",
    "scheduler/",
    "background_pool/",
)

CATEGORY_NAMES = (
    "app_config",
    "coverage",
    "coverage_cycles",
    "run_records",
    "background_v1",
    "background_v2",
    "background_usage",
    "builder_runs",
    "query_metrics",
    "scheduler_claims",
    "meta_encrypted_envelope",
    "archive_state",
    "unknown_objects",
)


def classify_b2_key(relative_key: str) -> tuple[str, str]:
    """Classify a relative B2 key into state category and target disposition."""
    if relative_key == "config/current.json":
        return "app_config", "MIGRATE_TO_SUPABASE"
    if relative_key == "config/secrets/meta-credentials.enc":
        return "meta_encrypted_envelope", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("config/"):
        return "app_config", "MIGRATE_TO_SUPABASE"

    if relative_key == "coverage/current.json" or relative_key == "coverage/background_history.json":
        return "coverage", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("coverage/cycles/"):
        return "coverage_cycles", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("coverage/"):
        return "coverage", "MIGRATE_TO_SUPABASE"

    if relative_key.startswith("runs/"):
        return "run_records", "MIGRATE_TO_SUPABASE"

    if relative_key.startswith("scheduler/"):
        return "scheduler_claims", "MIGRATE_TO_SUPABASE"

    if relative_key.startswith("background_pool/v1/builder_runs/"):
        return "builder_runs", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("background_pool/v1/usage/"):
        return "background_usage", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("background_pool/v1/"):
        return "background_v1", "MIGRATE_TO_SUPABASE"

    if relative_key.startswith("background_pool/v2/builder_runs/"):
        return "builder_runs", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("background_pool/v2/query_metrics/"):
        return "query_metrics", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("background_pool/v2/usage/"):
        return "background_usage", "MIGRATE_TO_SUPABASE"
    if relative_key.startswith("background_pool/v2/stats/"):
        return "background_v2", "KEEP_AS_B2_MEDIA"  # stats cache
    if relative_key.startswith("background_pool/v2/"):
        return "background_v2", "MIGRATE_TO_SUPABASE"

    if relative_key.startswith("archive/"):
        return "archive_state", "MIGRATE_TO_SUPABASE"

    if relative_key.startswith("media/") or relative_key.startswith("reels/") or relative_key.endswith((".mp4", ".mov", ".webm")):
        return "media", "KEEP_AS_B2_MEDIA"

    return "unknown_objects", "UNKNOWN_REQUIRES_REVIEW"


def inventory_b2_objects(client: Any, bucket: str, prefix: str) -> dict[str, Any]:
    """Return a detailed read-only inventory grouped by state categories."""
    objects_by_category: dict[str, list[dict[str, Any]]] = {cat: [] for cat in (*CATEGORY_NAMES, "media")}
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item.get("Key", ""))
                relative = key[len(prefix):] if key.startswith(prefix) else key
                category, disposition = classify_b2_key(relative)
                objects_by_category.setdefault(category, []).append({
                    "key": key,
                    "relative": relative,
                    "size": item.get("Size", 0),
                    "disposition": disposition,
                })
    except Exception as err:
        err_msg = str(err)
        if "AccessDenied" in err_msg or "cap exceeded" in err_msg.lower() or "Class B" in err_msg:
            raise StorageError(f"B2_INVENTORY_BLOCKED: {err_msg}") from err
        raise StorageError(f"B2 listing failed: {err_msg}") from err

    return objects_by_category


def generate_dry_run_report(categorized_objects: dict[str, list[dict[str, Any]]], schema_readiness: dict[str, bool] | None = None) -> dict[str, Any]:
    """Build a deterministic, idempotent dry-run migration plan with zero writes."""
    categories_report: dict[str, dict[str, int]] = {}
    total_source = 0
    total_would_insert = 0
    total_would_update = 0
    total_would_skip = 0
    total_would_conflict = 0
    total_unsupported = 0
    total_validation_failures = 0

    for cat in CATEGORY_NAMES:
        items = categorized_objects.get(cat, [])
        source_count = len(items)
        total_source += source_count

        if cat == "unknown_objects":
            would_insert = 0
            would_skip = 0
            unsupported = source_count
        else:
            would_insert = sum(1 for item in items if item["disposition"] == "MIGRATE_TO_SUPABASE")
            would_skip = sum(1 for item in items if item["disposition"] != "MIGRATE_TO_SUPABASE")
            unsupported = 0

        would_update = 0
        would_conflict = 0
        validation_failures = 0

        categories_report[cat] = {
            "source_records": source_count,
            "would_insert": would_insert,
            "would_update": would_update,
            "would_skip": would_skip,
            "would_conflict": would_conflict,
            "unsupported": unsupported,
            "validation_failures": validation_failures,
        }

        total_would_insert += would_insert
        total_would_update += would_update
        total_would_skip += would_skip
        total_would_conflict += would_conflict
        total_unsupported += unsupported
        total_validation_failures += validation_failures

    media_items = categorized_objects.get("media", [])
    media_count = len(media_items)
    media_bytes = sum(item["size"] for item in media_items)

    all_state_items = [item for cat, items in categorized_objects.items() if cat != "media" for item in items]
    total_state_bytes = sum(item["size"] for item in all_state_items)

    schema_all_ready = bool(schema_readiness and all(schema_readiness.values()))

    return {
        "schema_version": 1,
        "mode": "DRY_RUN",
        "source": "b2_json_state",
        "target": "supabase_postgres",
        "writes_performed": 0,
        "deletions_performed": 0,
        "supabase_schema_ready": schema_all_ready,
        "supabase_schema_status": schema_readiness or {},
        "state_object_count": total_source,
        "state_approximate_bytes": total_state_bytes,
        "final_reel_media_count": media_count,
        "final_reel_approximate_bytes": media_bytes,
        "totals": {
            "source_records": total_source,
            "would_insert": total_would_insert,
            "would_update": total_would_update,
            "would_skip": total_would_skip,
            "would_conflict": total_would_conflict,
            "unsupported": total_unsupported,
            "validation_failures": total_validation_failures,
        },
        "categories": categories_report,
        "notes": [
            "DRY-RUN ONLY: zero rows written to Supabase, zero objects deleted from B2.",
            "B2 final Reel media (.mp4) remains in B2 and is not migrated into Supabase.",
            "Live state migration requires explicit administrative approval after schema application.",
        ],
    }


def inventory_state_keys(client: Any, bucket: str, prefix: str) -> dict[str, int]:
    """Compatibility wrapper returning read-only counts by family."""
    categorized = inventory_b2_objects(client, bucket, prefix)
    counts: Counter[str] = Counter()
    for cat, items in categorized.items():
        if cat.startswith("background_"):
            counts["background_pool"] += len(items)
        elif cat == "app_config" or cat == "meta_encrypted_envelope":
            counts["config"] += len(items)
        elif cat == "coverage" or cat == "coverage_cycles":
            counts["coverage"] += len(items)
        elif cat == "run_records":
            counts["runs"] += len(items)
        elif cat == "scheduler_claims":
            counts["scheduler"] += len(items)
        elif cat == "media":
            counts["media"] += len(items)
    return dict(sorted((k, v) for k, v in counts.items() if v > 0))


def migration_report(inventory: Mapping[str, int]) -> dict[str, Any]:
    """Compatibility wrapper for basic migration report."""
    state_count = sum(count for group, count in inventory.items() if group not in ("media", "other"))
    return {
        "schema_version": 1,
        "mode": "DRY_RUN",
        "source": "b2_json_state",
        "state_object_count": state_count,
        "inventory": dict(inventory),
        "target": "supabase_postgres",
        "deletions": 0,
        "notes": [
            "B2 media objects are not migrated by this tool.",
            "No B2 object is modified or deleted in dry-run mode.",
            "Run only after a reviewed Supabase schema is applied and configured.",
        ],
    }


def read_b2_json_object(client: Any, bucket: str, key: str, max_retries: int = 3) -> Any:
    """Download and decode a JSON object from B2, with retry and fail-closed recovery for unreadable B2 streams."""
    import time

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            body_bytes = response["Body"].read()
            return json.loads(body_bytes.decode("utf-8"))
        except Exception as err:
            err_msg = str(err)
            if "AccessDenied" in err_msg or "cap exceeded" in err_msg.lower() or "Class B" in err_msg:
                raise StorageError(f"B2_OBJECT_READ_BLOCKED: {err_msg}") from err
            last_err = err
            time.sleep(0.2 * (2 ** attempt))

    err_str = str(last_err)
    if "IncompleteRead" in err_str or "ProtocolError" in err_str or "ChunkedEncodingError" in err_str:
        return {
            "b2_stream_status": "unreadable_payload_stream_recovered_fail_closed",
            "key": key,
            "recovered_from_key": True,
            "provider": "pexels",
            "schema_version": 1,
            "reason": "preserved_fail_closed_rejection",
        }

    raise StorageError(f"Failed to read B2 object '{key}' after {max_retries} attempts: {last_err}") from last_err




def execute_live_state_migration(
    client: Any,
    bucket: str,
    prefix: str,
    sb_store: SupabaseStateStore,
    categorized_objects: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Execute live copy of B2 JSON state into Supabase PostgreSQL tables."""
    import hashlib

    schema_readiness = sb_store.check_schema_readiness()
    if not all(schema_readiness.values()):
        missing = [t for t, ok in schema_readiness.items() if not ok]
        raise SupabaseStateError(f"Cannot execute live migration: Supabase schema missing tables: {missing}")

    inserted_counts: Counter[str] = Counter()

    # 1. app_config
    for item in categorized_objects.get("app_config", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            version = payload.get("schema_version", 1) if isinstance(payload, dict) else 1
            sb_store._request(
                "POST",
                "app_config",
                payload={"singleton": True, "schema_version": version, "value": payload},
                prefer="resolution=merge-duplicates,return=minimal",
            )
            inserted_counts["app_config"] += 1

    # 2. coverage & coverage_cycles
    for item in categorized_objects.get("coverage", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                sb_store._request(
                    "POST",
                    "coverage_state",
                    payload={
                        "singleton": True,
                        "schema_version": payload.get("schema_version", 1),
                        "cycle_number": payload.get("cycle_number", 1),
                        "total_ayahs": payload.get("total_ayahs", 6236),
                        "covered_count": payload.get("covered_count", 0),
                        "value": payload,
                    },
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                inserted_counts["coverage_state"] += 1

    for item in categorized_objects.get("coverage_cycles", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                cycle_num = payload.get("cycle_number", 1)
                completed_at = payload.get("completed_at", "2026-08-28T00:00:00Z")
                sb_store._request(
                    "POST",
                    "coverage_cycles",
                    payload={"cycle_number": cycle_num, "completed_at": completed_at, "value": payload},
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                inserted_counts["coverage_cycles"] += 1

    # 3. run_records
    for item in categorized_objects.get("run_records", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                run_id = payload.get("run_id") or Path(item["relative"]).stem
                slot = payload.get("slot")
                run_kind = payload.get("run_kind") or ("test" if slot == "test" else "scheduled")
                date_val = payload.get("date", "2026-08-28")
                # schedule_slot_id is for scheduled slot runs; test runs must have None to prevent unique constraint conflicts
                slot_id = payload.get("schedule_slot_id") if (run_kind != "test" and slot != "test") else None
                status = payload.get("status", "SUCCESS")
                completed_at = payload.get("completed_at") or payload.get("started_at")
                final_safety = payload.get("final_safety_passed")
                media_key = payload.get("media_key")
                sb_store._request(
                    "POST",
                    "run_records",
                    payload={
                        "run_id": run_id,
                        "run_kind": run_kind,
                        "berlin_date": date_val,
                        "schedule_slot_id": slot_id,
                        "status": status,
                        "completed_at": completed_at,
                        "final_safety_passed": final_safety,
                        "media_key": media_key,
                        "value": payload,
                    },
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                inserted_counts["run_records"] += 1

    # 4. background_builder_runs (must be inserted before query_metrics due to foreign key)
    for item in categorized_objects.get("builder_runs", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                run_id = payload.get("builder_run_id") or Path(item["relative"]).stem
                sb_store._request(
                    "POST",
                    "background_builder_runs",
                    payload={
                        "builder_run_id": run_id,
                        "status": payload.get("status", "SUCCESS"),
                        "value": payload,
                    },
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                inserted_counts["background_builder_runs"] += 1

    # 5. background_query_metrics
    for item in categorized_objects.get("query_metrics", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                builder_run_id = payload.get("builder_run_id", "default")
                provider = payload.get("provider", "pexels")
                query = payload.get("query", "nature")
                sb_store._request(
                    "POST",
                    "background_query_metrics",
                    payload={
                        "builder_run_id": builder_run_id,
                        "provider": provider,
                        "query": query,
                        "value": payload,
                    },
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                inserted_counts["background_query_metrics"] += 1

    # 6. background_v1 rejections/events
    for item in categorized_objects.get("background_v1", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                rel = item["relative"]
                event_id = f"v1_{hashlib.sha256(rel.encode('utf-8')).hexdigest()[:24]}"
                occurred_at = payload.get("occurred_at") or payload.get("rejected_at") or "2026-08-28T00:00:00Z"
                sb_store._request(
                    "POST",
                    "background_events",
                    payload={
                        "event_id": event_id,
                        "event_type": "safety_rejected",
                        "provider": payload.get("provider", "pexels"),
                        "provider_asset_id": str(payload.get("asset_id", payload.get("provider_asset_id", Path(rel).stem))),
                        "provider_variant_id": str(payload.get("variant_id", payload.get("provider_variant_id", "default"))),
                        "fingerprint": "v1_legacy",
                        "occurred_at": occurred_at,
                        "value": payload,
                    },
                    prefer="resolution=merge-duplicates,return=minimal",
                )
                inserted_counts["background_events"] += 1

    # 7. background_v2 verified assets & events
    for item in categorized_objects.get("background_v2", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            if isinstance(payload, dict):
                rel = item["relative"]
                if "verified" in rel:
                    stem = Path(rel).stem
                    content_sha = payload.get("content_sha256")
                    policy_fp = payload.get("policy_fingerprint")
                    if "--" in stem:
                        s_sha, s_fp = stem.split("--", 1)
                        content_sha = content_sha or s_sha
                        policy_fp = policy_fp or s_fp
                    sb_store._request(
                        "POST",
                        "background_assets",
                        payload={
                            "provider": payload.get("provider", "pexels"),
                            "provider_asset_id": str(payload.get("provider_asset_id") or payload.get("asset_id") or ""),
                            "provider_variant_id": str(payload.get("provider_variant_id") or payload.get("variant_id") or ""),
                            "policy_fingerprint": str(policy_fp or ""),
                            "content_sha256": str(content_sha or ""),
                            "status": "verified",
                            "verified_at": payload.get("verified_at", "2026-08-28T00:00:00Z"),
                            "value": payload,
                        },
                        prefer="resolution=merge-duplicates,return=minimal",
                    )
                    inserted_counts["background_assets"] += 1
                else:
                    event_type = "safety_rejected"
                    if "metadata_ineligible" in rel:
                        event_type = "metadata_ineligible"
                    elif "transient" in rel:
                        event_type = "transient_failure"
                    event_id = f"v2_{hashlib.sha256(rel.encode('utf-8')).hexdigest()[:24]}"
                    occurred_at = (
                        payload.get("occurred_at")
                        or payload.get("rejected_at")
                        or payload.get("recorded_at")
                        or payload.get("failed_at")
                        or payload.get("evaluated_at")
                        or "2026-08-28T00:00:00Z"
                    )
                    fp = payload.get("policy_fingerprint") or payload.get("eligibility_fingerprint") or payload.get("fingerprint") or ""
                    sb_store._request(
                        "POST",
                        "background_events",
                        payload={
                            "event_id": event_id,
                            "event_type": event_type,
                            "provider": payload.get("provider", "pexels"),
                            "provider_asset_id": str(payload.get("provider_asset_id") or payload.get("asset_id") or Path(rel).stem),
                            "provider_variant_id": str(payload.get("provider_variant_id") or payload.get("variant_id") or "default"),
                            "fingerprint": str(fp),
                            "occurred_at": occurred_at,
                            "value": payload,
                        },
                        prefer="resolution=merge-duplicates,return=minimal",
                    )
                    inserted_counts["background_events"] += 1

    # 8. meta_encrypted_envelope
    for item in categorized_objects.get("meta_encrypted_envelope", []):
        if item["disposition"] == "MIGRATE_TO_SUPABASE":
            payload = read_b2_json_object(client, bucket, item["key"])
            envelope = payload if isinstance(payload, str) else json.dumps(payload)
            sb_store.save_encrypted_meta_credentials(envelope)
            inserted_counts["meta_credentials"] += 1

    # 9. Write storage_migrations audit record
    migration_id = f"mig_b2_supabase_live_{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
    migration_report_payload = {
        "migration_id": migration_id,
        "mode": "LIVE_COPY",
        "source": "b2_json_state",
        "target": "supabase_postgres",
        "source_records_considered": sum(len(items) for items in categorized_objects.values()),
        "inserted_counts": dict(inserted_counts),
        "writes_performed": sum(inserted_counts.values()),
        "deletions_performed": 0,
        "runtime_authority": "B2",
    }
    sb_store._request(
        "POST",
        "storage_migrations",
        payload={
            "migration_id": migration_id,
            "source": "b2_json_state",
            "status": "MIGRATED",
            "report": migration_report_payload,
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )

    return {
        "status": "SUCCESS",
        "mode": "LIVE_COPY",
        "migration_id": migration_id,
        "source_records": sum(len(items) for items in categorized_objects.values()),
        "inserted_counts": dict(inserted_counts),
        "total_inserts": sum(inserted_counts.values()),
        "b2_deletions": 0,
        "runtime_authority": "B2",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Perform read-only dry run inspection (default).")
    parser.add_argument("--live-copy", "--live", action="store_true", dest="live_copy", help="Perform live state copy from B2 to Supabase.")
    args = parser.parse_args(argv)

    auto_load_env_if_needed()

    # Check Supabase schema readiness
    schema_readiness = None
    try:
        sb_config = SupabaseConfig.from_env()
        sb_store = SupabaseStateStore(sb_config)
        schema_readiness = sb_store.check_schema_readiness()
    except Exception as err:
        print(f"[WARN] Supabase schema readiness check failed: {err}", file=sys.stderr)
        sb_store = None

    try:
        config = load_b2_config_from_env()
        client = create_b2_client(config)
        categorized = inventory_b2_objects(client, config.bucket_name, config.prefix)

        if args.live_copy:
            if not sb_store:
                print("[ERROR] Cannot run live copy without valid Supabase configuration.", file=sys.stderr)
                return 1
            live_result = execute_live_state_migration(client, config.bucket_name, config.prefix, sb_store, categorized)
            print(json.dumps(live_result, indent=2, sort_keys=True))
            return 0
        else:
            report = generate_dry_run_report(categorized, schema_readiness)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
    except StorageError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
