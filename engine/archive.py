"""Pure, fail-closed monthly final-Reel archive planning primitives.

The actual Drive upload is intentionally separate from the planner. Archive
selection always comes from structured state, never B2 listing.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from engine.storage import RunRecord


class ArchiveError(Exception):
    pass


@dataclass(frozen=True)
class ArchivePlan:
    year: int
    month: int
    archive_name: str
    entries: tuple[RunRecord, ...]

    @property
    def month_key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def build_monthly_archive_plan(year: int, month: int, records: Iterable[RunRecord]) -> ArchivePlan:
    if year < 2000 or year > 9999 or month < 1 or month > 12:
        raise ArchiveError("Archive year/month is invalid.")
    selected = tuple(
        record for record in records
        if record.run_kind in ("scheduled", "manual_production")
        and record.status == "SUCCESS"
        and record.final_safety_passed is True
        and bool(record.media_key)
    )
    duplicate_ids = [record.run_id for record in selected]
    if len(duplicate_ids) != len(set(duplicate_ids)):
        raise ArchiveError("Archive selection contains duplicate run IDs.")
    return ArchivePlan(year, month, f"quran-reels-{year:04d}-{month:02d}.zip", selected)


def archive_manifest(plan: ArchivePlan, files: Sequence[dict[str, Any]], archive_sha256: str, created_at: str) -> dict[str, Any]:
    if len(files) != len(plan.entries):
        raise ArchiveError("Archive manifest file count does not match selected runs.")
    return {
        "schema_version": 1,
        "archive_month": plan.month_key,
        "archive_name": plan.archive_name,
        "reel_count": len(plan.entries),
        "run_ids": [record.run_id for record in plan.entries],
        "files": list(files),
        "archive_sha256": archive_sha256,
        "created_at": created_at,
    }


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def zip_member_name(plan: ArchivePlan, run_id: str) -> str:
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ArchiveError("Unsafe run ID for archive member.")
    return f"{plan.month_key}/{run_id}.mp4"
