"""V1 first-pending Facebook dispatch reservation; never dispatches media.

The caller MUST call this immediately before its one external Facebook send.
A successful result is a reservation, NOT a post, not a verified receipt and
not permission to retry after any uncertain provider outcome.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from engine.alignments import normalized_sha256
from engine.calm_reels_cycle import (
    CATALOG_ID, FreshCycleError, require_owner_ayah_source,
)
from engine.publisher import check_reciter_reuse_rights
from engine.storage import RunRecord
from engine.translation import EXPECTED_TRANSLATION_VERSIONS, SUPPORTED_TRANSLATION_SOURCES


class OwnerV1DispatchError(ValueError):
    """Stable fail-closed cause; never includes provider secrets or response data."""


def _require_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OwnerV1DispatchError(code)
    return value


def require_owner_v1_dispatch_record(record: RunRecord) -> tuple[int, str]:
    """Validate immutable, rendered source and publication provenance pre-claim.

    No obsolete topic-grant RPC. Authentic Quran/translation hashes must match
    independently fetched pinned sources. This alone never authorizes sending.
    """
    if not isinstance(record, RunRecord):
        raise OwnerV1DispatchError("OWNER_V1_RUN_RECORD_REQUIRED")
    if (record.run_kind not in ("scheduled", "manual_production")
        or record.status != "SUCCESS" or record.final_safety_passed is not True
        or not isinstance(record.media_key, str) or not record.media_key.strip()
        or record.facebook_status != "NOT_ATTEMPTED"
        or record.instagram_status != "NOT_ATTEMPTED"
        or record.facebook_media_id is not None
        or record.instagram_media_id is not None
        or record.buffer_post_id is not None):
        raise OwnerV1DispatchError("OWNER_V1_FINAL_SAFE_UNSENT_RUN_REQUIRED")
    provenance = record.provenance
    if not isinstance(provenance, Mapping):
        raise OwnerV1DispatchError("OWNER_V1_PROVENANCE_MISSING")
    owner = provenance.get("owner_v1")
    if not isinstance(owner, Mapping) or owner.get("catalog_id") != CATALOG_ID:
        raise OwnerV1DispatchError("OWNER_V1_OWNER_IDENTITY_MISMATCH")
    cycle = owner.get("cycle_number")
    ayah_key = owner.get("ayah_key")
    if (type(cycle) is not int or cycle < 1
        or not isinstance(ayah_key, str)
        or owner.get("editorial_selection_only") is not True
        or type(record.surah_number) is not int
        or type(record.first_ayah) is not int
        or type(record.last_ayah) is not int
        or record.last_ayah < record.first_ayah
        or ayah_key != f"{record.surah_number}:{record.first_ayah}"):
        raise OwnerV1DispatchError("OWNER_V1_EXACT_CYCLE_AYAH_MISMATCH")
    # A generated v1 selection cannot inherit fabricated publication approval.
    if (provenance.get("editorial_reconciliation_status") is not None
        or provenance.get("production_authorization") is not None
        or provenance.get("theme_policy_version") is not None):
        raise OwnerV1DispatchError("OWNER_V1_LEGACY_APPROVAL_NOT_ALLOWED")

    caption_rows = provenance.get("caption_ayahs")
    subtitle_rows = provenance.get("subtitle_ayahs")
    keys = [f'{record.surah_number}:{n}' for n in range(record.first_ayah, record.last_ayah + 1)]
    from engine.owner_reviewed_passages import require_contiguous_owner_passage
    # The historical hardcoded Fatiha range is not a production approval
    # authority. The state adapter and atomic SQL claim recheck the CURRENT
    # reviewed registry before any external Buffer request.
    if len(keys) > 2:
        # This validates the rendered range's structure, NOT approval. The
        # service-role-only registry is reread by the state adapter and checked
        # again in the atomic SQL RPC before the first external Buffer send.
        try:
            require_contiguous_owner_passage(tuple(keys))
        except ValueError as error:
            raise OwnerV1DispatchError('OWNER_V1_REVIEWED_RANGE_REQUIRED') from error
        from engine.owner_reviewed_passages import require_rendered_review_approval
        try:
            require_rendered_review_approval(owner, tuple(keys))
        except ValueError as error:
            raise OwnerV1DispatchError("OWNER_V1_REVIEW_APPROVAL_REQUIRED") from error
    if owner.get('ayah_keys', [ayah_key]) != keys:
        raise OwnerV1DispatchError('OWNER_V1_EXACT_CYCLE_AYAH_MISMATCH')
    hashes = owner.get('source_sha256_by_key', {ayah_key: owner.get('source_sha256')})
    if (not isinstance(hashes, Mapping) or set(hashes) != set(keys)
            or hashes[ayah_key] != owner.get('source_sha256')):
        raise OwnerV1DispatchError('OWNER_V1_FINAL_ARABIC_HASH_MISMATCH')
    if (not isinstance(caption_rows, (tuple, list)) or len(caption_rows) != len(keys)
            or not isinstance(subtitle_rows, (tuple, list)) or len(subtitle_rows) != len(keys)):
        raise OwnerV1DispatchError('OWNER_V1_FINAL_SOURCE_EVIDENCE_MISSING')
    for index, key in enumerate(keys):
        caption, subtitle = caption_rows[index], subtitle_rows[index]
        if not isinstance(caption, Mapping) or not isinstance(subtitle, Mapping):
            raise OwnerV1DispatchError('OWNER_V1_FINAL_SOURCE_EVIDENCE_MISSING')
        _validate_owner_member(record, key, hashes[key], caption, subtitle)
    return cycle, ayah_key


def _validate_owner_member(record: RunRecord, ayah_key: str, source_hash: str,
                           caption: Mapping, subtitle: Mapping) -> None:
    arabic = _require_text(caption.get("arabic_text"), "OWNER_V1_ARABIC_MISSING")
    if caption.get("verse_key") != ayah_key or subtitle.get("verse_key") != ayah_key:
        raise OwnerV1DispatchError("OWNER_V1_FINAL_AYAH_MISMATCH")
    try:
        verified_hash = require_owner_ayah_source(ayah_key, arabic)
    except FreshCycleError as error:
        raise OwnerV1DispatchError("OWNER_V1_FROZEN_SOURCE_MISMATCH") from error
    if (source_hash != verified_hash
        or caption.get("arabic_normalized_sha256") != normalized_sha256(arabic)
        or subtitle.get("arabic_source_hash") != normalized_sha256(arabic)):
        raise OwnerV1DispatchError("OWNER_V1_FINAL_ARABIC_HASH_MISMATCH")

    languages = record.translation_languages
    if (not isinstance(languages, (tuple, list)) or not languages
        or len(set(languages)) != len(languages)
        or any(lang not in ("en", "de") for lang in languages)):
        raise OwnerV1DispatchError("OWNER_V1_LANGUAGES_INVALID")
    translated = caption.get("translations")
    if not isinstance(translated, Mapping) or set(translated) != set(languages):
        raise OwnerV1DispatchError("OWNER_V1_FINAL_TRANSLATION_MISSING")
    # Pinned canonical source comparison is independent of local render data.
    from engine.reel_publication_policy import _trusted_source_hashes, TRANSLATION_HASH_FIELDS
    for language in languages:
        item = translated[language]
        if not isinstance(item, Mapping):
            raise OwnerV1DispatchError("OWNER_V1_FINAL_TRANSLATION_MISSING")
        original = _require_text(item.get("text"), "OWNER_V1_FINAL_TRANSLATION_MISSING")
        translation_hash = normalized_sha256(original)
        key = SUPPORTED_TRANSLATION_SOURCES[language]
        if (item.get("translation_key") != key
            or item.get("translation_version") != EXPECTED_TRANSLATION_VERSIONS[key]
            or item.get("normalized_sha256") != translation_hash
            or subtitle.get(TRANSLATION_HASH_FIELDS[language]) != translation_hash):
            raise OwnerV1DispatchError("OWNER_V1_FINAL_TRANSLATION_HASH_MISMATCH")
        try:
            source_hashes = _trusted_source_hashes(record.surah_number, int(ayah_key.split(':')[1]), language)
        except Exception as error:
            raise OwnerV1DispatchError("OWNER_V1_TRUSTED_SOURCE_UNAVAILABLE") from error
        if source_hashes != (normalized_sha256(arabic), translation_hash):
            raise OwnerV1DispatchError("OWNER_V1_TRUSTED_SOURCE_MISMATCH")


def claim_owner_v1_before_facebook_dispatch(
    record: RunRecord,
    *,
    store: Any,
    stored_config: Any,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Atomic first-pending reservation after final-safe checks, before send.

    A claim from the same run returned as ALREADY_CLAIMED must NOT dispatch
    again: the earlier provider outcome might be unknown but already visible.
    """
    cycle, ayah_key = require_owner_v1_dispatch_record(record)
    if (not isinstance(env, Mapping)
        or not check_reciter_reuse_rights(
            getattr(stored_config, "reciter_reuse_confirmed", False), env=env,
        )):
        raise OwnerV1DispatchError("OWNER_V1_PUBLICATION_RIGHTS_NOT_CONFIRMED")
    reviewed = record.last_ayah > record.first_ayah + 1
    claim_fn = getattr(store, (
        'claim_calm_reels_v1_reviewed_passage' if reviewed
        else 'claim_calm_reels_v1_next_ayah'), None)
    if not callable(claim_fn):
        raise OwnerV1DispatchError("OWNER_V1_ATOMIC_CLAIM_STORE_REQUIRED")
    try:
        if reviewed:
            from engine.owner_reviewed_passages import require_rendered_review_approval
            approval = require_rendered_review_approval(
                record.provenance["owner_v1"],
                tuple(f"{record.surah_number}:{ayah}" for ayah in range(record.first_ayah, record.last_ayah + 1)),
            )
        response = (claim_fn(
            cycle_number=cycle, first_ayah_key=ayah_key,
            last_ayah_key=f'{record.surah_number}:{record.last_ayah}',
            run_id=record.run_id,
            review_approval=approval,
        ) if reviewed else claim_fn(
            cycle_number=cycle, ayah_key=ayah_key, run_id=record.run_id,
        ))
    except Exception as error:
        raise OwnerV1DispatchError("OWNER_V1_CLAIM_UNKNOWN_RECONCILE_MANUALLY") from error
    if (not isinstance(response, Mapping)
        or response.get("state") not in {"CLAIMED", "ALREADY_CLAIMED"}
        or type(response.get("cycle_number")) is not int
        or response["cycle_number"] != cycle
        or response.get("ayah_key") != ayah_key
        or response.get('last_ayah_key', ayah_key) != f'{record.surah_number}:{record.last_ayah}'):
        raise OwnerV1DispatchError("OWNER_V1_CLAIM_RESULT_INVALID")
    if response["state"] != "CLAIMED":
        raise OwnerV1DispatchError("OWNER_V1_ALREADY_CLAIMED_NO_REDISPATCH")
    return {"state": "CLAIMED", "cycle_number": cycle, "ayah_key": ayah_key}
