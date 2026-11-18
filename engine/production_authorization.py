"""Live, fail-closed production authorization for Quran Reel passages.

The screening ledger owns the semantic decision.  Production only consumes the
server-resolved passage catalog: preserved legacy catalog passages plus final,
dual-pass ``ELIGIBLE_CANDIDATE`` decisions.  This module deliberately has no
write path and never converts an unreviewed candidate into an authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping, Sequence

from engine.quran import STANDARD_SURAH_AYAH_COUNTS


class ProductionAuthorizationError(ValueError):
    """Raised when live production authorization evidence is missing or invalid."""


def _positive_int(value: object, field: str) -> int:
    if (isinstance(value, bool)
            or not isinstance(value, (int, str))
            or (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) is None)):
        raise ProductionAuthorizationError(f"PRODUCTION_AUTHORIZATION_INVALID: {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ProductionAuthorizationError(
            f"PRODUCTION_AUTHORIZATION_INVALID: {field}"
        ) from error
    if parsed < 1:
        raise ProductionAuthorizationError(f"PRODUCTION_AUTHORIZATION_INVALID: {field}")
    return parsed


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProductionAuthorizationError(f"PRODUCTION_AUTHORIZATION_INVALID: {field}")
    parsed = tuple(str(item).strip() for item in value)
    if not parsed or any(not item for item in parsed):
        raise ProductionAuthorizationError(f"PRODUCTION_AUTHORIZATION_INVALID: {field}")
    return parsed


@dataclass(frozen=True)
class ProductionAuthorizedPassage:
    """One exact contiguous passage returned by the authoritative RPC."""

    policy_version: str
    surah_number: int
    first_ayah: int
    last_ayah: int
    authorization_ids: tuple[int, ...]
    approval_bases: tuple[str, ...]
    candidate_ayah_keys: tuple[str, ...]
    authorization_fingerprint: str
    source_evidence: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def from_rpc_row(cls, row: Mapping[str, object], policy_version: str) -> "ProductionAuthorizedPassage":
        row_policy = str(row.get("policy_version", "")).strip()
        if row_policy != policy_version:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_POLICY_MISMATCH")
        surah = _positive_int(row.get("surah_number"), "surah_number")
        first = _positive_int(row.get("passage_first_ayah"), "passage_first_ayah")
        last = _positive_int(row.get("passage_last_ayah"), "passage_last_ayah")
        if surah not in STANDARD_SURAH_AYAH_COUNTS or first > last or last > STANDARD_SURAH_AYAH_COUNTS[surah]:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_RANGE_INVALID")
        raw_ids = row.get("authorization_ids")
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)) or not raw_ids:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID: authorization_ids")
        authorization_ids = tuple(_positive_int(item, "authorization_ids") for item in raw_ids)
        if len(set(authorization_ids)) != len(authorization_ids):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_DUPLICATE_ID")
        approval_bases = _string_tuple(row.get("approval_bases"), "approval_bases")
        if any(basis not in {"LEGACY_CATALOG", "SCREENING_ELIGIBLE"} for basis in approval_bases):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_BASIS_INVALID")
        candidate_keys = _string_tuple(row.get("candidate_ayah_keys"), "candidate_ayah_keys")
        expected_prefix = f"{surah}:"
        for key in candidate_keys:
            if not key.startswith(expected_prefix):
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_CANDIDATE_INVALID")
            try:
                candidate_ayah = int(key.split(":", 1)[1])
            except (IndexError, ValueError) as error:
                raise ProductionAuthorizationError(
                    "PRODUCTION_AUTHORIZATION_CANDIDATE_INVALID"
                ) from error
            if key != f"{surah}:{candidate_ayah}":
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_CANDIDATE_INVALID")
            if not first <= candidate_ayah <= last:
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_CANDIDATE_INVALID")
        fingerprint = str(row.get("authorization_fingerprint", "")).strip()
        if len(fingerprint) != 32 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FINGERPRINT_INVALID")
        source_evidence = row.get("source_evidence", {})
        if not isinstance(source_evidence, Mapping):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SOURCE_INVALID")
        validated_sources = {}
        for key, evidence in source_evidence.items():
            if key not in {f"{surah}:{ayah}" for ayah in range(first, last + 1)} or not isinstance(evidence, Mapping):
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SOURCE_INVALID")
            for hash_field in ("source_hash", "translation_hash"):
                digest = evidence.get(hash_field)
                if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SOURCE_INVALID")
            if evidence.get("translation_language") != "en" or evidence.get("translation_version") != "1.0.19":
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SOURCE_VERSION_INVALID")
            validated_sources[key] = dict(evidence)
        if "LEGACY_CATALOG" not in approval_bases and len(validated_sources) != last - first + 1:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SOURCE_MISSING")
        return cls(
            policy_version=row_policy,
            surah_number=surah,
            first_ayah=first,
            last_ayah=last,
            authorization_ids=authorization_ids,
            approval_bases=tuple(sorted(set(approval_bases))),
            candidate_ayah_keys=tuple(sorted(set(candidate_keys))),
            authorization_fingerprint=fingerprint,
            source_evidence=validated_sources,
        )

    @property
    def verse_keys(self) -> frozenset[str]:
        return frozenset(
            f"{self.surah_number}:{ayah}"
            for ayah in range(self.first_ayah, self.last_ayah + 1)
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "surah_number": self.surah_number,
            "first_ayah": self.first_ayah,
            "last_ayah": self.last_ayah,
            "authorization_ids": list(self.authorization_ids),
            "approval_bases": list(self.approval_bases),
            "candidate_ayah_keys": list(self.candidate_ayah_keys),
            "authorization_fingerprint": self.authorization_fingerprint,
            "source_evidence": {key: dict(value) for key, value in self.source_evidence.items()},
        }


@dataclass(frozen=True)
class ProductionAuthorizationCatalog:
    """An immutable, exact-range catalog fetched for one production run."""

    policy_version: str
    passages: tuple[ProductionAuthorizedPassage, ...]

    @classmethod
    def from_rpc_rows(
        cls,
        policy_version: str,
        rows: Iterable[Mapping[str, object]],
    ) -> "ProductionAuthorizationCatalog":
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_POLICY_INVALID")
        materialized_rows = tuple(rows)
        if any(not isinstance(row, Mapping) for row in materialized_rows):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_RESPONSE_INVALID")
        parsed = tuple(
            ProductionAuthorizedPassage.from_rpc_row(row, policy_version)
            for row in materialized_rows
        )
        if not parsed:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_POOL_EMPTY")
        identities = {
            (passage.surah_number, passage.first_ayah, passage.last_ayah)
            for passage in parsed
        }
        if len(identities) != len(parsed):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_DUPLICATE_RANGE")
        return cls(policy_version=policy_version, passages=tuple(sorted(
            parsed,
            key=lambda passage: (passage.surah_number, passage.first_ayah, passage.last_ayah),
        )))

    @property
    def authorized_verse_keys(self) -> frozenset[str]:
        return frozenset().union(*(passage.verse_keys for passage in self.passages))

    def passage_for(self, surah_number: int, first_ayah: int, last_ayah: int) -> ProductionAuthorizedPassage | None:
        for passage in self.passages:
            if (
                passage.surah_number == surah_number
                and passage.first_ayah == first_ayah
                and passage.last_ayah == last_ayah
            ):
                return passage
        return None

    def evaluate_keys(self, verse_keys: Sequence[str]) -> tuple[bool, str]:
        if not verse_keys:
            return False, "EDITORIAL_RANGE_INVALID"
        parsed: list[tuple[int, int]] = []
        for key in verse_keys:
            if not isinstance(key, str) or ":" not in key:
                return False, "EDITORIAL_RANGE_INVALID"
            try:
                surah, ayah = (int(value) for value in key.split(":", 1))
            except ValueError:
                return False, "EDITORIAL_RANGE_INVALID"
            if surah not in STANDARD_SURAH_AYAH_COUNTS or not 1 <= ayah <= STANDARD_SURAH_AYAH_COUNTS[surah]:
                return False, "EDITORIAL_RANGE_INVALID"
            parsed.append((surah, ayah))
        if any(surah != parsed[0][0] or ayah != parsed[0][1] + index for index, (surah, ayah) in enumerate(parsed)):
            return False, "EDITORIAL_RANGE_INVALID"
        if self.passage_for(parsed[0][0], parsed[0][1], parsed[-1][1]) is None:
            return False, "EDITORIAL_NOT_PRODUCTION_AUTHORIZED"
        return True, "EDITORIAL_ELIGIBLE"

    def require_range(self, surah_number: int, first_ayah: int, last_ayah: int) -> ProductionAuthorizedPassage:
        passage = self.passage_for(surah_number, first_ayah, last_ayah)
        if passage is None:
            raise ProductionAuthorizationError("EDITORIAL_NOT_PRODUCTION_AUTHORIZED")
        return passage

    def require_snapshot(self, snapshot: object, surah_number: int, first_ayah: int, last_ayah: int) -> None:
        if not isinstance(snapshot, Mapping):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_PROVENANCE_MISSING")
        passage = self.require_range(surah_number, first_ayah, last_ayah)
        expected = passage.snapshot()
        if snapshot.get("source_evidence") != expected["source_evidence"]:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_PROVENANCE_STALE")
        for field in (
            "policy_version", "surah_number", "first_ayah", "last_ayah",
            "authorization_fingerprint",
        ):
            if snapshot.get(field) != expected[field]:
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_PROVENANCE_STALE")
        for field in ("authorization_ids", "approval_bases", "candidate_ayah_keys"):
            actual = snapshot.get(field)
            if not isinstance(actual, Sequence) or isinstance(actual, (str, bytes)):
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_PROVENANCE_STALE")
            if tuple(str(item) for item in actual) != tuple(str(item) for item in expected[field]):
                raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_PROVENANCE_STALE")


def load_production_authorization_catalog(
    store: Any,
    *,
    policy_version: str,
) -> ProductionAuthorizationCatalog:
    """Fetch one exact authorization snapshot from the server-side RPC."""
    try:
        rows = store.get_production_authorized_passages(policy_version)
    except Exception as error:
        raise ProductionAuthorizationError(
            "PRODUCTION_AUTHORIZATION_UNAVAILABLE"
        ) from error
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_RESPONSE_INVALID")
    return ProductionAuthorizationCatalog.from_rpc_rows(policy_version, rows)
