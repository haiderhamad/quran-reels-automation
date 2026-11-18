"""Exact owner-reviewed coherent Quran ranges; publication rights remain in SQL.

The frozen 2,211-ayah catalog is NOT permission to join neighbouring verses.
Production reads a service-role-only registry of explicitly reviewed ranges
before selection; final dispatch must independently match the database claim.
"""
from __future__ import annotations

from datetime import date
from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Mapping, Sequence


# The installation must supply its own exact, evidence-bound reviews.
REVIEWED_OWNER_PASSAGES: tuple[tuple[str, ...], ...] = ()

_KEY_PATTERN = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")


def reviewed_owner_passage_starting_at(
    ayah_key: str,
    approved_passages: Sequence[tuple[str, ...]] = REVIEWED_OWNER_PASSAGES,
) -> tuple[str, ...]:
    """Return an exact reviewed range only, never infer contextual boundaries."""
    return next((keys for keys in approved_passages if keys[0] == ayah_key), ())


def require_contiguous_owner_passage(keys: tuple[str, ...]) -> None:
    """Structure-only validation; this does NOT authorize publication."""
    if not isinstance(keys, tuple) or len(keys) < 3:
        raise ValueError("OWNER_V1_REVIEWED_PASSAGE_NOT_CONTIGUOUS")
    if not all(isinstance(key, str) and _KEY_PATTERN.fullmatch(key) for key in keys):
        raise ValueError("OWNER_V1_REVIEWED_PASSAGE_NOT_CONTIGUOUS")
    surah, first = keys[0].split(":")
    expected = tuple(f"{surah}:{int(first) + index}" for index in range(len(keys)))
    if keys != expected:
        raise ValueError("OWNER_V1_REVIEWED_PASSAGE_NOT_CONTIGUOUS")


def require_reviewed_owner_passage(
    keys: tuple[str, ...],
    approved_passages: Sequence[tuple[str, ...]] = REVIEWED_OWNER_PASSAGES,
) -> None:
    """Require exact registry membership and canonical within-surah adjacency."""
    if not isinstance(keys, tuple) or keys not in approved_passages:
        raise ValueError("OWNER_V1_PASSAGE_NOT_REVIEWED")
    require_contiguous_owner_passage(keys)


def parse_reviewed_passage_rows(
    rows: object,
) -> tuple[tuple[str, ...], ...]:
    """Validate read-only PostgREST owner reviews against the frozen catalog.

    Bad/overlapping/partially catalogued rows reject the *whole* registry.
    A missing review never authorizes a multi-ayah Reel.
    """
    from engine.calm_reels_cycle import owner_ordered_keys

    if not isinstance(rows, (list, tuple)) or len(rows) > 2211:
        raise ValueError("OWNER_V1_REVIEWED_REGISTRY_INVALID")
    catalog = owner_ordered_keys()
    ordinals = {key: index for index, key in enumerate(catalog)}
    passages: list[tuple[str, ...]] = []
    seen_keys: set[str] = set()

    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("OWNER_V1_REVIEWED_REGISTRY_INVALID")
        first, last = row.get("first_ayah_key"), row.get("last_ayah_key")
        approval, reviewed_at = row.get("approval_reference"), row.get("reviewed_at")
        if (not isinstance(first, str) or not _KEY_PATTERN.fullmatch(first)
                or not isinstance(last, str) or not _KEY_PATTERN.fullmatch(last)
                or not isinstance(approval, str)
                or len(approval.strip()) < 9
                or not approval.startswith("private-owner-")
                or not isinstance(reviewed_at, str)):
            raise ValueError("OWNER_V1_REVIEW_EVIDENCE_INVALID")
        try:
            if date.fromisoformat(reviewed_at) > date.today():
                raise ValueError("future review")
        except ValueError as error:
            raise ValueError("OWNER_V1_REVIEW_EVIDENCE_INVALID") from error
        if first not in ordinals or last not in ordinals:
            raise ValueError("OWNER_V1_REVIEWED_PASSAGE_CATALOG_MISMATCH")
        surah, start = map(int, first.split(":"))
        end_surah, end = map(int, last.split(":"))
        if surah != end_surah or end < start + 2:
            raise ValueError("OWNER_V1_REVIEWED_PASSAGE_NOT_CONTIGUOUS")
        keys = tuple(f"{surah}:{i}" for i in range(start, end + 1))
        offset = ordinals[first]
        if tuple(catalog[offset:offset + len(keys)]) != keys or ordinals[last] != offset + len(keys) - 1:
            raise ValueError("OWNER_V1_REVIEWED_PASSAGE_CATALOG_MISMATCH")
        if seen_keys.intersection(keys):
            raise ValueError("OWNER_V1_REVIEWED_PASSAGE_OVERLAP")
        seen_keys.update(keys)
        passages.append(keys)

    passages.sort(key=lambda keys: ordinals[keys[0]])
    return tuple(passages)


@dataclass(frozen=True)
class ReviewedPassageApproval:
    """Immutable evidence for one exact owner-reviewed passage."""

    first_ayah_key: str
    last_ayah_key: str
    approval_reference: str
    reviewed_at: str

    def __post_init__(self) -> None:
        parse_reviewed_passage_rows([self.snapshot()])
        if date.fromisoformat(self.reviewed_at).isoformat() != self.reviewed_at:
            raise ValueError("OWNER_V1_REVIEW_EVIDENCE_INVALID")

    def snapshot(self) -> dict[str, str]:
        return {"first_ayah_key": self.first_ayah_key, "last_ayah_key": self.last_ayah_key,
                "approval_reference": self.approval_reference, "reviewed_at": self.reviewed_at}

    @property
    def ayah_keys(self) -> tuple[str, ...]:
        surah, first = map(int, self.first_ayah_key.split(":"))
        last = int(self.last_ayah_key.split(":")[1])
        return tuple(f"{surah}:{ayah}" for ayah in range(first, last + 1))

    @property
    def sha256(self) -> str:
        # PostgreSQL jsonb_build_array(... )::text uses the same comma-space
        # separators. An ordered array avoids JSON object key-order ambiguity.
        body = [self.first_ayah_key, self.last_ayah_key, self.approval_reference, self.reviewed_at]
        return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode("utf-8")).hexdigest()

    @classmethod
    def from_snapshot(cls, value: object) -> ReviewedPassageApproval:
        fields = {"first_ayah_key", "last_ayah_key", "approval_reference", "reviewed_at"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("OWNER_V1_REVIEW_APPROVAL_REQUIRED")
        return cls(**value)


def parse_reviewed_passage_approvals(rows: object) -> tuple[ReviewedPassageApproval, ...]:
    keys = parse_reviewed_passage_rows(rows)
    by_first = {row["first_ayah_key"]: row for row in rows}
    return tuple(ReviewedPassageApproval.from_snapshot(by_first[passage[0]]) for passage in keys)


def require_rendered_review_approval(owner: Mapping, keys: tuple[str, ...]) -> ReviewedPassageApproval:
    approval = ReviewedPassageApproval.from_snapshot(owner.get("review_approval"))
    if approval.ayah_keys != keys or owner.get("review_approval_sha256") != approval.sha256:
        raise ValueError("OWNER_V1_REVIEW_APPROVAL_MISMATCH")
    return approval
