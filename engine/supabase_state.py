"""Server-only Supabase PostgREST state adapter.

Authoritative state storage for configuration, Coverage D-011, RunRecords,
scheduler claims, background pool evidence/metrics, and encrypted Meta credentials.
Never stores media bytes.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import sys
from dataclasses import asdict
from typing import Any, Mapping, Optional, Sequence

import requests

from engine.storage import (
    TOTAL_CANONICAL_AYAHS,
    CoverageState,
    RunRecord,
    StorageError,
    StoredConfig,
    stored_config_from_dict,
    run_record_from_dict,
    HistoricalBackgroundUse,
    historical_background_usage,
    recent_content_history_entries,
)


class SupabaseStateError(StorageError):
    """Raised for a failed server-side Supabase state operation."""


LIVE_TEST_WRITE_OPT_IN_ENV = "QURAN_REELS_ALLOW_LIVE_TEST_WRITES"


def _is_unittest_process() -> bool:
    """Return whether this interpreter is executing the normal unittest suite.

    This deliberately detects the loaded test runner rather than an ambient
    configuration value.  A production worker can therefore use its normal
    server-side transport, while a test process that accidentally inherits
    production Supabase credentials cannot reach it.
    """
    return "unittest" in sys.modules or any(
        argument == "unittest" or argument.endswith("unittest")
        for argument in sys.argv
    )


class SupabaseConfig:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        if not self.url.startswith("https://"):
            raise SupabaseStateError("SUPABASE_URL must be an HTTPS URL.")
        if not self.service_role_key:
            raise SupabaseStateError("SUPABASE_SERVICE_ROLE_KEY is required server-side.")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SupabaseConfig":
        values = os.environ if env is None else env
        return cls(
            str(values.get("SUPABASE_URL", "")).strip(),
            str(values.get("SUPABASE_SERVICE_ROLE_KEY", "")).strip(),
        )


REQUIRED_SUPABASE_TABLES = (
    "app_config",
    "coverage_state",
    "coverage_cycles",
    "run_records",
    "buffer_publication_claims",
    "buffer_draft_validations",
    "scheduler_claims",
    "background_assets",
    "background_events",
    "background_builder_runs",
    "background_query_metrics",
    "background_candidates",
    "meta_credentials",
    "archive_jobs",
    "archive_items",
    "storage_migrations",
)

SUPABASE_READ_PAGE_SIZE = 500

def _extract_rpc_row(result: Any) -> Optional[dict[str, Any]]:
    if isinstance(result, Mapping):
        return dict(result) if result else None
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)) and result:
        first = result[0]
        if isinstance(first, Mapping):
            return dict(first) if first else None
    return None


class SupabaseStateStore:
    """Small PostgREST adapter for durable non-media application state.

    ``session`` is injectable so unit tests never need a live Supabase project.
    The service-role key is held only in server/worker memory and is never
    returned from any method or serialized into logs.
    """

    def __init__(self, config: SupabaseConfig, session: Any = requests) -> None:
        self.config = config
        self.session = session
        self._default_live_transport = session is requests

    def _assert_live_transport_allowed(self) -> None:
        """Fail closed before normal unit tests can call live Supabase.

        Fake/injected sessions remain available for hermetic unit tests.  A
        deliberate live integration test must set the explicit opt-in flag;
        this is intentionally scoped to the unittest process rather than
        changing normal production worker behaviour.
        """
        if (
            self._default_live_transport
            and _is_unittest_process()
            and os.environ.get(LIVE_TEST_WRITE_OPT_IN_ENV) != "1"
        ):
            raise SupabaseStateError(
                "Live Supabase access is blocked during unittest execution. "
                f"Set {LIVE_TEST_WRITE_OPT_IN_ENV}=1 only for an explicitly "
                "reviewed live integration test."
            )

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        payload: Optional[Any] = None,
        prefer: str = "return=representation",
    ) -> Any:
        self._assert_live_transport_allowed()
        req_params = dict(params or {})
        if "merge-duplicates" in prefer and "on_conflict" not in req_params:
            pk_map = {
                "app_config": "singleton",
                "coverage_state": "singleton",
                "coverage_cycles": "cycle_number",
                "run_records": "run_id",
                "buffer_publication_claims": "run_id,platform,publisher_backend,channel_id",
                "buffer_draft_validations": "validation_id",
                "scheduler_claims": "berlin_date,slot_id",
                "background_assets": "provider,provider_asset_id,provider_variant_id,policy_fingerprint,content_sha256",
                "background_events": "event_id",
                "background_builder_runs": "builder_run_id",
                "background_query_metrics": "builder_run_id,provider,query",
                "meta_credentials": "singleton",
                "storage_migrations": "migration_id",
            }
            if table in pk_map:
                req_params["on_conflict"] = pk_map[table]

        response = self.session.request(
            method,
            f"{self.config.url}/rest/v1/{table}",
            params=req_params,
            data=(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={
                "apikey": self.config.service_role_key,
                "Authorization": f"Bearer {self.config.service_role_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Prefer": prefer,
            },
            timeout=20,
        )
        status_code = getattr(response, "status_code", 0)
        if not getattr(response, "ok", False):
            # Special check for 409 conflict
            if status_code == 409:
                return {"_conflict": True, "status_code": 409}
            error_detail = getattr(response, "text", "")
            raise SupabaseStateError(
                f"Supabase {method} {table} failed with HTTP {status_code}: {error_detail}"
            )
        if not getattr(response, "content", b""):
            return []
        try:
            return response.json()
        except Exception as err:
            raise SupabaseStateError(
                f"Supabase {method} {table} returned invalid JSON."
            ) from err

    def _get_paginated(
        self,
        table: str,
        params: Mapping[str, str],
        *,
        order: str,
        page_size: int = SUPABASE_READ_PAGE_SIZE,
        read_until_empty: bool = False,
    ) -> list[dict[str, Any]]:
        """Read a complete bounded PostgREST collection in stable pages."""
        if page_size < 1 or page_size > SUPABASE_READ_PAGE_SIZE:
            raise SupabaseStateError("Supabase read page size must be between 1 and 500.")
        base_params = dict(params)
        base_params["order"] = order
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page_params = {**base_params, "limit": str(page_size), "offset": str(offset)}
            page = self._request("GET", table, params=page_params)
            if not isinstance(page, list):
                raise SupabaseStateError(f"Supabase GET {table} returned a malformed page.")
            if any(not isinstance(row, Mapping) for row in page):
                raise SupabaseStateError(f"Supabase GET {table} returned a malformed row.")
            rows.extend(dict(row) for row in page)
            # Self-hosted PostgREST can cap a response below the requested limit.
            if not page or (not read_until_empty and len(page) < page_size):
                return rows
            offset += len(page)

    def _rpc(self, function: str, payload: Mapping[str, Any]) -> Any:
        """Call a narrowly allowlisted database function with service credentials."""
        self._assert_live_transport_allowed()
        response = self.session.request(
            "POST",
            f"{self.config.url}/rest/v1/rpc/{function}",
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers={
                "apikey": self.config.service_role_key,
                "Authorization": f"Bearer {self.config.service_role_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Prefer": "return=representation",
            },
            timeout=20,
        )
        if not getattr(response, "ok", False):
            raise SupabaseStateError(
                f"Supabase RPC {function} failed with HTTP {getattr(response, 'status_code', 0)}: {getattr(response, 'text', '')}"
            )
        if not getattr(response, "content", b""):
            return []
        try:
            return response.json()
        except Exception as err:
            raise SupabaseStateError(f"Supabase RPC {function} returned invalid JSON.") from err

    def load_calm_reels_v1_fresh_cycle(self) -> Any:
        """Read only the CURRENT 2,211-ayah Facebook receipt epoch.

        Never read coverage_state or old run_records here. Missing/invalid
        source proof is an error, NOT a zero counter.
        """
        from engine.calm_reels_cycle import FreshCycleSnapshot, FreshCycleError

        rows = self._request(
            "GET", "calm_reels_v1_cycle_state",
            params={
                "singleton": "eq.true",
                "select": "catalog_id,catalog_sha256,total_ayahs,cycle_number,started_at",
                "limit": "2",
            },
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise FreshCycleError("FRESH_CYCLE_STATE_UNAVAILABLE")
        state = rows[0]
        if not isinstance(state, Mapping) or type(state.get("cycle_number")) is not int:
            raise FreshCycleError("FRESH_CYCLE_STATE_INVALID")
        posts = self._get_paginated(
            "calm_reels_v1_confirmed_posts",
            {
                "cycle_number": f"eq.{state['cycle_number']}",
                "select": "cycle_number,ayah_key,platform,run_id,media_key,external_post_id,evidence_sha256,verified_at",
            },
            order="ayah_key.asc",
        )
        if len(posts) > 2211:
            raise FreshCycleError("FRESH_CYCLE_RECEIPT_OVERFLOW")
        return FreshCycleSnapshot.from_supabase_rows(state, posts)

    def load_calm_reels_v1_reviewed_passages(self) -> tuple[tuple[str, ...], ...]:
        """Read approved whole-passage boundaries; never mutate review records.

        The service-role-only review table is authoritative for 3+ ayah clips.
        Missing or invalid evidence fails closed, never generates approvals.
        """
        return tuple(approval.ayah_keys for approval in self.load_calm_reels_v1_reviewed_passage_approvals())

    def load_calm_reels_v1_reviewed_passage_approvals(self):
        """Read validated immutable review evidence, never synthesize approval."""
        from engine.owner_reviewed_passages import parse_reviewed_passage_approvals

        rows = self._request(
            "GET", "calm_reels_v1_reviewed_passages",
            params={
                "select": "first_ayah_key,last_ayah_key,approval_reference,reviewed_at",
                "limit": "2211",
            },
        )
        return parse_reviewed_passage_approvals(rows)

    def pending_owner_dispatch_run(self, *, cycle_number: int, ayah_key: str) -> str | None:
        """Return the one outstanding run in this cycle. Never claim or send."""
        rows = self._request('GET', 'calm_reels_v1_dispatch_claims', params={
            'cycle_number': f'eq.{cycle_number}', 'state': 'eq.CLAIMED',
            'select': 'cycle_number,ayah_key,run_id', 'limit': '2',
        })
        if rows == []:
            return None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise SupabaseStateError('OWNER_V1_PENDING_CLAIM_INVALID')
        row = rows[0]
        if (type(row.get('cycle_number')) is not int or row['cycle_number'] != cycle_number
            or row.get('ayah_key') != ayah_key or not isinstance(row.get('run_id'), str)
            or not 1 <= len(row['run_id']) <= 120):
            raise SupabaseStateError('OWNER_V1_PENDING_CLAIM_MISMATCH')
        return row['run_id']

    def claim_calm_reels_v1_next_ayah(
        self, *, cycle_number: int, ayah_key: str, run_id: str,
    ) -> dict[str, Any]:
        """Atomic claim immediately before external publishing dispatch.

        DB RPC validates the frozen catalog and serializes competing claims.
        Never retry with another run_id after a timeout/unknown provider result.
        This method does NOT publish, mark a receipt, or grant rights.
        """
        from engine.calm_reels_cycle import choose_next_owner_ayah

        if type(cycle_number) is not int or cycle_number < 1:
            raise SupabaseStateError("CALM_V1_CYCLE_INVALID")
        if not isinstance(run_id, str) or not (1 <= len(run_id) <= 120):
            raise SupabaseStateError("CALM_V1_RUN_ID_INVALID")
        if not isinstance(ayah_key, str):
            raise SupabaseStateError("CALM_V1_AYAH_KEY_INVALID")
        decision = choose_next_owner_ayah(self.load_calm_reels_v1_fresh_cycle())
        if (decision.cycle_number != cycle_number
            or decision.state != "READY_FOR_TECHNICAL_PREFLIGHT"
            or decision.next_ayah_key != ayah_key):
            raise SupabaseStateError("CALM_V1_FIRST_PENDING_MISMATCH")
        result = _extract_rpc_row(self._rpc(
            "calm_reels_v1_claim_next_ayah",
            {
                "p_cycle_number": cycle_number,
                "p_ayah_key": ayah_key,
                "p_run_id": run_id,
            },
        ))
        if (result is None
            or result.get("state") not in {"CLAIMED", "ALREADY_CLAIMED"}
            or type(result.get("cycle_number")) is not int
            or result["cycle_number"] != cycle_number
            or result.get("ayah_key") != ayah_key):
            raise SupabaseStateError("CALM_V1_CLAIM_RPC_INVALID")
        return result

    def claim_calm_reels_v1_reviewed_passage(
        self, *, cycle_number: int, first_ayah_key: str, last_ayah_key: str,
        run_id: str,
        review_approval,
    ) -> dict[str, Any]:
        """One atomic SQL claim for every member of one owner-reviewed Reel.

        This is a reservation only, never a Facebook send or verified receipt.
        Unknown RPC outcomes are not retried; reconciliation is required.
        """
        from engine.calm_reels_cycle import choose_next_owner_ayah
        from engine.owner_reviewed_passages import (
            ReviewedPassageApproval, require_reviewed_owner_passage, reviewed_owner_passage_starting_at,
        )
        if type(cycle_number) is not int or cycle_number < 1:
            raise SupabaseStateError("CALM_V1_CYCLE_INVALID")
        if not isinstance(run_id, str) or not (1 <= len(run_id) <= 120):
            raise SupabaseStateError("CALM_V1_RUN_ID_INVALID")
        if (not isinstance(review_approval, ReviewedPassageApproval)
                or review_approval.first_ayah_key != first_ayah_key
                or review_approval.last_ayah_key != last_ayah_key):
            raise SupabaseStateError("CALM_V1_REVIEW_APPROVAL_REQUIRED")
        approvals = self.load_calm_reels_v1_reviewed_passage_approvals()
        registry = tuple(approval.ayah_keys for approval in approvals)
        keys = reviewed_owner_passage_starting_at(first_ayah_key, registry)
        try:
            require_reviewed_owner_passage(keys, registry)
        except ValueError as error:
            raise SupabaseStateError("CALM_V1_PASSAGE_NOT_REVIEWED") from error
        if keys[-1] != last_ayah_key:
            raise SupabaseStateError("CALM_V1_PASSAGE_RANGE_MISMATCH")
        if review_approval not in approvals:
            raise SupabaseStateError("CALM_V1_REVIEW_APPROVAL_CHANGED")
        decision = choose_next_owner_ayah(
            self.load_calm_reels_v1_fresh_cycle(), reviewed_passages=registry,
        )
        if (decision.state != "READY_FOR_TECHNICAL_PREFLIGHT"
            or decision.cycle_number != cycle_number
            or decision.next_ayah_key != first_ayah_key
            or decision.reviewed_ayah_keys != keys):
            raise SupabaseStateError("CALM_V1_FIRST_PENDING_MISMATCH")
        result = _extract_rpc_row(self._rpc(
            "calm_reels_v1_claim_next_reviewed_passage", {
                "p_cycle_number": cycle_number,
                "p_first_ayah_key": first_ayah_key,
                "p_last_ayah_key": last_ayah_key,
                "p_run_id": run_id,
                "p_review_approval_sha256": review_approval.sha256,
            },
        ))
        if (result is None or result.get("state") not in ("CLAIMED", "ALREADY_CLAIMED")
            or type(result.get("cycle_number")) is not int
            or result["cycle_number"] != cycle_number
            or result.get("ayah_key") != first_ayah_key
            or result.get("last_ayah_key") != last_ayah_key
            or result.get("review_approval_sha256") != review_approval.sha256):
            raise SupabaseStateError("CALM_V1_PASSAGE_CLAIM_RPC_INVALID")
        return result

    def record_calm_reels_v1_verified_facebook_post(
        self, *, cycle_number: int, run_id: str, facebook_video_id: str,
        evidence_sha256: str, verified_at: str,
    ) -> dict[str, Any]:
        """Commit independently verified Facebook evidence via atomic DB RPC.

        The caller must save the independently verified, source-locked
        RunRecord first. DB also checks the pending claim, video ID, source
        catalog order, evidence fingerprint, and transactional rollover.
        """
        import re
        from datetime import datetime

        if type(cycle_number) is not int or cycle_number < 1:
            raise SupabaseStateError("CALM_V1_RECEIPT_CYCLE_INVALID")
        if not isinstance(run_id, str) or not (1 <= len(run_id) <= 120):
            raise SupabaseStateError("CALM_V1_RECEIPT_RUN_INVALID")
        if (not isinstance(facebook_video_id, str)
            or re.fullmatch(r"[1-9][0-9]{5,24}", facebook_video_id) is None):
            raise SupabaseStateError("CALM_V1_RECEIPT_VIDEO_ID_INVALID")
        if (not isinstance(evidence_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", evidence_sha256) is None):
            raise SupabaseStateError("CALM_V1_RECEIPT_EVIDENCE_INVALID")
        if not isinstance(verified_at, str):
            raise SupabaseStateError("CALM_V1_RECEIPT_TIME_INVALID")
        try:
            timestamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise SupabaseStateError("CALM_V1_RECEIPT_TIME_INVALID") from error
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise SupabaseStateError("CALM_V1_RECEIPT_TIME_INVALID")

        result = _extract_rpc_row(self._rpc(
            "calm_reels_v1_record_verified_facebook_post",
            {
                "p_cycle_number": cycle_number,
                "p_run_id": run_id,
                "p_facebook_post_id": facebook_video_id,
                "p_platform_evidence_sha256": evidence_sha256,
                "p_verified_at": verified_at,
            },
        ))
        if result is None or result.get("state") not in (
            "RECORDED", "ALREADY_RECORDED", "ROLLED_OVER",
        ):
            raise SupabaseStateError("CALM_V1_RECEIPT_RPC_INVALID")
        state = result["state"]
        count = result.get("confirmed_ayahs")
        next_cycle = result.get("cycle_number")
        if (type(count) is not int or type(next_cycle) is not int
            or not (
                (state == "ROLLED_OVER" and next_cycle == cycle_number + 1 and count == 0)
                or (state != "ROLLED_OVER" and next_cycle == cycle_number
                    and 1 <= count <= 2211)
            )):
            raise SupabaseStateError("CALM_V1_RECEIPT_RPC_INVALID")
        return result

    def get_production_authorized_passages(self, policy_version: str) -> list[dict[str, Any]]:
        """Read the live, server-validated production passage catalog.

        The RPC resolves legacy approvals and automatically authorized final
        screening decisions.  It intentionally returns exact passages rather
        than an unconstrained collection of individual ayahs.
        """
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise SupabaseStateError("A non-empty editorial policy version is required.")
        result = self._rpc(
            "get_production_authorized_passages_v2",
            {"p_policy_version": policy_version},
        )
        if not isinstance(result, list) or any(not isinstance(row, Mapping) for row in result):
            raise SupabaseStateError(
                "Supabase production authorization RPC returned a malformed response."
            )
        return [dict(row) for row in result]

    def check_schema_readiness(self) -> dict[str, bool]:
        """Check whether each required Supabase table exists and is accessible."""
        self._assert_live_transport_allowed()
        results: dict[str, bool] = {}
        for table in REQUIRED_SUPABASE_TABLES:
            try:
                response = self.session.request(
                    "GET",
                    f"{self.config.url}/rest/v1/{table}",
                    params={"limit": "0"},
                    headers={
                        "apikey": self.config.service_role_key,
                        "Authorization": f"Bearer {self.config.service_role_key}",
                    },
                    timeout=10,
                )
                results[table] = bool(getattr(response, "status_code", 0) == 200)
            except Exception:
                results[table] = False
        return results

    # =========================================================================
    # APP CONFIG
    # =========================================================================

    def load_config(self) -> Optional[StoredConfig]:
        rows = self._request(
            "GET", "app_config", params={"singleton": "eq.true", "select": "value"}
        )
        if not rows:
            return None
        return stored_config_from_dict(dict(rows[0]["value"]))

    def save_config(self, config: StoredConfig) -> None:
        self._request(
            "POST",
            "app_config",
            payload={
                "singleton": True,
                "schema_version": config.schema_version,
                "value": asdict(config),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    # =========================================================================
    # COVERAGE D-011
    # =========================================================================

    def load_coverage(self) -> CoverageState:
        """Load current coverage state. If table is empty, returns fresh default."""
        rows = self._request(
            "GET",
            "coverage_state",
            params={"singleton": "eq.true", "select": "value"},
        )
        if not rows:
            return CoverageState(
                cycle_number=1,
                total_ayahs=TOTAL_CANONICAL_AYAHS,
                covered_count=0,
                covered_ayahs=(),
                processed_run_ids=(),
            )
        raw = dict(rows[0]["value"])
        return CoverageState(
            schema_version=raw.get("schema_version", 1),
            cycle_number=raw.get("cycle_number", 1),
            total_ayahs=raw.get("total_ayahs", TOTAL_CANONICAL_AYAHS),
            covered_count=raw.get("covered_count", 0),
            covered_ayahs=tuple(raw.get("covered_ayahs", ())),
            processed_run_ids=tuple(raw.get("processed_run_ids", ())),
            last_updated_at=raw.get("last_updated_at"),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
        )

    def save_coverage(self, state: CoverageState) -> None:
        self._request(
            "POST",
            "coverage_state",
            payload={
                "singleton": True,
                "schema_version": state.schema_version,
                "cycle_number": state.cycle_number,
                "total_ayahs": state.total_ayahs,
                "covered_count": state.covered_count,
                "value": asdict(state),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def archive_completed_cycle(
        self, cycle_number: int, completed_at: str, value: Mapping[str, Any]
    ) -> None:
        self._request(
            "POST",
            "coverage_cycles",
            payload={
                "cycle_number": cycle_number,
                "completed_at": completed_at,
                "value": dict(value),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def record_covered_ayahs(
        self,
        run_id: str,
        ayah_keys: Sequence[str],
        production_authorization: Optional[Any] = None,
    ) -> CoverageState:
        """Record an eligible production range and archive completed editorial cycles."""
        from engine.reel_theme_policy import eligible_verse_keys, evaluate_verse_keys

        verdict = evaluate_verse_keys(ayah_keys, production_authorization=production_authorization)
        if not verdict.eligible:
            raise SupabaseStateError(verdict.reason_code)
        state = self.load_coverage()
        if run_id in state.processed_run_ids:
            return state

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        updated_covered = sorted(set(state.covered_ayahs) | set(ayah_keys))
        covered_count = len(updated_covered)
        recent_runs = list(state.processed_run_ids) + [run_id]

        started_at = state.started_at or now_iso

        authorized_keys = (
            production_authorization.authorized_verse_keys
            if production_authorization is not None
            else eligible_verse_keys()
        )
        if authorized_keys.issubset(set(updated_covered)):
            # Cycle complete: archive and rollover
            cycle_num = state.cycle_number
            completed_snapshot = CoverageState(
                schema_version=state.schema_version,
                cycle_number=cycle_num,
                total_ayahs=state.total_ayahs,
                covered_count=covered_count,
                covered_ayahs=tuple(updated_covered),
                processed_run_ids=tuple(recent_runs),
                last_updated_at=now_iso,
                started_at=started_at,
                completed_at=now_iso,
            )
            self.archive_completed_cycle(cycle_num, now_iso, asdict(completed_snapshot))

            new_state = CoverageState(
                schema_version=state.schema_version,
                cycle_number=cycle_num + 1,
                total_ayahs=state.total_ayahs,
                covered_count=0,
                covered_ayahs=(),
                processed_run_ids=(run_id,),
                last_updated_at=now_iso,
                started_at=now_iso,
                completed_at=None,
            )
            self.save_coverage(new_state)
            return new_state

        updated_state = CoverageState(
            schema_version=state.schema_version,
            cycle_number=state.cycle_number,
            total_ayahs=state.total_ayahs,
            covered_count=covered_count,
            covered_ayahs=tuple(updated_covered),
            processed_run_ids=tuple(recent_runs),
            last_updated_at=now_iso,
            started_at=started_at,
            completed_at=None,
        )
        self.save_coverage(updated_state)
        return updated_state

    # =========================================================================
    # RUN RECORDS
    # =========================================================================

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        rows = self._request(
            "GET",
            "run_records",
            params={"run_id": f"eq.{run_id}", "select": "value", "limit": "1"},
        )
        return run_record_from_dict(dict(rows[0]["value"])) if rows else None

    def get_run_record(
        self,
        date: str,
        slot: str,
        run_id: Optional[str] = None,
        run_kind: Optional[str] = None,
    ) -> Optional[RunRecord]:
        if run_id:
            return self.get_run(run_id)
        params: dict[str, str] = {
            "berlin_date": f"eq.{date}",
            "select": "value",
            "order": "completed_at.desc.nullslast",
            "limit": "1",
        }
        if slot and slot not in ("manual-production", "test"):
            params["schedule_slot_id"] = f"eq.{slot}"
        if run_kind:
            params["run_kind"] = f"eq.{run_kind}"
        rows = self._request("GET", "run_records", params=params)
        return run_record_from_dict(dict(rows[0]["value"])) if rows else None

    def save_run(self, record: RunRecord) -> None:
        slot_id = (
            record.schedule_slot_id
            if (record.run_kind != "test" and record.slot != "test")
            else None
        )
        self._request(
            "POST",
            "run_records",
            payload={
                "run_id": record.run_id,
                "run_kind": record.run_kind,
                "berlin_date": record.date,
                "schedule_slot_id": slot_id,
                "status": record.status,
                "completed_at": record.completed_at or record.started_at,
                "final_safety_passed": record.final_safety_passed,
                "media_key": record.media_key,
                "value": asdict(record),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def start_run_record(self, record: RunRecord) -> None:
        self.save_run(record)

    def complete_run_record(self, record: RunRecord) -> None:
        self.save_run(record)

    def fail_run_record(self, record: RunRecord) -> None:
        self.save_run(record)

    # =========================================================================
    # BUFFER WRITE LIFECYCLES
    # =========================================================================

    def get_buffer_publication_claim(
        self, run_id: str, platform: str, publisher_backend: str, channel_id: str
    ) -> Optional[dict[str, Any]]:
        rows = self._request(
            "GET", "buffer_publication_claims",
            params={"run_id": f"eq.{run_id}", "platform": f"eq.{platform}",
                    "publisher_backend": f"eq.{publisher_backend}", "channel_id": f"eq.{channel_id}",
                    "select": "*", "limit": "1"},
        )
        return dict(rows[0]) if rows else None

    def claim_buffer_publication_dispatch(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        """Insert the logical publication claim exactly once, or return it.

        The composite key is the immutable publication target, so two workers
        cannot both obtain a first-dispatch claim for the same target.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {
            "run_id": str(claim["run_id"]),
            "platform": str(claim["platform"]),
            "publisher_backend": str(claim["publisher_backend"]),
            "channel_id": str(claim["channel_id"]),
            "publication_fingerprint": str(claim["publication_fingerprint"]),
            "state": str(claim.get("state") or "DISPATCHING"),
            "dispatch_attempts": int(claim.get("dispatch_attempts") or 1),
            "updated_at": now,
            "value": {**dict(claim), "created_at": now, "updated_at": now},
        }
        result = self._request("POST", "buffer_publication_claims", payload=payload, prefer="return=representation")
        if isinstance(result, dict) and result.get("_conflict"):
            existing = self.get_buffer_publication_claim(payload["run_id"], payload["platform"], payload["publisher_backend"], payload["channel_id"])
            if existing is None:
                raise SupabaseStateError("Buffer publication claim conflicted but could not be read.")
            return {**existing, "_claim_created": False}
        if isinstance(result, Sequence) and result:
            return {**dict(result[0]), "_claim_created": True}
        # PostgREST may be configured to return minimal despite the preference.
        return {**payload, "buffer_source": claim.get("buffer_source"), "_claim_created": True}

    def transition_buffer_publication_claim(
        self, run_id: str, platform: str, publisher_backend: str, channel_id: str,
        expected_states: tuple[str, ...], state: str, updates: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        if not expected_states:
            raise SupabaseStateError("Expected Buffer publication states are required.")
        current = self.get_buffer_publication_claim(run_id, platform, publisher_backend, channel_id)
        if current is None:
            return None
        value = dict(current.get("value") or {})
        value.update(dict(updates))
        value["state"] = state
        value["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        params = {
            "run_id": f"eq.{run_id}",
            "platform": f"eq.{platform}",
            "publisher_backend": f"eq.{publisher_backend}",
            "channel_id": f"eq.{channel_id}",
            "state": "in.(" + ",".join(expected_states) + ")",
        }
        payload = {
            "state": state,
            "dispatch_attempts": int(value.get("dispatch_attempts") or current.get("dispatch_attempts") or 1),
            "buffer_post_id": value.get("buffer_post_id"),
            "buffer_post_status": value.get("buffer_post_status"),
            "external_link": value.get("external_link"),
            "accepted_at": value.get("accepted_at"),
            "last_error_code": value.get("last_error_code"),
            "updated_at": value["updated_at"],
            "value": value,
        }
        rows = self._request("PATCH", "buffer_publication_claims", params=params, payload=payload, prefer="return=representation")
        return dict(rows[0]) if isinstance(rows, Sequence) and rows else None

    def get_buffer_draft_validation(self, validation_id: str) -> Optional[dict[str, Any]]:
        rows = self._request(
            "GET", "buffer_draft_validations",
            params={"validation_id": f"eq.{validation_id}", "select": "*", "limit": "1"},
        )
        return dict(rows[0]) if rows else None

    def claim_buffer_draft_validation(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {
            "validation_id": str(draft["validation_id"]), "run_id": str(draft["run_id"]),
            "channel_id": str(draft["channel_id"]), "buffer_source": str(draft["buffer_source"]),
            "state": str(draft.get("state") or "CREATING"),
            "updated_at": now,
            "value": {**dict(draft), "created_at": now, "updated_at": now},
        }
        result = self._request("POST", "buffer_draft_validations", payload=payload, prefer="return=representation")
        if isinstance(result, dict) and result.get("_conflict"):
            existing = self.get_buffer_draft_validation(payload["validation_id"])
            if existing is None:
                raise SupabaseStateError("Buffer draft claim conflicted but could not be read.")
            return {**existing, "_claim_created": False}
        if isinstance(result, Sequence) and result:
            return {**dict(result[0]), "_claim_created": True}
        return {**payload, "_claim_created": True}

    def transition_buffer_draft_validation(
        self, validation_id: str, expected_states: tuple[str, ...], state: str, updates: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        current = self.get_buffer_draft_validation(validation_id)
        if current is None:
            return None
        value = dict(current.get("value") or {})
        value.update(dict(updates)); value["state"] = state
        value["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = self._request(
            "PATCH", "buffer_draft_validations",
            params={"validation_id": f"eq.{validation_id}", "state": "in.(" + ",".join(expected_states) + ")"},
            payload={"state": state, "draft_post_id": value.get("draft_post_id"), "last_error_code": value.get("last_error_code"), "updated_at": value["updated_at"], "value": value},
            prefer="return=representation",
        )
        return dict(rows[0]) if isinstance(rows, Sequence) and rows else None

    def list_run_records(
        self,
        limit: int = 50,
        run_kind: Optional[str] = None,
        status: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Sequence[RunRecord]:
        params: dict[str, str] = {
            "select": "value",
            "order": "completed_at.desc.nullslast",
            "limit": str(limit),
        }
        if run_kind:
            params["run_kind"] = f"eq.{run_kind}"
        if status:
            params["status"] = f"eq.{status}"
        if date:
            params["berlin_date"] = f"eq.{date}"

        rows = self._request("GET", "run_records", params=params)
        return tuple(run_record_from_dict(dict(r["value"])) for r in rows)

    def get_production_background_usage(self) -> Sequence[HistoricalBackgroundUse]:
        # A unique cursor also works when the API caps responses below our page size.
        params = {
            "run_kind": "in.(scheduled,manual_production)",
            "status": "eq.SUCCESS",
            "media_key": "not.is.null",
            "or": "(final_safety_passed.is.true,final_safety_passed.is.null)",
            "select": ("run_id,completed_at,started_at:value->>started_at,"
                       "pexels_video_ids:value->pexels_video_ids,"
                       "verified_backgrounds:value->provenance->verified_backgrounds,"
                       "pexels_videos:value->provenance->pexels_videos"),
            "order": "run_id.asc",
            "limit": str(SUPABASE_READ_PAGE_SIZE),
        }
        usage = []
        while True:
            page = self._request("GET", "run_records", params=params)
            if not isinstance(page, list):
                raise SupabaseStateError("Invalid production background history page.")
            if not page:
                return tuple(usage)
            cursor = "gt." + page[-1]["run_id"]
            if cursor == params.get("run_id"):
                raise SupabaseStateError("Production background history cursor did not advance.")
            for row in page:
                usage.extend(historical_background_usage(row))
            params = {**params, "run_id": cursor}

    def get_recent_content_history(
        self, last_n_runs: int = 30
    ) -> Sequence[dict[str, Any]]:
        return recent_content_history_entries(self.list_run_records(limit=last_n_runs))

    def list_successful_production_runs(
        self, year: int, month: int
    ) -> Sequence[RunRecord]:
        start = f"{year:04d}-{month:02d}-01"
        end = (
            f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}-01"
        )
        rows = self._request(
            "GET",
            "run_records",
            params={
                "run_kind": "in.(scheduled,manual_production)",
                "status": "eq.SUCCESS",
                "final_safety_passed": "is.true",
                "completed_at": f"gte.{start}",
                "completed_at": f"lt.{end}",
                "media_key": "not.is.null",
                "select": "value",
                "order": "completed_at.asc",
            },
        )
        return tuple(run_record_from_dict(dict(row["value"])) for row in rows)

    # =========================================================================
    # SCHEDULER CLAIMS
    # =========================================================================

    def claim_scheduler_slot(
        self,
        berlin_date: str,
        schedule_slot_id: str,
        claimed_at: Optional[str] = None,
        worker_run_id: Optional[str] = None,
    ) -> bool:
        """Atomically claim a scheduler slot. Returns False if already claimed."""
        claimed_at_iso = (
            claimed_at
            or datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        run_id = worker_run_id or f"claim_{berlin_date}_{schedule_slot_id}"
        val = {
            "date": berlin_date,
            "slot_id": schedule_slot_id,
            "run_id": run_id,
            "status": "CLAIMED",
            "claimed_at": claimed_at_iso,
            "updated_at": claimed_at_iso,
        }
        res = self._request(
            "POST",
            "scheduler_claims",
            payload={
                "berlin_date": berlin_date,
                "slot_id": schedule_slot_id,
                "run_id": run_id,
                "status": "CLAIMED",
                "value": val,
            },
            prefer="return=minimal",
        )
        if isinstance(res, dict) and res.get("_conflict"):
            return False
        return True

    def is_scheduler_slot_claimed(
        self, berlin_date: str, schedule_slot_id: str
    ) -> bool:
        rows = self._request(
            "GET",
            "scheduler_claims",
            params={
                "berlin_date": f"eq.{berlin_date}",
                "slot_id": f"eq.{schedule_slot_id}",
                "select": "run_id",
                "limit": "1",
            },
        )
        return bool(rows)

    def get_scheduler_claim(
        self, berlin_date: str, schedule_slot_id: str
    ) -> Optional[dict[str, Any]]:
        rows = self._request(
            "GET",
            "scheduler_claims",
            params={
                "berlin_date": f"eq.{berlin_date}",
                "slot_id": f"eq.{schedule_slot_id}",
                "select": "*",
                "limit": "1",
            },
        )
        return dict(rows[0]) if rows else None

    def release_scheduler_slot(
        self, berlin_date: str, schedule_slot_id: str
    ) -> None:
        self._request(
            "DELETE",
            "scheduler_claims",
            params={
                "berlin_date": f"eq.{berlin_date}",
                "slot_id": f"eq.{schedule_slot_id}",
            },
            prefer="return=minimal",
        )

    def update_scheduler_claim_status(
        self,
        berlin_date: str,
        schedule_slot_id: str,
        status: str,
        failure_reason: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if failure_reason:
            payload["value"] = {
                "date": berlin_date,
                "slot_id": schedule_slot_id,
                "status": status,
                "failure_reason": failure_reason,
            }
        self._request(
            "PATCH",
            "scheduler_claims",
            params={
                "berlin_date": f"eq.{berlin_date}",
                "slot_id": f"eq.{schedule_slot_id}",
            },
            payload=payload,
            prefer="return=minimal",
        )

    # =========================================================================
    # BACKGROUND POOL
    # =========================================================================

    def enqueue_background_candidate(
        self,
        provider: str,
        provider_asset_id: str,
        provider_variant_id: str,
        search_query: str,
        metadata: Mapping[str, Any],
        status: str = "PENDING",
    ) -> dict[str, Any]:
        result = self._rpc("enqueue_background_candidate", {
            "p_provider": provider,
            "p_provider_asset_id": str(provider_asset_id),
            "p_provider_variant_id": str(provider_variant_id),
            "p_search_query": search_query,
            "p_metadata": dict(metadata),
            "p_status": status,
        })
        row = _extract_rpc_row(result)
        return row if row is not None else {}

    def claim_background_candidate(self, worker_id: str, lease_seconds: int = 900) -> Optional[dict[str, Any]]:
        result = self._rpc("claim_background_candidate", {
            "p_worker_id": worker_id,
            "p_lease_seconds": int(lease_seconds),
        })
        return _extract_rpc_row(result)

    def complete_background_candidate(
        self,
        candidate_id: str,
        lease_token: str,
        status: str,
        error_code: Optional[str] = None,
        value: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        result = self._rpc("complete_background_candidate", {
            "p_candidate_id": candidate_id,
            "p_lease_token": lease_token,
            "p_status": status,
            "p_error_code": error_code,
            "p_value": dict(value or {}),
        })
        row = _extract_rpc_row(result)
        if row is None:
            raise SupabaseStateError("Background candidate lease was lost before terminal transition.")
        return row

    def background_candidate_summary(self) -> dict[str, Any]:
        rows = self._get_paginated(
            "background_candidates",
            {"select": "status,updated_at,discovered_at"},
            order="candidate_id.asc",
        )
        counts = {"total": 0, "pending": 0, "processing": 0, "verified": 0, "safety_rejected": 0, "metadata_ineligible": 0, "transient_failure": 0}
        last_activity: Optional[str] = None
        last_discovery: Optional[str] = None
        for row in rows:
            counts["total"] += 1
            key = str(row.get("status") or "").lower()
            if key in counts:
                counts[key] += 1
            updated = row.get("updated_at")
            discovered = row.get("discovered_at")
            if isinstance(updated, str) and (last_activity is None or updated > last_activity):
                last_activity = updated
            if isinstance(discovered, str) and (last_discovery is None or discovered > last_discovery):
                last_discovery = discovered
        return {**counts, "last_activity": last_activity, "last_discovery": last_discovery}

    def get_verified_background_assets(
        self,
        provider: Optional[str] = None,
        policy_fingerprint: Optional[str] = None,
    ) -> Sequence[dict[str, Any]]:
        params: dict[str, str] = {
            "status": "eq.verified",
            "select": "provider,provider_asset_id,provider_variant_id,policy_fingerprint,content_sha256,value",
        }
        if provider:
            params["provider"] = f"eq.{provider}"
        if policy_fingerprint:
            params["policy_fingerprint"] = f"eq.{policy_fingerprint}"
        rows = self._get_paginated(
            "background_assets",
            params,
            order="provider.asc,provider_asset_id.asc,provider_variant_id.asc,policy_fingerprint.asc,content_sha256.asc",
        )
        return tuple(dict(r) for r in rows)

    def count_selectable_background_assets(
        self,
        policy_fingerprint: Optional[str] = None,
    ) -> int:
        """Return the count of verified, non-HIGH_RISK background assets for production selection."""
        from engine.background_pool import CURRENT_SAFETY_POLICY_FINGERPRINT
        fingerprint = policy_fingerprint or CURRENT_SAFETY_POLICY_FINGERPRINT
        assets = self.get_verified_background_assets(policy_fingerprint=fingerprint)
        selectable = 0
        for asset in assets:
            val = asset.get("value")
            if isinstance(val, Mapping):
                render_robustness = val.get("render_robustness")
                tier = (
                    render_robustness.get("risk_tier")
                    if isinstance(render_robustness, Mapping)
                    else val.get("risk_tier")
                )
                if tier == "HIGH_RISK":
                    continue
            selectable += 1
        return selectable

    def record_background_event(
        self,
        event_type: str,
        provider: str,
        provider_asset_id: str,
        provider_variant_id: str,
        fingerprint: str,
        value: Mapping[str, Any],
        occurred_at: Optional[str] = None,
    ) -> str:
        occ = occurred_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        evt_key = f"{provider}_{provider_asset_id}_{provider_variant_id}_{event_type}_{occ}"
        if event_type == "usage":
            # A replay of one render upserts the same event, even on another day.
            evt_key = json.dumps([event_type, value["run_id"], provider, provider_asset_id, provider_variant_id])
        event_id = f"evt_{hashlib.sha256(evt_key.encode('utf-8')).hexdigest()[:24]}"
        self._request(
            "POST",
            "background_events",
            payload={
                "event_id": event_id,
                "event_type": event_type,
                "provider": provider,
                "provider_asset_id": str(provider_asset_id),
                "provider_variant_id": str(provider_variant_id),
                "fingerprint": fingerprint,
                "occurred_at": occ,
                "value": dict(value),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return event_id

    def record_background_usage(
        self,
        provider: str,
        provider_asset_id: str,
        provider_variant_id: str,
        content_sha256: str,
        run_id: str,
        value: Mapping[str, Any],
    ) -> None:
        self.record_background_event(
            event_type="usage",
            provider=provider,
            provider_asset_id=provider_asset_id,
            provider_variant_id=provider_variant_id,
            fingerprint=content_sha256,
            value={**value, "run_id": run_id},
            occurred_at=value["used_at"],
        )

    def record_render_safety_failure(
        self,
        provider: str,
        provider_asset_id: str,
        provider_variant_id: str,
        content_sha256: str,
        run_id: str,
        attempt_number: int,
        start_offset_seconds: float,
        verse_keys: Sequence[str],
        render_config_fingerprint: str,
        failure_reason: str,
        detector_counts: Mapping[str, int],
        background_plan: Optional[Sequence[Mapping[str, Any]]] = None,
        occurred_at: Optional[str] = None,
    ) -> str:
        occ = occurred_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        offset_bucket = int(math.floor(start_offset_seconds)) if isinstance(start_offset_seconds, (int, float)) else 0

        # Canonical retry identity
        plan_segments: list[str] = []
        if background_plan:
            for seg in background_plan:
                s_prov = str(seg.get("provider", "unknown"))
                s_aid = str(seg.get("provider_asset_id") or seg.get("asset_id", "0"))
                s_vid = str(seg.get("provider_variant_id") or seg.get("variant_id", "0"))
                s_off = int(math.floor(seg.get("start_offset_seconds", 0.0)))
                plan_segments.append(f"{s_prov}:{s_aid}:{s_vid}:{s_off}")
        plan_identity = "|".join(plan_segments) if plan_segments else f"{provider}:{provider_asset_id}:{provider_variant_id}:{offset_bucket}"
        canonical_raw = f"{run_id}:{attempt_number}:{plan_identity}:{render_config_fingerprint}"
        event_id = f"rsf_{hashlib.sha256(canonical_raw.encode('utf-8')).hexdigest()}"

        value_payload: dict[str, Any] = {
            "run_id": run_id,
            "attempt_number": attempt_number,
            "start_offset_seconds": float(start_offset_seconds),
            "offset_bucket_seconds": offset_bucket,
            "verse_keys": list(verse_keys),
            "content_sha256": content_sha256,
            "render_config_fingerprint": render_config_fingerprint,
            "detector_counts": dict(detector_counts),
            "failure_reason": failure_reason,
        }
        if background_plan is not None:
            value_payload["background_plan"] = list(background_plan)

        self._request(
            "POST",
            "background_events",
            payload={
                "event_id": event_id,
                "event_type": "render_safety_failure",
                "provider": provider,
                "provider_asset_id": str(provider_asset_id),
                "provider_variant_id": str(provider_variant_id),
                "fingerprint": render_config_fingerprint,
                "occurred_at": occ,
                "value": value_payload,
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
        return event_id

    def save_builder_run(
        self, builder_run_id: str, status: str, value: Mapping[str, Any]
    ) -> None:
        self._request(
            "POST",
            "background_builder_runs",
            payload={
                "builder_run_id": builder_run_id,
                "status": status,
                "value": dict(value),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def get_builder_run(self, builder_run_id: str) -> Optional[dict[str, Any]]:
        rows = self._request(
            "GET",
            "background_builder_runs",
            params={"builder_run_id": f"eq.{builder_run_id}", "select": "*", "limit": "1"},
        )
        return dict(rows[0]) if rows else None

    def save_query_metrics(
        self,
        builder_run_id: str,
        provider: str,
        query: str,
        value: Mapping[str, Any],
    ) -> None:
        self._request(
            "POST",
            "background_query_metrics",
            payload={
                "builder_run_id": builder_run_id,
                "provider": provider,
                "query": query,
                "value": dict(value),
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def get_background_pool_stats(self) -> dict[str, Any]:
        """Compute pool stats from Supabase tables without B2 access."""
        verified_rows = self._get_paginated(
            "background_assets",
            {"status": "eq.verified", "select": "provider"},
            order="provider.asc,provider_asset_id.asc,provider_variant_id.asc,policy_fingerprint.asc,content_sha256.asc",
        )
        v_count = len(verified_rows)
        p_count = sum(1 for r in verified_rows if r.get("provider") == "pexels")
        pix_count = sum(1 for r in verified_rows if r.get("provider") == "pixabay")

        event_rows = self._get_paginated(
            "background_events",
            {"select": "event_type"},
            order="event_id.asc",
        )
        event_counts: dict[str, int] = {}
        for r in event_rows:
            et = r.get("event_type", "unknown")
            event_counts[et] = event_counts.get(et, 0) + 1

        return {
            "verified_count": v_count,
            "pexels_verified": p_count,
            "pixabay_verified": pix_count,
            "safety_rejected_count": event_counts.get("safety_rejected", 0),
            "metadata_ineligible_count": event_counts.get("metadata_ineligible", 0),
            "transient_failure_count": event_counts.get("transient_failure", 0),
            "total_events": len(event_rows),
        }

    # =========================================================================
    # META CREDENTIALS
    # =========================================================================

    def save_encrypted_meta_credentials(self, envelope: str) -> None:
        if not envelope:
            raise SupabaseStateError("Encrypted Meta credential envelope cannot be empty.")
        self._request(
            "POST",
            "meta_credentials",
            payload={"singleton": True, "encrypted_envelope": envelope},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def load_encrypted_meta_credentials(self) -> Optional[str]:
        rows = self._request(
            "GET",
            "meta_credentials",
            params={"singleton": "eq.true", "select": "encrypted_envelope"},
        )
        return str(rows[0]["encrypted_envelope"]) if rows else None

    def delete_encrypted_meta_credentials(self) -> None:
        self._request(
            "DELETE",
            "meta_credentials",
            params={"singleton": "eq.true"},
            prefer="return=minimal",
        )
