"""Select complete ayah ranges from caller-supplied authentic timing data."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from numbers import Real
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from engine.config import (
    DurationConfig,
    VerseSelectionLimits,
    choose_random_target_duration,
)


from engine.reel_theme_policy import eligible_verse_keys, evaluate_verse_keys, require_candidate_eligible

if TYPE_CHECKING:
    from engine.production_authorization import ProductionAuthorizationCatalog


SIMILAR_DURATION_TOLERANCE_SECONDS = 1.0


class VerseSelectionError(ValueError):
    """Raised when verse selection cannot proceed safely."""


@dataclass(frozen=True)
class AyahTiming:
    """One complete ayah with authentic recitation boundaries in seconds."""

    surah: int
    ayah: int
    start_time: float
    end_time: float
    arabic_text: str
    english_text: str


@dataclass(frozen=True)
class VerseRange:
    """A previously used inclusive ayah range."""

    surah: int
    first_ayah: int
    last_ayah: int


@dataclass(frozen=True)
class VerseCandidate:
    """A selected complete consecutive range and its authentic duration."""

    records: tuple[AyahTiming, ...]
    duration_seconds: float
    target_duration_seconds: float

    @property
    def surah(self) -> int:
        return self.records[0].surah

    @property
    def first_ayah(self) -> int:
        return self.records[0].ayah

    @property
    def last_ayah(self) -> int:
        return self.records[-1].ayah


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_valid_timing_record(record: object) -> bool:
    if not isinstance(record, AyahTiming):
        return False
    if not _is_positive_integer(record.surah) or record.surah > 114:
        return False
    if not _is_positive_integer(record.ayah):
        return False
    if not _is_finite_number(record.start_time):
        return False
    if not _is_finite_number(record.end_time):
        return False
    if record.start_time < 0 or record.end_time <= record.start_time:
        return False
    if not isinstance(record.arabic_text, str) or not record.arabic_text.strip():
        return False
    if not isinstance(record.english_text, str) or not record.english_text.strip():
        return False
    return True


def _validated_recent_ranges(
    recent_ranges: Iterable[VerseRange | tuple[int, int, int]],
) -> set[tuple[int, int, int]]:
    validated = set()
    for recent_range in recent_ranges:
        if isinstance(recent_range, VerseRange):
            surah, first_ayah, last_ayah = (
                recent_range.surah,
                recent_range.first_ayah,
                recent_range.last_ayah,
            )
        elif isinstance(recent_range, tuple) and len(recent_range) == 3:
            surah, first_ayah, last_ayah = recent_range
        else:
            raise VerseSelectionError("Recent verse history contains a malformed range.")

        if (
            not _is_positive_integer(surah)
            or surah > 114
            or not _is_positive_integer(first_ayah)
            or not _is_positive_integer(last_ayah)
            or first_ayah > last_ayah
        ):
            raise VerseSelectionError("Recent verse history contains a malformed range.")
        validated.add(
            (
                surah,
                first_ayah,
                last_ayah,
            )
        )
    return validated


def _valid_unique_records(records: Sequence[AyahTiming]) -> list[AyahTiming]:
    identities = [
        (record.surah, record.ayah)
        for record in records
        if isinstance(record, AyahTiming)
        and _is_positive_integer(record.surah)
        and _is_positive_integer(record.ayah)
    ]
    identity_counts = Counter(identities)
    return [
        record
        for record in records
        if _is_valid_timing_record(record)
        and identity_counts[(record.surah, record.ayah)] == 1
    ]


def _candidate_windows(
    records: Sequence[AyahTiming],
    duration_config: DurationConfig,
    limits: VerseSelectionLimits,
    recent_ranges: set[tuple[int, int, int]],
    target_duration: float,
    covered_ayahs: Optional[set[str]] = None,
    allow_coverage_exceptions: bool = False,
    production_authorization: Optional["ProductionAuthorizationCatalog"] = None,
) -> list[VerseCandidate]:
    covered = covered_ayahs if covered_ayahs is not None else set()
    records_by_surah: dict[int, list[AyahTiming]] = defaultdict(list)
    approved = (
        production_authorization.authorized_verse_keys
        if production_authorization is not None
        else eligible_verse_keys()
    )
    for record in _valid_unique_records(records):
        if f"{record.surah}:{record.ayah}" not in approved:
            continue
        # Strict non-repetition inside cycle: exclude already-covered ayahs
        if f"{record.surah}:{record.ayah}" in covered:
            continue
        records_by_surah[record.surah].append(record)

    candidates: list[VerseCandidate] = []
    for surah_records in records_by_surah.values():
        surah_records.sort(key=lambda record: record.ayah)

        # Segment into strictly contiguous runs of uncovered ayahs
        blocks: list[list[AyahTiming]] = []
        current_block: list[AyahTiming] = []
        for record in surah_records:
            if not current_block:
                current_block.append(record)
            else:
                prev = current_block[-1]
                if record.ayah == prev.ayah + 1 and record.start_time >= prev.end_time:
                    current_block.append(record)
                else:
                    blocks.append(current_block)
                    current_block = [record]
        if current_block:
            blocks.append(current_block)

        for block in blocks:
            block_candidates: list[VerseCandidate] = []
            for start_index, first_record in enumerate(block):
                arabic_characters = 0
                english_characters = 0
                previous_record: Optional[AyahTiming] = None

                for end_index in range(start_index, len(block)):
                    record = block[end_index]
                    if previous_record is not None:
                        if record.ayah != previous_record.ayah + 1:
                            break
                        if record.start_time < previous_record.end_time:
                            break

                    ayah_count = end_index - start_index + 1
                    if limits.maximum_ayahs is not None and ayah_count > limits.maximum_ayahs:
                        break

                    arabic_characters += len(record.arabic_text.strip())
                    english_characters += len(record.english_text.strip())
                    if arabic_characters > limits.maximum_arabic_characters:
                        break
                    if english_characters > limits.maximum_english_characters:
                        break

                    duration = float(record.end_time) - float(first_record.start_time)
                    if duration > duration_config.maximum_duration:
                        break

                    if duration >= duration_config.minimum_duration:
                        overlaps_recent_range = any(
                            recent_surah == first_record.surah
                            and first_record.ayah <= recent_last_ayah
                            and record.ayah >= recent_first_ayah
                            for (
                                recent_surah,
                                recent_first_ayah,
                                recent_last_ayah,
                            ) in recent_ranges
                        )
                        if not overlaps_recent_range:
                            cand = VerseCandidate(
                                records=tuple(block[start_index:end_index + 1]),
                                duration_seconds=duration,
                                target_duration_seconds=target_duration,
                            )
                            if evaluate_verse_keys(
                                (f"{r.surah}:{r.ayah}" for r in cand.records),
                                production_authorization=production_authorization,
                            ).eligible:
                                block_candidates.append(cand)

                    previous_record = record

            if allow_coverage_exceptions:
                # 1. Approved-corpus long ayah duration exception:
                # If an individual uncovered ayah by itself exceeds max_duration, allow it as a single-ayah Reel.
                for record in block:
                    duration = float(record.end_time) - float(record.start_time)
                    if duration > duration_config.maximum_duration:
                        overlaps_recent_range = any(
                            recent_surah == record.surah
                            and record.ayah <= recent_last_ayah
                            and record.ayah >= recent_first_ayah
                            for (
                                recent_surah,
                                recent_first_ayah,
                                recent_last_ayah,
                            ) in recent_ranges
                        )
                        if not overlaps_recent_range:
                            cand = VerseCandidate(
                                records=(record,),
                                duration_seconds=duration,
                                target_duration_seconds=target_duration,
                            )
                            if evaluate_verse_keys(
                                (f"{r.surah}:{r.ayah}" for r in cand.records),
                                production_authorization=production_authorization,
                            ).eligible:
                                block_candidates.append(cand)

                # 2. Approved-corpus short remaining range duration exception:
                # If no standard candidate exists in this contiguous block (e.g. block duration < min_duration),
                # allow the entire contiguous remaining block as a completion candidate so no ayah is stranded.
                if not block_candidates and block:
                    total_arabic = sum(len(r.arabic_text.strip()) for r in block)
                    total_english = sum(len(r.english_text.strip()) for r in block)
                    if (
                        total_arabic <= limits.maximum_arabic_characters
                        and total_english <= limits.maximum_english_characters
                        and (limits.maximum_ayahs is None or len(block) <= limits.maximum_ayahs)
                    ):
                        first_rec = block[0]
                        last_rec = block[-1]
                        overlaps_recent = any(
                            recent_surah == first_rec.surah
                            and first_rec.ayah <= recent_last_ayah
                            and last_rec.ayah >= recent_first_ayah
                            for (
                                recent_surah,
                                recent_first_ayah,
                                recent_last_ayah,
                            ) in recent_ranges
                        )
                        if not overlaps_recent:
                            block_dur = float(last_rec.end_time) - float(first_rec.start_time)
                            cand = VerseCandidate(
                                records=tuple(block),
                                duration_seconds=block_dur,
                                target_duration_seconds=target_duration,
                            )
                            if evaluate_verse_keys(
                                (f"{r.surah}:{r.ayah}" for r in cand.records),
                                production_authorization=production_authorization,
                            ).eligible:
                                block_candidates.append(cand)

            candidates.extend(block_candidates)

    return candidates


def _select_exact_owner_reviewed_passage(
    records: Sequence[AyahTiming],
    ayah_key: str,
    reviewed_ayah_keys: tuple[str, ...],
    duration_config: DurationConfig,
    limits: VerseSelectionLimits,
    reviewed_registry: tuple[tuple[str, ...], ...],
) -> VerseCandidate:
    """Require every authentic ayah of one explicitly reviewed contextual span."""
    from engine.calm_reels_cycle import FreshCycleError, require_owner_ayah_source
    from engine.owner_reviewed_passages import require_reviewed_owner_passage

    try:
        require_reviewed_owner_passage(reviewed_ayah_keys, reviewed_registry)
    except ValueError as error:
        raise VerseSelectionError("OWNER_V1_PASSAGE_NOT_REVIEWED") from error
    if reviewed_ayah_keys[0] != ayah_key:
        raise VerseSelectionError("OWNER_V1_EXACT_TARGET_MISMATCH")
    if limits.maximum_ayahs is not None and len(reviewed_ayah_keys) > limits.maximum_ayahs:
        raise VerseSelectionError("OWNER_V1_TEXT_LIMIT_EXCEEDED")

    selected: list[AyahTiming] = []
    for key in reviewed_ayah_keys:
        matching = [record for record in records
                    if isinstance(record, AyahTiming)
                    and f"{record.surah}:{record.ayah}" == key]
        if len(matching) != 1 or not _is_valid_timing_record(matching[0]):
            raise VerseSelectionError("OWNER_V1_EXACT_TIMING_UNAVAILABLE")
        member = matching[0]
        if selected and member.start_time < selected[-1].end_time:
            raise VerseSelectionError("OWNER_V1_OVERLAPPING_TIMING")
        try:
            require_owner_ayah_source(key, member.arabic_text)
        except FreshCycleError as error:
            raise VerseSelectionError(str(error)) from error
        selected.append(member)
    span = selected[-1].end_time - selected[0].start_time
    if not duration_config.minimum_duration <= span <= duration_config.maximum_duration:
        raise VerseSelectionError("OWNER_V1_EXACT_DURATION_OUT_OF_RANGE")
    return VerseCandidate(tuple(selected), span, span)


def select_owner_v1_ayah_candidate(
    records: Sequence[AyahTiming],
    ayah_key: str,
    *,
    duration_config: DurationConfig,
    limits: VerseSelectionLimits,
    pending_successor: str | None = None,
    reviewed_ayah_keys: tuple[str, ...] = (),
    reviewed_registry: tuple[tuple[str, ...], ...] | None = None,
) -> VerseCandidate:
    """Select exactly one approved passage or the legacy first-pending singleton/pair.

    Never pad or loop the recitation. Missing or invalid source evidence fails closed.
    Exact owner selections use mandatory subtitle preflight for display eligibility;
    aggregate character budgets belong to the legacy candidate search, not panels.
    """
    from engine.calm_reels_cycle import FreshCycleError, require_owner_ayah_source
    if reviewed_ayah_keys:
        if pending_successor is not None and pending_successor != reviewed_ayah_keys[1]:
            raise VerseSelectionError("OWNER_V1_INVALID_SUCCESSOR")
        if reviewed_registry is None:
            from engine.owner_reviewed_passages import REVIEWED_OWNER_PASSAGES
            reviewed_registry = REVIEWED_OWNER_PASSAGES
        return _select_exact_owner_reviewed_passage(
            records, ayah_key, reviewed_ayah_keys, duration_config, limits,
            reviewed_registry,
        )

    if not isinstance(ayah_key, str):
        raise VerseSelectionError("OWNER_V1_TARGET_INVALID")
    selected = tuple(
        record for record in records
        if isinstance(record, AyahTiming)
        and f"{record.surah}:{record.ayah}" == ayah_key
    )
    if len(selected) != 1 or not _is_valid_timing_record(selected[0]):
        raise VerseSelectionError("OWNER_V1_EXACT_TIMING_UNAVAILABLE")
    record = selected[0]
    try:
        require_owner_ayah_source(ayah_key, record.arabic_text)
    except FreshCycleError as error:
        raise VerseSelectionError(str(error)) from error
    duration = float(record.end_time) - float(record.start_time)
    if duration > duration_config.maximum_duration:
        raise VerseSelectionError("OWNER_V1_EXACT_DURATION_OUT_OF_RANGE")
    if limits.maximum_ayahs is not None and limits.maximum_ayahs < 1:
        raise VerseSelectionError("OWNER_V1_TEXT_LIMIT_EXCEEDED")
    if pending_successor is not None and pending_successor != f'{record.surah}:{record.ayah + 1}':
        raise VerseSelectionError('OWNER_V1_INVALID_SUCCESSOR')
    if (duration < duration_config.minimum_duration and pending_successor is not None
            and (limits.maximum_ayahs is None or limits.maximum_ayahs >= 2)):
        neighbours = [r for r in records if isinstance(r, AyahTiming)
                      and (r.surah, r.ayah) == (record.surah, record.ayah + 1)]
        if len(neighbours) != 1 or not _is_valid_timing_record(neighbours[0]):
            raise VerseSelectionError('OWNER_V1_EXACT_TIMING_UNAVAILABLE')
        neighbour = neighbours[0]
        if neighbour.start_time < record.end_time:
            raise VerseSelectionError('OWNER_V1_OVERLAPPING_TIMING')
        try:
            require_owner_ayah_source(pending_successor, neighbour.arabic_text)
        except FreshCycleError as error:
            raise VerseSelectionError(str(error)) from error
        span = neighbour.end_time - record.start_time
        if duration_config.minimum_duration <= span <= duration_config.maximum_duration:
            return VerseCandidate((record, neighbour), span, span)
    if duration < duration_config.minimum_duration:
        # Only the independently audited Fatiha 1:1 excerpt (read-92,
        # 0.300–3.240 s) has a historical exception to the normal minimum.
        # The production generator additionally verifies the reciter/read.
        if (ayah_key != "1:1"
                or record.start_time != 0.300
                or record.end_time != 3.240):
            raise VerseSelectionError("OWNER_V1_EXACT_DURATION_BELOW_MINIMUM")
    return VerseCandidate((record,), duration, duration)


def select_verse_candidate(
    records: Sequence[AyahTiming],
    duration_config: Optional[DurationConfig] = None,
    recent_ranges: Iterable[VerseRange | tuple[int, int, int]] = (),
    limits: Optional[VerseSelectionLimits] = None,
    rng: Optional[random.Random] = None,
    target_duration: Optional[float] = None,
    covered_ayahs: Optional[Iterable[str]] = None,
    allow_coverage_exceptions: bool = True,
    production_authorization: Optional["ProductionAuthorizationCatalog"] = None,
) -> VerseCandidate:
    """Select a complete authentic ayah range or fail without relaxing rules."""
    config = duration_config if duration_config is not None else DurationConfig()
    selection_limits = limits if limits is not None else VerseSelectionLimits()
    generator = rng if rng is not None else random.Random()
    covered_set = set(covered_ayahs) if covered_ayahs is not None else None

    if target_duration is None:
        target = choose_random_target_duration(config, generator)
    elif (
        not _is_finite_number(target_duration)
        or target_duration < config.minimum_duration
        or target_duration > config.maximum_duration
    ):
        raise VerseSelectionError(
            "target_duration must be finite and inside the configured range."
        )
    else:
        target = float(target_duration)

    # First attempt: standard candidates without exceptions
    candidates = _candidate_windows(
        records=records,
        duration_config=config,
        limits=selection_limits,
        recent_ranges=_validated_recent_ranges(recent_ranges),
        target_duration=target,
        covered_ayahs=covered_set,
        allow_coverage_exceptions=False,
        production_authorization=production_authorization,
    )

    # If no standard candidates exist and coverage exceptions are enabled with covered_set
    if not candidates and allow_coverage_exceptions and covered_set is not None:
        candidates = _candidate_windows(
            records=records,
            duration_config=config,
            limits=selection_limits,
            recent_ranges=_validated_recent_ranges(recent_ranges),
            target_duration=target,
            covered_ayahs=covered_set,
            allow_coverage_exceptions=True,
            production_authorization=production_authorization,
        )

    if not candidates:
        raise VerseSelectionError(
            "NO_ELIGIBLE_VERSE_RANGE: No valid complete ayah candidate fits the duration, timing, "
            "history, coverage, and visual limits."
        )


    best_distance = min(
        abs(candidate.duration_seconds - target)
        for candidate in candidates
    )
    similarly_valid = [
        candidate
        for candidate in candidates
        if abs(candidate.duration_seconds - target)
        <= best_distance + SIMILAR_DURATION_TOLERANCE_SECONDS
    ]
    selected = generator.choice(similarly_valid)
    require_candidate_eligible(
        selected,
        production_authorization=production_authorization,
    )
    return selected
