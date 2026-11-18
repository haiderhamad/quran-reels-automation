"""Deterministic, positive whole-ayah editorial eligibility for production Reels.

The catalog grants editorial eligibility only; canonical text, translation,
source provenance, timing, visual and publication checks remain independent.
Adding material requires reviewing each whole ayah and its surrounding context.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from engine.quran import STANDARD_SURAH_AYAH_COUNTS

if TYPE_CHECKING:
    from engine.production_authorization import ProductionAuthorizationCatalog

CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'reel_theme_catalog.json'
THEME_POLICY_VERSION = 'calm-inviting-v1'


class EditorialPolicyError(ValueError):
    """An exact production range has no current positive eligibility."""


@dataclass(frozen=True)
class PolicyVerdict:
    eligible: bool
    reason_code: str
    policy_version: str = THEME_POLICY_VERSION


def _valid_range(surah: object, first: object, last: object) -> bool:
    return (all(type(v) is int for v in (surah, first, last))
            and surah in STANDARD_SURAH_AYAH_COUNTS
            and 1 <= first <= last <= STANDARD_SURAH_AYAH_COUNTS[surah])


@lru_cache(maxsize=1)
def _load_catalog() -> tuple[frozenset[str], tuple[tuple[str, frozenset[str]], ...]]:
    """Load the versioned catalog strictly; malformed/missing data fails closed."""
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
        if (data.get('schema_version') != 1
                or data.get('policy_version') != THEME_POLICY_VERSION
                or data.get('content_profile') != 'calm_inviting'
                or not isinstance(data.get('ranges'), list)):
            raise ValueError('invalid catalog metadata')
        approved: set[str] = set()
        for entry in data['ranges']:
            surah, first, last = entry['surah'], entry['first_ayah'], entry['last_ayah']
            if not _valid_range(surah, first, last):
                raise ValueError('invalid catalog range')
            if any(not isinstance(entry.get(k), str) or not entry[k].strip()
                   for k in ('theme', 'review_note', 'source_url')):
                raise ValueError('missing review metadata')
            keys = {f'{surah}:{ayah}' for ayah in range(first, last + 1)}
            if keys & approved:
                raise ValueError('duplicate catalog eligibility')
            approved.update(keys)
        requirements = data['context_requirements']
        if not isinstance(requirements, list):
            raise ValueError('invalid context requirements')
        contexts = []
        triggers = set()
        for requirement in requirements:
            key, anchors = requirement['required_when_contains'], requirement['require_any_of']
            if (key not in approved or key in triggers or not isinstance(anchors, list)
                    or not anchors or any(a not in approved or a == key
                        or a.split(':')[0] != key.split(':')[0] for a in anchors)
                    or not isinstance(requirement.get('review_note'), str)
                    or not requirement['review_note'].strip()):
                raise ValueError('invalid context eligibility')
            triggers.add(key)
            contexts.append((key, frozenset(anchors)))
        return frozenset(approved), tuple(contexts)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise EditorialPolicyError('THEME_CATALOG_INVALID') from exc


def eligible_verse_keys() -> frozenset[str]:
    """Approved identities; exact ranges must also satisfy their positive context."""
    return _load_catalog()[0]


eligible_verse_keys.cache_clear = _load_catalog.cache_clear


def evaluate_range(
    surah: int,
    first_ayah: int,
    last_ayah: int,
    *,
    production_authorization: Optional["ProductionAuthorizationCatalog"] = None,
) -> PolicyVerdict:
    if not _valid_range(surah, first_ayah, last_ayah):
        return PolicyVerdict(False, 'EDITORIAL_RANGE_INVALID')
    return evaluate_verse_keys(
        (f'{surah}:{a}' for a in range(first_ayah, last_ayah + 1)),
        production_authorization=production_authorization,
    )


def evaluate_verse_keys(
    verse_keys: Iterable[str],
    *,
    production_authorization: Optional["ProductionAuthorizationCatalog"] = None,
) -> PolicyVerdict:
    """Validate every key plus exact order, one surah and consecutive completeness."""
    if isinstance(verse_keys, (str, bytes)):
        return PolicyVerdict(False, 'EDITORIAL_RANGE_INVALID')
    try:
        keys = tuple(verse_keys)
        parsed = []
        for key in keys:
            if not isinstance(key, str) or not re.fullmatch(r'[1-9]\d*:[1-9]\d*', key):
                return PolicyVerdict(False, 'EDITORIAL_RANGE_INVALID')
            s, a = map(int, key.split(':'))
            if not _valid_range(s, a, a):
                return PolicyVerdict(False, 'EDITORIAL_RANGE_INVALID')
            parsed.append((s, a))
        if (not parsed or any(s != parsed[0][0] or a != parsed[0][1] + i
                              for i, (s, a) in enumerate(parsed))):
            return PolicyVerdict(False, 'EDITORIAL_RANGE_INVALID')
    except TypeError:
        return PolicyVerdict(False, 'EDITORIAL_RANGE_INVALID')
    if production_authorization is not None:
        eligible, reason = production_authorization.evaluate_keys(keys)
        return PolicyVerdict(
            eligible,
            reason,
            policy_version=production_authorization.policy_version,
        )
    approved = eligible_verse_keys()
    if any(k not in approved for k in keys):
        return PolicyVerdict(False, 'EDITORIAL_INELIGIBLE')
    selected = set(keys)
    if any(key in selected and not (anchors & selected)
           for key, anchors in _load_catalog()[1]):
        return PolicyVerdict(False, 'EDITORIAL_CONTEXT_REQUIRED')
    return PolicyVerdict(True, 'EDITORIAL_ELIGIBLE')


def require_candidate_eligible(
    candidate: object,
    *,
    production_authorization: Optional["ProductionAuthorizationCatalog"] = None,
) -> None:
    try:
        keys = [f'{r.surah}:{r.ayah}' for r in candidate.records]
    except (AttributeError, TypeError) as exc:
        raise EditorialPolicyError('EDITORIAL_RANGE_INVALID') from exc
    verdict = evaluate_verse_keys(
        keys,
        production_authorization=production_authorization,
    )
    if not verdict.eligible:
        raise EditorialPolicyError(verdict.reason_code)
