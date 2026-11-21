"""Central state-authority selector and store abstractions.

Separates authoritative structured application state from B2 media storage.
Default authority is strictly 'b2' until production cutover approval.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Mapping, Optional, Protocol, Sequence

from engine.storage import (
    B2Config,
    CoverageState,
    RunRecord,
    HistoricalBackgroundUse,
    background_usage_for_run,
    StorageError,
    StoredConfig,
    create_b2_client,
    load_b2_config_from_env,
    load_config as b2_load_config,
    save_config as b2_save_config,
    load_coverage as b2_load_coverage,
    save_coverage as b2_save_coverage,
    update_coverage_for_run as b2_update_coverage_for_run,
    get_run_record as b2_get_run_record,
    put_run_record as b2_put_run_record,
    list_recent_runs as b2_list_recent_runs,
    upload_reel as b2_upload_reel,
    create_presigned_get_url as b2_presigned_url,
)
from engine.supabase_state import SupabaseConfig, SupabaseStateStore


STATE_AUTHORITY_B2 = "b2"
STATE_AUTHORITY_SUPABASE = "supabase"
VALID_STATE_AUTHORITIES = (STATE_AUTHORITY_B2, STATE_AUTHORITY_SUPABASE)


def get_state_authority(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the configured state authority ('b2' or 'supabase').

    Defaults to 'b2'. Fails closed on any unknown/unrecognized value.
    """
    values = os.environ if env is None else env
    raw = values.get("STATE_AUTHORITY")
    if raw is None or not str(raw).strip():
        return STATE_AUTHORITY_B2
    cleaned = str(raw).strip().lower()
    if cleaned not in VALID_STATE_AUTHORITIES:
        raise StorageError(
            f"Invalid STATE_AUTHORITY '{raw}'. Must be 'b2' or 'supabase'."
        )
    return cleaned


class StateStore(Protocol):
    """Protocol for authoritative non-media application state operations."""

    def load_config(self) -> Optional[StoredConfig]: ...
    def save_config(self, config: StoredConfig) -> None: ...
    def load_coverage(self) -> CoverageState: ...
    def save_coverage(self, state: CoverageState) -> None: ...
    def record_covered_ayahs(
        self,
        run_id: str,
        ayah_keys: Sequence[str],
        production_authorization: Optional[Any] = None,
    ) -> CoverageState: ...
    def get_run(self, run_id: str) -> Optional[RunRecord]: ...
    def get_run_record(
        self,
        date: str,
        slot: str,
        run_id: Optional[str] = None,
        run_kind: Optional[str] = None,
    ) -> Optional[RunRecord]: ...
    def save_run(self, record: RunRecord) -> None: ...
    def start_run_record(self, record: RunRecord) -> None: ...
    def complete_run_record(self, record: RunRecord) -> None: ...
    def fail_run_record(self, record: RunRecord) -> None: ...
    def list_run_records(
        self,
        limit: int = 50,
        run_kind: Optional[str] = None,
        status: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Sequence[RunRecord]: ...
    def list_successful_production_runs(
        self, year: int, month: int
    ) -> Sequence[RunRecord]: ...
    def get_production_background_usage(self) -> Sequence[HistoricalBackgroundUse]: ...
    def get_recent_content_history(
        self, last_n_runs: int = 30
    ) -> Sequence[dict[str, Any]]: ...
    def claim_scheduler_slot(
        self,
        berlin_date: str,
        schedule_slot_id: str,
        claimed_at: Optional[str] = None,
        worker_run_id: Optional[str] = None,
    ) -> bool: ...
    def is_scheduler_slot_claimed(
        self, berlin_date: str, schedule_slot_id: str
    ) -> bool: ...
    def load_encrypted_meta_credentials(self) -> Optional[str]: ...
    def save_encrypted_meta_credentials(self, envelope: str) -> None: ...
    def get_buffer_publication_claim(self, run_id: str, platform: str, publisher_backend: str, channel_id: str) -> Optional[Mapping[str, Any]]: ...
    def claim_buffer_publication_dispatch(self, claim: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def transition_buffer_publication_claim(self, run_id: str, platform: str, publisher_backend: str, channel_id: str, expected_states: tuple[str, ...], state: str, updates: Mapping[str, Any]) -> Optional[Mapping[str, Any]]: ...
    def get_buffer_draft_validation(self, validation_id: str) -> Optional[Mapping[str, Any]]: ...
    def claim_buffer_draft_validation(self, draft: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def transition_buffer_draft_validation(self, validation_id: str, expected_states: tuple[str, ...], state: str, updates: Mapping[str, Any]) -> Optional[Mapping[str, Any]]: ...


class B2StateStore:
    """Adapter for Backblaze B2 JSON state (rollback & legacy compatibility)."""

    def __init__(self, b2_config: B2Config, client: Optional[Any] = None) -> None:
        self.b2_config = b2_config
        self.client = client or create_b2_client(b2_config)

    def load_config(self) -> Optional[StoredConfig]:
        return b2_load_config(self.client, self.b2_config)

    def save_config(self, config: StoredConfig) -> None:
        b2_save_config(self.client, self.b2_config, config)

    def load_coverage(self) -> CoverageState:
        return b2_load_coverage(self.client, self.b2_config)

    def save_coverage(self, state: CoverageState) -> None:
        b2_save_coverage(self.client, self.b2_config, state)

    def record_covered_ayahs(
        self,
        run_id: str,
        ayah_keys: Sequence[str],
        production_authorization: Optional[Any] = None,
    ) -> CoverageState:
        # Note: B2 coverage update legacy adapter
        return b2_update_coverage_for_run(
            self.client, self.b2_config, run_id=run_id, verse_keys=ayah_keys
        )

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        return b2_get_run_record(self.client, self.b2_config, run_id)

    def get_run_record(
        self,
        date: str,
        slot: str,
        run_id: Optional[str] = None,
        run_kind: Optional[str] = None,
    ) -> Optional[RunRecord]:
        return b2_get_run_record(
            self.client, self.b2_config, date=date, slot=slot, run_id=run_id, run_kind=run_kind
        )

    def save_run(self, record: RunRecord) -> None:
        b2_put_run_record(self.client, self.b2_config, record)

    def start_run_record(self, record: RunRecord) -> None:
        b2_put_run_record(self.client, self.b2_config, record)

    def complete_run_record(self, record: RunRecord) -> None:
        b2_put_run_record(self.client, self.b2_config, record)

    def fail_run_record(self, record: RunRecord) -> None:
        b2_put_run_record(self.client, self.b2_config, record)

    def list_run_records(
        self,
        limit: int = 50,
        run_kind: Optional[str] = None,
        status: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Sequence[RunRecord]:
        runs = b2_list_recent_runs(self.client, self.b2_config, limit=limit)
        if run_kind:
            runs = tuple(r for r in runs if r.run_kind == run_kind)
        if status:
            runs = tuple(r for r in runs if r.status == status)
        if date:
            runs = tuple(r for r in runs if r.date == date)
        return runs

    def list_successful_production_runs(
        self, year: int, month: int
    ) -> Sequence[RunRecord]:
        runs = b2_list_recent_runs(self.client, self.b2_config, limit=500)
        res = []
        for r in runs:
            if (
                r.status == "SUCCESS"
                and r.run_kind in ("scheduled", "manual_production")
                and r.final_safety_passed
                and r.media_key
            ):
                if r.completed_at and r.completed_at.startswith(
                    f"{year:04d}-{month:02d}"
                ):
                    res.append(r)
        return tuple(sorted(res, key=lambda x: x.completed_at or ""))

    def get_production_background_usage(self) -> Sequence[HistoricalBackgroundUse]:
        runs = b2_list_recent_runs(self.client, self.b2_config, limit=None)
        return tuple(use for run in runs for use in background_usage_for_run(run))

    def get_recent_content_history(
        self, last_n_runs: int = 30
    ) -> Sequence[dict[str, Any]]:
        from engine.storage import get_recent_content_history as b2_history
        return tuple(b2_history(self.client, self.b2_config, last_n_runs=last_n_runs))

    def claim_scheduler_slot(
        self,
        berlin_date: str,
        schedule_slot_id: str,
        claimed_at: Optional[str] = None,
        worker_run_id: Optional[str] = None,
    ) -> bool:
        key = f"{self.b2_config.prefix}scheduler/claims/{berlin_date}/{schedule_slot_id}.json"
        payload = json.dumps(
            {
                "berlin_date": berlin_date,
                "schedule_slot_id": schedule_slot_id,
                "claimed_at": claimed_at
                or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "worker_run_id": worker_run_id
                or f"claim_{berlin_date}_{schedule_slot_id}",
                "status": "claimed",
            },
            indent=2,
        ).encode("utf-8")
        try:
            self.client.put_object(
                Bucket=self.b2_config.bucket_name,
                Key=key,
                Body=payload,
                ContentType="application/json",
            )
            return True
        except Exception:
            return False

    def is_scheduler_slot_claimed(
        self, berlin_date: str, schedule_slot_id: str
    ) -> bool:
        key = f"{self.b2_config.prefix}scheduler/claims/{berlin_date}/{schedule_slot_id}.json"
        try:
            self.client.head_object(Bucket=self.b2_config.bucket_name, Key=key)
            return True
        except Exception:
            return False

    def load_encrypted_meta_credentials(self) -> Optional[str]:
        key = f"{self.b2_config.prefix}config/secrets/meta-credentials.enc"
        try:
            resp = self.client.get_object(Bucket=self.b2_config.bucket_name, Key=key)
            body = resp["Body"].read()
            return body.decode("utf-8") if isinstance(body, bytes) else str(body)
        except Exception:
            return None

    def save_encrypted_meta_credentials(self, envelope: str) -> None:
        key = f"{self.b2_config.prefix}config/secrets/meta-credentials.enc"
        self.client.put_object(
            Bucket=self.b2_config.bucket_name,
            Key=key,
            Body=envelope.encode("utf-8"),
            ContentType="application/octet-stream",
        )


class MediaStore:
    """Dedicated B2 media store for final Reel MP4 files only.

    Never used for application metadata or JSON state.
    """

    def __init__(self, b2_config: B2Config, client: Optional[Any] = None) -> None:
        self.b2_config = b2_config
        self.client = client or create_b2_client(b2_config)

    def upload_reel(
        self,
        local_file_path: str,
        date: str,
        slot: str,
        content_sha256: str,
        run_id: Optional[str] = None,
        run_kind: Optional[str] = None,
    ) -> str:
        return b2_upload_reel(
            self.client,
            self.b2_config,
            local_file_path=local_file_path,
            date=date,
            slot=slot,
            content_sha256=content_sha256,
            run_id=run_id,
            run_kind=run_kind,
        )

    def generate_presigned_media_url(
        self, media_key: str, expires_in_seconds: int = 21600
    ) -> str:
        return b2_presigned_url(
            self.client,
            self.b2_config,
            media_key=media_key,
            expires_in_seconds=expires_in_seconds,
        )

    def head_media_object(self, media_key: str) -> dict[str, Any]:
        return self.client.head_object(
            Bucket=self.b2_config.bucket_name, Key=media_key
        )

    def delete_media_object(self, media_key: str) -> None:
        self.client.delete_object(
            Bucket=self.b2_config.bucket_name, Key=media_key
        )


def get_active_state_store(
    env: Optional[Mapping[str, str]] = None,
    *,
    supabase_session: Optional[Any] = None,
    b2_client: Optional[Any] = None,
    b2_config: Optional[B2Config] = None,
) -> StateStore:
    """Return the active StateStore based on STATE_AUTHORITY.

    When STATE_AUTHORITY=supabase: returns SupabaseStateStore.
    When STATE_AUTHORITY=b2 (default): returns B2StateStore.
    """
    authority = get_state_authority(env)
    if authority == STATE_AUTHORITY_SUPABASE:
        sb_config = SupabaseConfig.from_env(env)
        if supabase_session is not None:
            return SupabaseStateStore(sb_config, session=supabase_session)
        return SupabaseStateStore(sb_config)

    cfg = b2_config if b2_config is not None else load_b2_config_from_env(env)
    return B2StateStore(cfg, client=b2_client)


def get_media_store(
    env: Optional[Mapping[str, str]] = None,
    *,
    b2_client: Optional[Any] = None,
    b2_config: Optional[B2Config] = None,
) -> MediaStore:
    """Return the B2 MediaStore for final Reel .mp4 storage."""
    cfg = b2_config if b2_config is not None else load_b2_config_from_env(env)
    return MediaStore(cfg, client=b2_client)
