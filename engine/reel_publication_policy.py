"""Source-bound production eligibility checks shared by publishing adapters."""
from collections.abc import Mapping, Sequence
from typing import Any
from functools import lru_cache

from engine.alignments import normalized_sha256
from engine.reel_theme_policy import EditorialPolicyError, evaluate_range, evaluate_verse_keys
from engine.translation import EXPECTED_TRANSLATION_VERSIONS, SUPPORTED_TRANSLATION_SOURCES
from engine.production_authorization import (
    ProductionAuthorizationCatalog,
    ProductionAuthorizationError,
    load_production_authorization_catalog,
)

TRANSLATION_HASH_FIELDS = {"en": "english_source_hash", "de": "german_source_hash"}


@lru_cache(maxsize=1)
def _trusted_quran():
    from engine.quran import fetch_quran_data, validate_quran_source_for_run
    quran = fetch_quran_data(qf_env="production")
    validate_quran_source_for_run("scheduled", quran)
    return quran


@lru_cache(maxsize=2)
def _trusted_translation(language: str):
    from engine.translation import fetch_translation_data_for_language
    return fetch_translation_data_for_language(language)


def _trusted_source_hashes(surah: int, ayah: int, language: str) -> tuple[str, str]:
    """Authenticate legacy source bytes and alternate pinned translations."""
    return (
        normalized_sha256(_trusted_quran().get_ayah(surah, ayah).arabic_text),
        normalized_sha256(_trusted_translation(language).get_ayah(surah, ayah).translation_text),
    )


def _resolve_production_authorization(
    record: Any,
    production_authorization: ProductionAuthorizationCatalog | None,
) -> ProductionAuthorizationCatalog | None:
    """Load the live catalog for direct production transport entry points."""
    if production_authorization is not None:
        return production_authorization
    try:
        from engine.supabase_state import SupabaseConfig, SupabaseStateStore

        return load_production_authorization_catalog(
            SupabaseStateStore(SupabaseConfig.from_env()),
            policy_version="calm-inviting-v1",
        )
    except Exception as error:
        raise EditorialPolicyError("PRODUCTION_AUTHORIZATION_UNAVAILABLE") from error


def require_record_eligible(
    record: Any,
    *,
    production_authorization: ProductionAuthorizationCatalog | None = None,
) -> None:
    """Validate exact source evidence and current production authorization."""
    if record is None:
        raise EditorialPolicyError("THEME_POLICY_REJECTED: missing production evidence")
    authorization = _resolve_production_authorization(record, production_authorization)
    verdict = evaluate_range(
        getattr(record, "surah_number", None),
        getattr(record, "first_ayah", None),
        getattr(record, "last_ayah", None),
        production_authorization=authorization,
    )
    if not verdict.eligible:
        raise EditorialPolicyError(verdict.reason_code)
    expected = tuple(f"{record.surah_number}:{n}" for n in range(record.first_ayah, record.last_ayah + 1))
    provenance = getattr(record, "provenance", None)
    if not isinstance(provenance, Mapping):
        raise EditorialPolicyError("THEME_POLICY_REJECTED: missing source provenance")
    if authorization is not None:
        try:
            authorization.require_snapshot(
                provenance.get("production_authorization"),
                int(record.surah_number),
                int(record.first_ayah),
                int(record.last_ayah),
            )
        except (ProductionAuthorizationError, TypeError, ValueError) as error:
            raise EditorialPolicyError(str(error)) from error
    groups = []
    for field in ("caption_ayahs", "subtitle_ayahs"):
        rows = provenance.get(field)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            raise EditorialPolicyError("THEME_POLICY_REJECTED: missing final range evidence")
        if any(not isinstance(row, Mapping) for row in rows):
            raise EditorialPolicyError("THEME_POLICY_REJECTED: malformed final range evidence")
        keys = tuple(row.get("verse_key") for row in rows)
        if keys != expected or not evaluate_verse_keys(
            keys,
            production_authorization=authorization,
        ).eligible:
            raise EditorialPolicyError("THEME_POLICY_REJECTED: final range mismatch")
        groups.append(rows)
    configured_languages = getattr(record, "translation_languages", None)
    if configured_languages is None:
        sources = provenance.get("translation_sources")
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)) and sources:
            configured_languages = tuple(source.get("language") if isinstance(source, Mapping) else None for source in sources)
        else:
            translations = groups[0][0].get("translations")
            configured_languages = tuple(translations) if isinstance(translations, Mapping) else ()
    if (not isinstance(configured_languages, Sequence) or isinstance(configured_languages, (str, bytes))
            or not configured_languages or any(not isinstance(language, str) or language not in TRANSLATION_HASH_FIELDS for language in configured_languages)
            or len(set(configured_languages)) != len(configured_languages)):
        raise EditorialPolicyError("THEME_POLICY_REJECTED: invalid translation evidence")
    for caption, subtitle in zip(*groups):
        arabic = caption.get("arabic_text")
        if not isinstance(arabic, str) or not arabic.strip():
            raise EditorialPolicyError("THEME_POLICY_REJECTED: missing source text")
        source_hash = normalized_sha256(arabic)
        if caption.get("arabic_normalized_sha256") != source_hash or subtitle.get("arabic_source_hash") != source_hash:
            raise EditorialPolicyError("THEME_POLICY_REJECTED: final source mismatch")
        translations = caption.get("translations")
        if not isinstance(translations, Mapping) or set(translations) != set(configured_languages):
            raise EditorialPolicyError("THEME_POLICY_REJECTED: translation language mismatch")
        for language in configured_languages:
            translation = translations[language]
            if not isinstance(translation, Mapping) or not isinstance(translation.get("text"), str) or not translation["text"].strip():
                raise EditorialPolicyError("THEME_POLICY_REJECTED: missing caption source")
            translation_hash = normalized_sha256(translation["text"])
            source_key = SUPPORTED_TRANSLATION_SOURCES[language]
            if (translation.get("normalized_sha256") != translation_hash
                    or subtitle.get(TRANSLATION_HASH_FIELDS[language]) != translation_hash
                    or translation.get("translation_key") != source_key
                    or translation.get("translation_version") != EXPECTED_TRANSLATION_VERSIONS[source_key]):
                raise EditorialPolicyError("THEME_POLICY_REJECTED: final translation mismatch")
            evidence = authorization.require_range(
                record.surah_number, record.first_ayah, record.last_ayah,
            ).source_evidence.get(caption["verse_key"]) if authorization is not None else None
            if evidence and language == evidence["translation_language"]:
                trusted_hashes = (evidence["source_hash"], evidence["translation_hash"])
            else:
                try:
                    trusted_hashes = _trusted_source_hashes(
                        record.surah_number, int(caption["verse_key"].split(":")[1]), language,
                    )
                except Exception as error:
                    raise EditorialPolicyError("PRODUCTION_AUTHORIZATION_SOURCE_UNAVAILABLE") from error
            if (source_hash, translation_hash) != trusted_hashes:
                raise EditorialPolicyError("PRODUCTION_AUTHORIZATION_SOURCE_MISMATCH")


def require_publication_request(
    record: Any,
    caption: str,
    *,
    backend: str,
    production_authorization: ProductionAuthorizationCatalog | None = None,
) -> None:
    """Prevent adapter callers from substituting a caption after validation."""
    if getattr(record, "run_kind", None) not in {"scheduled", "manual_production"}:
        raise EditorialPolicyError("THEME_POLICY_REJECTED: production run kind required")
    require_record_eligible(
        record,
        production_authorization=production_authorization,
    )
    if backend == "buffer":
        from engine.buffer_publisher import build_buffer_facebook_caption
        expected = build_buffer_facebook_caption(record)
    else:
        from engine.publisher import build_reel_caption_from_record
        expected = build_reel_caption_from_record(record)
    if caption != expected:
        raise EditorialPolicyError("THEME_POLICY_REJECTED: final caption mismatch")
