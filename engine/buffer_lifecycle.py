"""Durable, provider-neutral lifecycle helpers for Buffer writes.

The state rows themselves live in Supabase.  This module deliberately keeps
the decision rules independent of HTTP so they can be tested without any live
Buffer or Supabase access.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol


PUBLICATION_DISPATCHING = "DISPATCHING"
PUBLICATION_SUCCESS = "SUCCESS"
PUBLICATION_FAILED = "FAILED"
PUBLICATION_UNKNOWN = "UNKNOWN_OUTCOME"
PUBLICATION_STATES = frozenset({
    PUBLICATION_DISPATCHING,
    PUBLICATION_SUCCESS,
    PUBLICATION_FAILED,
    PUBLICATION_UNKNOWN,
})

DRAFT_CREATING = "CREATING"
DRAFT_CREATED = "CREATED"
DRAFT_CLEANUP_PENDING = "CLEANUP_PENDING"
DRAFT_DELETED = "DELETED"
DRAFT_STATES = frozenset({DRAFT_CREATING, DRAFT_CREATED, DRAFT_CLEANUP_PENDING, DRAFT_DELETED})


def publication_fingerprint(
    *,
    run_id: str,
    platform: str,
    channel_id: str,
    media_identity: str,
    caption: str,
) -> str:
    """Return a stable logical identity for one immutable publication target."""
    payload = {
        "channel_id": channel_id,
        "caption_sha256": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
        "media_identity": media_identity,
        "platform": platform,
        "post_type": f"{platform}_reel",
        "publisher_backend": "buffer",
        "run_id": run_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def publication_source(run_id: str, fingerprint: str, *, draft: bool = False) -> str:
    """Opaque Buffer tracking metadata; never part of the public caption."""
    prefix = "quran-reels-draft" if draft else "quran-reels"
    return f"{prefix}:{run_id}:{fingerprint[:24]}"


@dataclass(frozen=True)
class ReconciliationResult:
    """Result from a read-only provider reconciliation query."""

    outcome: str  # FOUND | ABSENT | INDETERMINATE
    post_id: Optional[str] = None
    external_link: Optional[str] = None
    accepted_at: Optional[str] = None
    buffer_post_status: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in {"FOUND", "ABSENT", "INDETERMINATE"}:
            raise ValueError("Invalid reconciliation outcome.")
        if self.outcome == "FOUND" and not self.post_id:
            raise ValueError("FOUND reconciliation requires a Buffer post ID.")


class LifecycleStore(Protocol):
    """Minimal durable state contract used by the coordinator."""

    def get_buffer_publication_claim(
        self, run_id: str, platform: str, publisher_backend: str, channel_id: str
    ) -> Optional[Mapping[str, Any]]: ...
    def claim_buffer_publication_dispatch(self, claim: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def transition_buffer_publication_claim(
        self, run_id: str, platform: str, publisher_backend: str, channel_id: str,
        expected_states: tuple[str, ...], state: str, updates: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]: ...


class BufferPublicationCoordinator:
    """Atomically claim, recover, and finish one logical public publication."""

    def __init__(
        self, store: LifecycleStore, *, platform: str, publisher_backend: str, channel_id: str
    ) -> None:
        self.store = store
        self.platform = platform
        self.publisher_backend = publisher_backend
        self.channel_id = channel_id

    def claim(self, *, run_id: str, fingerprint: str, source: str) -> Mapping[str, Any]:
        claim = self.store.claim_buffer_publication_dispatch({
            "run_id": run_id,
            "platform": self.platform,
            "publisher_backend": self.publisher_backend,
            "channel_id": self.channel_id,
            "publication_fingerprint": fingerprint,
            "buffer_source": source,
            "state": PUBLICATION_DISPATCHING,
            "dispatch_attempts": 1,
        })
        # A logical publication target is immutable.  Reusing it with changed
        # caption or media inputs is neither a safe retry nor permission to
        # create a second Reel.  The caller must create a separate, reviewed
        # logical publication instead.
        existing_value = claim.get("value") if isinstance(claim.get("value"), Mapping) else {}
        existing_fingerprint = claim.get("publication_fingerprint") or existing_value.get("publication_fingerprint")
        if existing_fingerprint and str(existing_fingerprint) != fingerprint:
            raise RuntimeError("BUFFER_PUBLICATION_TARGET_FINGERPRINT_MISMATCH")
        return claim

    def mark_success(
        self, run_id: str, *, post_id: str, external_link: Optional[str], accepted_at: str,
        buffer_post_status: Optional[str] = None,
    ) -> Mapping[str, Any]:
        updated = self.store.transition_buffer_publication_claim(
            run_id,
            self.platform, self.publisher_backend, self.channel_id,
            (PUBLICATION_DISPATCHING, PUBLICATION_UNKNOWN),
            PUBLICATION_SUCCESS,
            {
                "buffer_post_id": post_id,
                "external_link": external_link,
                "accepted_at": accepted_at,
                "buffer_post_status": buffer_post_status,
            },
        )
        if updated is None:
            existing = self.store.get_buffer_publication_claim(
                run_id, self.platform, self.publisher_backend, self.channel_id
            )
            if existing and existing.get("state") == PUBLICATION_SUCCESS:
                return existing
            raise RuntimeError("BUFFER_PUBLICATION_CLAIM_LOST")
        return updated

    def mark_failed_before_dispatch(self, run_id: str, code: str) -> Mapping[str, Any]:
        updated = self.store.transition_buffer_publication_claim(
            run_id, self.platform, self.publisher_backend, self.channel_id,
            (PUBLICATION_DISPATCHING,), PUBLICATION_FAILED, {"last_error_code": code}
        )
        if updated is None:
            raise RuntimeError("BUFFER_PUBLICATION_CLAIM_LOST")
        return updated

    def mark_unknown_outcome(self, run_id: str, code: str) -> Mapping[str, Any]:
        updated = self.store.transition_buffer_publication_claim(
            run_id, self.platform, self.publisher_backend, self.channel_id,
            (PUBLICATION_DISPATCHING, PUBLICATION_FAILED, PUBLICATION_UNKNOWN), PUBLICATION_UNKNOWN,
            {"last_error_code": code}
        )
        if updated is None:
            existing = self.store.get_buffer_publication_claim(
                run_id, self.platform, self.publisher_backend, self.channel_id
            )
            if existing and existing.get("state") in {PUBLICATION_UNKNOWN, PUBLICATION_SUCCESS}:
                return existing
            raise RuntimeError("BUFFER_PUBLICATION_CLAIM_LOST")
        return updated

    def recover(self, claim: Mapping[str, Any], reconciliation: ReconciliationResult) -> str:
        """Return SUCCESS, DISPATCHING (one proven-safe retry), or UNKNOWN_OUTCOME.

        Only an explicit ``ABSENT`` result allows another write.  An ordinary
        empty or failed provider response must be represented as INDETERMINATE.
        """
        run_id = str(claim["run_id"])
        if reconciliation.outcome == "FOUND":
            self.mark_success(
                run_id,
                post_id=str(reconciliation.post_id),
                external_link=reconciliation.external_link,
                accepted_at=reconciliation.accepted_at or "reconciled",
                buffer_post_status=reconciliation.buffer_post_status,
            )
            return PUBLICATION_SUCCESS
        if reconciliation.outcome != "ABSENT":
            self.mark_unknown_outcome(run_id, "BUFFER_PUBLISH_OUTCOME_UNKNOWN")
            return PUBLICATION_UNKNOWN
        attempts = int(claim.get("dispatch_attempts") or 0)
        # One post-dispatch recovery retry is deliberately bounded.  A later
        # uncertainty remains fail-closed rather than multiplying Reels.
        if attempts >= 2:
            self.mark_unknown_outcome(run_id, "BUFFER_PUBLISH_OUTCOME_UNKNOWN")
            return PUBLICATION_UNKNOWN
        updated = self.store.transition_buffer_publication_claim(
            run_id,
            self.platform, self.publisher_backend, self.channel_id,
            (PUBLICATION_UNKNOWN, PUBLICATION_FAILED),
            PUBLICATION_DISPATCHING,
            {"dispatch_attempts": attempts + 1, "last_error_code": None},
        )
        return PUBLICATION_DISPATCHING if updated is not None else PUBLICATION_UNKNOWN


class BufferDraftCoordinator:
    """Small separate lifecycle for non-public validation drafts."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def claim(self, draft: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.store.claim_buffer_draft_validation({**dict(draft), "state": DRAFT_CREATING})

    def mark_created(self, validation_id: str, draft_post_id: str) -> Optional[Mapping[str, Any]]:
        return self.store.transition_buffer_draft_validation(
            validation_id, (DRAFT_CREATING, DRAFT_CLEANUP_PENDING), DRAFT_CREATED,
            {"draft_post_id": draft_post_id, "last_error_code": None},
        )

    def mark_cleanup_pending(self, validation_id: str, draft_post_id: Optional[str], code: str) -> Optional[Mapping[str, Any]]:
        return self.store.transition_buffer_draft_validation(
            validation_id, (DRAFT_CREATING, DRAFT_CREATED, DRAFT_CLEANUP_PENDING), DRAFT_CLEANUP_PENDING,
            {"draft_post_id": draft_post_id, "last_error_code": code},
        )

    def mark_deleted(self, validation_id: str, draft_post_id: str) -> Optional[Mapping[str, Any]]:
        return self.store.transition_buffer_draft_validation(
            validation_id, (DRAFT_CREATED, DRAFT_CLEANUP_PENDING), DRAFT_DELETED,
            {"draft_post_id": draft_post_id, "last_error_code": None},
        )
