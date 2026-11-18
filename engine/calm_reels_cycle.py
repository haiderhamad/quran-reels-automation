"""Frozen v1 Quran Reel ordering: ONLY current-cycle confirmed Facebook posts.

This module is intentionally read-only. No historic coverage, Buffer acceptance,
generated video or media upload can advance the 2,211-ayah counter. Authoritative
whole-passage approval and source/rights review remain mandatory.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Sequence

from engine.calm_reels_scope import (
    CATALOG_FILE, CATALOG_SHA256, CATALOG_ID,
    CalmReelsScopeError, owner_shortlist_keys,
)
from engine.production_authorization import (
    ProductionAuthorizationCatalog, ProductionAuthorizedPassage,
    ProductionAuthorizationError,
)
from engine.owner_reviewed_passages import ReviewedPassageApproval

class FreshCycleError(ValueError):
    """Wrong, incomplete or inconsistent new publication-cycle data."""


def owner_ordered_keys() -> tuple[str, ...]:
    """Byte-lock the owner-controlled list and preserve exact canonical order."""
    try:
        raw = CATALOG_FILE.read_bytes()
        if hashlib.sha256(raw).hexdigest() != CATALOG_SHA256:
            raise ValueError("catalog SHA does not match owner freeze")
        data = json.loads(raw)
        if (data.get("catalog_id") != CATALOG_ID
            or data.get("status") != "FROZEN_OWNER_EDITORIAL_SCOPE_ONLY"
            or data.get("publication_authorized") is not False
            or data.get("ayah_count") != 2211
            or len(data["ayahs"]) != 2211):
            raise ValueError("unexpected owner catalog")
        keys = tuple(entry["key"] for entry in data["ayahs"])
        if len(set(keys)) != 2211 or set(keys) != owner_shortlist_keys():
            raise ValueError("catalog identities failed source verification")
        return keys
    except (KeyError, TypeError, OSError, ValueError) as err:
        raise FreshCycleError("FROZEN_EDITORIAL_CATALOG_UNAVAILABLE") from err


def require_owner_ayah_source(ayah_key: str, arabic_text: str) -> str:
    """Verify an exact Uthmani text byte hash against the owner-frozen manifest.

    Only an exact source hash qualifies; a normalized/guessed replacement text
    must never silently become publishable. This check is NOT a publish grant.
    """
    keys = owner_ordered_keys()  # also validates the immutable whole-file SHA
    if not isinstance(ayah_key, str) or ayah_key not in keys:
        raise FreshCycleError("OWNER_V1_AYAH_NOT_IN_FROZEN_CATALOG")
    if not isinstance(arabic_text, str) or not arabic_text.strip():
        raise FreshCycleError("OWNER_V1_ARABIC_TEXT_MISSING")
    try:
        source_bytes = CATALOG_FILE.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != CATALOG_SHA256:
            raise ValueError("Frozen owner manifest changed")
        entries = json.loads(source_bytes)["ayahs"]
        expected = next(row["arabic_sha256"] for row in entries if row["key"] == ayah_key)
    except (KeyError, StopIteration, TypeError, ValueError, OSError) as error:
        raise FreshCycleError("OWNER_V1_SOURCE_PROOF_UNAVAILABLE") from error
    digest = hashlib.sha256(arabic_text.encode("utf-8")).hexdigest()
    if digest != expected:
        raise FreshCycleError("OWNER_V1_ARABIC_SOURCE_HASH_MISMATCH")
    return digest


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.tzinfo is not None and dt.utcoffset() is not None
    except ValueError:
        return False


@dataclass(frozen=True)
class FreshCycleSnapshot:
    cycle_number: int
    started_at: str
    confirmed_ayahs: frozenset[str]

    @classmethod
    def from_supabase_rows(
        cls, state: Mapping[str, Any], receipts: Sequence[Mapping[str, Any]]
    ) -> "FreshCycleSnapshot":
        expected = owner_ordered_keys()
        if (not isinstance(state, Mapping)
            or state.get("catalog_id") != CATALOG_ID
            or state.get("catalog_sha256") != CATALOG_SHA256
            or type(state.get("total_ayahs")) is not int
            or state["total_ayahs"] != 2211
            or type(state.get("cycle_number")) is not int
            or state["cycle_number"] < 1
            or not _timestamp(state.get("started_at"))
            or not isinstance(receipts, (tuple, list))
            or len(receipts) > 2211):
            raise FreshCycleError("FRESH_CYCLE_STATE_INVALID")
        keys = set()
        for item in receipts:
            if not isinstance(item, Mapping) or item.get("cycle_number") != state["cycle_number"]:
                raise FreshCycleError("FRESH_CYCLE_RECEIPT_INVALID")
            key = item.get("ayah_key")
            if (not isinstance(key, str) or key not in expected or key in keys
                or item.get("platform") != "facebook"
                or not isinstance(item.get("run_id"), str) or not item["run_id"]
                or not isinstance(item.get("media_key"), str) or not item["media_key"]
                or not isinstance(item.get("external_post_id"), str) or not item["external_post_id"]
                or not isinstance(item.get("evidence_sha256"), str)
                or len(item["evidence_sha256"]) != 64
                or any(ch not in "0123456789abcdef" for ch in item["evidence_sha256"])
                or not _timestamp(item.get("verified_at"))):
                raise FreshCycleError("FRESH_CYCLE_RECEIPT_INVALID")
            if datetime.fromisoformat(item["verified_at"].replace("Z","+00:00")) < datetime.fromisoformat(state["started_at"].replace("Z","+00:00")):
                raise FreshCycleError("FRESH_CYCLE_PREVIOUS_EPOCH_RECEIPT")
            keys.add(key)
        return cls(
            cycle_number=state["cycle_number"],
            started_at=state["started_at"],
            confirmed_ayahs=frozenset(keys),
        )

    @property
    def confirmed_count(self) -> int:
        return len(self.confirmed_ayahs)

    @property
    def next_ayah_key(self) -> str | None:
        return next((key for key in owner_ordered_keys() if key not in self.confirmed_ayahs), None)


@dataclass(frozen=True)
class NextPassageDecision:
    """Candidate to preflight; this is not a reservation or post permission."""
    state: str  # READY_FOR_CONTEXT_PREFLIGHT | BLOCKED_NOT_AUTHORIZED | COMPLETE
    next_ayah_key: str | None
    passage: ProductionAuthorizedPassage | None


@dataclass(frozen=True)
class OwnerNextAyahDecision:
    """Read-only editorial order, not technical approval or a publish reservation."""

    state: str  # READY_FOR_TECHNICAL_PREFLIGHT | COMPLETE_PENDING_VERIFIED_ROLLOVER
    cycle_number: int
    next_ayah_key: str | None
    confirmed_count: int
    pending_successor: str | None = None
    reviewed_ayah_keys: tuple[str, ...] = ()
    reviewed_registry: tuple[tuple[str, ...], ...] = ()
    reviewed_approval: ReviewedPassageApproval | None = None


def choose_next_owner_ayah(
    cycle: FreshCycleSnapshot, *,
    reviewed_passages: Sequence[tuple[str, ...]] | None = None,
    reviewed_approvals: Sequence[ReviewedPassageApproval] = (),
) -> OwnerNextAyahDecision:
    """Select the FIRST unconfirmed owner key, independent of retired topic grants.

    Publishing still requires authenticated Quran text/hash, genuine recitation,
    context integrity, media safety, content rights, atomic dedupe reservation,
    and external platform-confirmed delivery. This function grants NONE of them.
    """
    if (
        not isinstance(cycle, FreshCycleSnapshot)
        or type(cycle.cycle_number) is not int
        or cycle.cycle_number < 1
        or not isinstance(cycle.confirmed_ayahs, frozenset)
    ):
        raise FreshCycleError("FRESH_CYCLE_STATE_INVALID")
    from engine.owner_reviewed_passages import (
        REVIEWED_OWNER_PASSAGES, require_reviewed_owner_passage,
        reviewed_owner_passage_starting_at,
    )

    sequence = owner_ordered_keys()
    if not cycle.confirmed_ayahs.issubset(set(sequence)):
        raise FreshCycleError("FRESH_CYCLE_RECEIPT_INVALID")
    approved = REVIEWED_OWNER_PASSAGES if reviewed_passages is None else reviewed_passages
    if not isinstance(approved, (list, tuple)):
        raise FreshCycleError("OWNER_V1_REVIEWED_REGISTRY_INVALID")
    approved = tuple(approved)
    seen: set[str] = set()
    # An incomplete, overlapping or partially confirmed exact passage blocks
    # publication instead of silently selecting a smaller fragment.
    for reviewed in approved:
        try:
            require_reviewed_owner_passage(reviewed, approved)
        except ValueError as error:
            raise FreshCycleError("OWNER_V1_REVIEWED_REGISTRY_INVALID") from error
        members = set(reviewed)
        if not members.issubset(sequence):
            raise FreshCycleError("OWNER_V1_REVIEWED_PASSAGE_CATALOG_MISMATCH")
        if seen.intersection(members):
            raise FreshCycleError("OWNER_V1_REVIEWED_PASSAGE_OVERLAP")
        seen.update(members)
        confirmed = members.intersection(cycle.confirmed_ayahs)
        if confirmed and len(confirmed) != len(reviewed):
            raise FreshCycleError("OWNER_V1_REVIEWED_PASSAGE_PARTIALLY_CONFIRMED")
    missing = (key for key in sequence if key not in cycle.confirmed_ayahs)
    pending, second = next(missing, None), next(missing, None)
    neighbour = None
    if pending is not None:
        surah, verse = pending.split(':')
        if second == f'{surah}:{int(verse) + 1}':
            neighbour = second
    return OwnerNextAyahDecision(
        state=(
            "READY_FOR_TECHNICAL_PREFLIGHT"
            if pending is not None else "COMPLETE_PENDING_VERIFIED_ROLLOVER"
        ),
        cycle_number=cycle.cycle_number,
        next_ayah_key=pending,
        confirmed_count=len(cycle.confirmed_ayahs),
        pending_successor=neighbour,
        reviewed_ayah_keys=(
            reviewed_owner_passage_starting_at(pending, approved) if pending is not None else ()
        ),
        reviewed_registry=approved,
        reviewed_approval=next((approval for approval in reviewed_approvals
                                if approval.first_ayah_key == pending), None),
    )


def preview_next_owner_ayah_from_store(store: object) -> OwnerNextAyahDecision:
    """Read fresh verified Facebook receipts only; never call the old grant RPC.

    No history-based coverage, writes, async dispatch or public posting.
    """
    if not callable(getattr(store, "load_calm_reels_v1_fresh_cycle", None)):
        raise FreshCycleError("FRESH_CYCLE_STORE_UNAVAILABLE")
    cycle = store.load_calm_reels_v1_fresh_cycle()
    approval_loader = getattr(store, "load_calm_reels_v1_reviewed_passage_approvals", None)
    if callable(approval_loader):
        approvals = approval_loader()
        from engine.owner_reviewed_passages import ReviewedPassageApproval
        if isinstance(approvals, (tuple, list)) and all(isinstance(item, ReviewedPassageApproval) for item in approvals):
            return choose_next_owner_ayah(cycle,
                reviewed_passages=tuple(item.ayah_keys for item in approvals), reviewed_approvals=approvals)
        if not type(store).__module__.startswith("unittest.mock"):
            raise FreshCycleError("OWNER_V1_REVIEWED_REGISTRY_UNAVAILABLE")
    # Production SupabaseStore returns a validated service-role-only registry.
    # Legacy in-memory test doubles retain their frozen Fatiha fixture.
    loader = getattr(store, "load_calm_reels_v1_reviewed_passages", None)
    reviewed: Sequence[tuple[str, ...]] | None = None
    if not callable(loader):
        if not type(store).__module__.startswith("unittest.mock"):
            raise FreshCycleError("OWNER_V1_REVIEWED_REGISTRY_UNAVAILABLE")
    else:
        candidate = loader()
        if isinstance(candidate, (tuple, list)):
            reviewed = candidate
        elif not type(store).__module__.startswith("unittest.mock"):
            raise FreshCycleError("OWNER_V1_REVIEWED_REGISTRY_UNAVAILABLE")
    return choose_next_owner_ayah(cycle, reviewed_passages=reviewed)


def preview_next_fresh_passage_from_store(store: object) -> NextPassageDecision:
    """Read fresh receipts + existing approved exact passage RPC only.

    No historic generation coverage, no writes, and no scheduler reservation.
    This preflight is NOT authorization to dispatch an automatic public post.
    """
    from engine.production_authorization import load_production_authorization_catalog

    if not callable(getattr(store, "load_calm_reels_v1_fresh_cycle", None)):
        raise FreshCycleError("FRESH_CYCLE_STORE_UNAVAILABLE")
    cycle = store.load_calm_reels_v1_fresh_cycle()
    approved = load_production_authorization_catalog(
        store, policy_version="calm-inviting-v1",
    )
    return choose_next_fresh_passage(cycle, approved)


def choose_next_fresh_passage(
    cycle: FreshCycleSnapshot,
    existing_grants: ProductionAuthorizationCatalog,
) -> NextPassageDecision:
    """Stop on the first pending ayah: never jump ahead or split an exact grant."""
    if not isinstance(cycle, FreshCycleSnapshot) or cycle.cycle_number < 1:
        raise FreshCycleError("FRESH_CYCLE_STATE_INVALID")
    if not isinstance(existing_grants, ProductionAuthorizationCatalog) or existing_grants.policy_version != "calm-inviting-v1":
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_POLICY_MISMATCH")
    sequence = owner_ordered_keys()
    if not cycle.confirmed_ayahs.issubset(sequence):
        raise FreshCycleError("FRESH_CYCLE_RECEIPT_INVALID")
    next_key = next((key for key in sequence if key not in cycle.confirmed_ayahs), None)
    if next_key is None:
        return NextPassageDecision("COMPLETE_PENDING_VERIFIED_ROLLOVER", None, None)
    possibilities = [
        passage for passage in existing_grants.passages
        if next_key in passage.verse_keys
        and passage.verse_keys.issubset(set(sequence))
        and not passage.verse_keys.intersection(cycle.confirmed_ayahs)
    ]
    if not possibilities:
        return NextPassageDecision("BLOCKED_NOT_AUTHORIZED_FOR_EXACT_PASSAGE", next_key, None)
    possibilities.sort(key=lambda p: (len(p.verse_keys), p.surah_number, p.first_ayah, p.last_ayah))
    return NextPassageDecision("READY_FOR_CONTEXT_PREFLIGHT", next_key, possibilities[0])
