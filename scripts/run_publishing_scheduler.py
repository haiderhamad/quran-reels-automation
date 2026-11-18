#!/usr/bin/env python3
"""Evaluate Europe/Berlin publishing schedule from Supabase app_config and claim due slots.

This script implements the dynamic Quran publishing scheduler heartbeat.
It inspects live Supabase state, respects automation_enabled and individual slot
toggles, evaluates the bounded 45-minute grace window in Europe/Berlin wall-clock
time, and atomically acquires scheduler claims for due slots.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path
from typing import Optional

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.scheduler import (
    DEFAULT_GRACE_WINDOW_MINUTES,
    berlin_now,
    is_slot_due,
    production_schedule_entries,
)
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from scripts.build_verified_background_pool import auto_load_env_if_needed


def write_github_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def get_supabase_store() -> SupabaseStateStore:
    auto_load_env_if_needed()
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")
    return SupabaseStateStore(SupabaseConfig(url=url, service_role_key=key))


def record_heartbeat(
    store: SupabaseStateStore,
    *,
    source: str = "github_cron",
    due_slot_count: int = 0,
    claim_count: int = 0,
    dispatch_count: int = 0,
    result: Optional[dict] = None,
) -> None:
    try:
        hb_id = f"hb_{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}"
        store._request(
            "POST",
            "scheduler_heartbeats",
            payload={
                "heartbeat_id": hb_id,
                "source": source,
                "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "due_slot_count": due_slot_count,
                "claim_count": claim_count,
                "dispatch_count": dispatch_count,
                "result": result or {},
            },
            prefer="return=minimal",
        )
    except Exception:
        pass


def evaluate_and_claim_due_slot(
    store: SupabaseStateStore,
    *,
    now: Optional[datetime.datetime] = None,
    grace_window_minutes: int = DEFAULT_GRACE_WINDOW_MINUTES,
    force: bool = False,
) -> tuple[bool, Optional[dict[str, str]]]:
    """Evaluate due slots and atomically acquire claim for the first due unclaimed slot.

    Returns:
        (automation_active: bool, due_slot_info: Optional[dict[str, str]])
        If due_slot_info is not None, it contains:
            "slot_id", "run_id", "berlin_date", "berlin_time", "publish_facebook", "publish_instagram"
    """
    config = store.load_config()
    if config is None:
        print("[SCHEDULER] No app_config found in Supabase.")
        return False, None

    local_dt = berlin_now(now)
    local_date = local_dt.date().isoformat()
    local_time = local_dt.strftime("%H:%M")

    print(
        f"[SCHEDULER] Heartbeat at Berlin time: {local_date} {local_time} "
        f"(automation_enabled={config.automation_enabled}, force={force})"
    )

    if not config.automation_enabled and not force:
        print("[SCHEDULER] automation_enabled is False. Master kill switch active. Skipping.")
        return False, None

    enabled_entries = production_schedule_entries(config.publishing_schedule)
    print(
        f"[SCHEDULER] Found {len(enabled_entries)} enabled slots: "
        + ", ".join(f"{e.id}@{e.time}" for e in enabled_entries)
    )

    for entry in enabled_entries:
        if not is_slot_due(entry.time, now=local_dt, grace_window_minutes=grace_window_minutes):
            continue

        print(
            f"[SCHEDULER] Slot '{entry.id}' ({entry.time}) is within {grace_window_minutes}m grace window."
        )

        # Check existing run record for this local date and stable slot ID
        existing_run = store.get_run_record(date=local_date, slot=entry.id, run_kind="scheduled")
        if existing_run is not None and existing_run.status in ("SUCCESS", "STARTED"):
            print(
                f"[SCHEDULER] Slot '{entry.id}' already has a {existing_run.status} run today "
                f"(run_id={existing_run.run_id}). Skipping."
            )
            continue

        # Check existing scheduler claim
        if store.is_scheduler_slot_claimed(berlin_date=local_date, schedule_slot_id=entry.id):
            print(f"[SCHEDULER] Slot '{entry.id}' is already claimed today. Skipping.")
            continue

        # Atomically claim
        run_id = f"scheduled_{local_date.replace('-', '')}_{entry.id}"
        claimed = store.claim_scheduler_slot(
            berlin_date=local_date,
            schedule_slot_id=entry.id,
            worker_run_id=run_id,
        )
        if not claimed:
            print(f"[SCHEDULER] Atomic claim conflict for slot '{entry.id}'. Skipping.")
            continue

        print(
            f"[SCHEDULER] Successfully acquired atomic claim for slot '{entry.id}' "
            f"(run_id={run_id})."
        )
        record_heartbeat(
            store,
            source="github_cron",
            due_slot_count=1,
            claim_count=1,
            dispatch_count=1,
            result={"slot_id": entry.id, "run_id": run_id},
        )
        return True, {
            "slot_id": entry.id,
            "run_id": run_id,
            "berlin_date": local_date,
            "berlin_time": local_time,
            "publish_facebook": "true" if config.facebook_enabled else "false",
            "publish_instagram": "true" if config.instagram_enabled else "false",
        }

    print("[SCHEDULER] No slots are currently due and unclaimed.")
    record_heartbeat(
        store,
        source="github_cron",
        due_slot_count=0,
        claim_count=0,
        dispatch_count=0,
        result={"status": "NO_DUE_SLOTS"},
    )
    return True, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dynamic Europe/Berlin Quran publishing scheduler heartbeat."
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        default=False,
        help="Check schedule, acquire claim if due, write GITHUB_OUTPUT, and exit without running video pipeline.",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="Deterministic ISO-8601 timestamp override (e.g. 2026-09-05T10:07:00Z).",
    )
    parser.add_argument(
        "--grace-window-minutes",
        type=int,
        default=DEFAULT_GRACE_WINDOW_MINUTES,
        help="Grace window in minutes (default 45).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force evaluation even if automation_enabled is False.",
    )
    args = parser.parse_args(argv)

    now_dt: Optional[datetime.datetime] = None
    if args.now:
        now_dt = datetime.datetime.fromisoformat(args.now)

    try:
        store = get_supabase_store()
        _, due_slot = evaluate_and_claim_due_slot(
            store,
            now=now_dt,
            grace_window_minutes=args.grace_window_minutes,
            force=args.force,
        )

        if due_slot is not None:
            write_github_output("due", "true")
            write_github_output("slot_id", due_slot["slot_id"])
            write_github_output("run_id", due_slot["run_id"])
            write_github_output("publish_facebook", due_slot["publish_facebook"])
            write_github_output("publish_instagram", due_slot["publish_instagram"])
            print(f"[SCHEDULER] OUTPUT: due=true slot_id={due_slot['slot_id']} run_id={due_slot['run_id']}")

            if not args.check_only:
                # Direct execution mode
                print(f"[SCHEDULER] Executing generation job for slot '{due_slot['slot_id']}'...")
                from engine.job import JobRequest, run_job
                job_req = JobRequest(
                    run_kind="scheduled",
                    slot=due_slot["slot_id"],
                    schedule_slot_id=due_slot["slot_id"],
                    run_id=due_slot["run_id"],
                    publish_facebook=due_slot["publish_facebook"] == "true",
                    publish_instagram=due_slot["publish_instagram"] == "true",
                )
                res = run_job(job_req)
                print(f"[SCHEDULER] Job result: status={res.status} run_id={res.run_id}")
                if res.status == "FAILED":
                    store.update_scheduler_claim_status(
                        due_slot["berlin_date"],
                        due_slot["slot_id"],
                        status="FAILED",
                        failure_reason=res.failure_reason,
                    )
                    return 1
                else:
                    store.update_scheduler_claim_status(
                        due_slot["berlin_date"],
                        due_slot["slot_id"],
                        status="DISPATCHED",
                    )
        else:
            write_github_output("due", "false")
            write_github_output("slot_id", "")
            write_github_output("run_id", "")
            write_github_output("publish_facebook", "false")
            write_github_output("publish_instagram", "false")
            print("[SCHEDULER] OUTPUT: due=false")

        return 0
    except Exception as err:
        print(f"[SCHEDULER] Technical error: {err}", file=sys.stderr)
        write_github_output("due", "false")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
