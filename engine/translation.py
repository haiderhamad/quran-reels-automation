"""Verified multilingual translation sources from QuranEnc / Rowwad."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Union


DEFAULT_API_BASE_URL = "https://quranenc.com/api/v1"
DEFAULT_TRANSLATION_KEY = "english_rwwad"
GERMAN_TRANSLATION_KEY = "german_rwwad"
EXPECTED_LANGUAGE_ISO = "en"  # Backward-compatible default.
SUPPORTED_TRANSLATION_SOURCES = {
    "en": DEFAULT_TRANSLATION_KEY,
    "de": GERMAN_TRANSLATION_KEY,
}
EXPECTED_TRANSLATION_VERSIONS = {
    "english_rwwad": "1.0.19",
    "german_rwwad": "1.0.15",
}
EXPECTED_SURAH_COUNT = 114
EXPECTED_TOTAL_AYAH_COUNT = 6236
# urllib exposes one socket timeout, not independent connect/read values.  This
# therefore bounds both phases at 45 seconds; moving to another HTTP client
# solely to split the values would add an unnecessary second transport.
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_USER_AGENT = "QuranReelsAutomation/1.0"
TRANSLATION_FETCH_MAX_ATTEMPTS = 4
TRANSLATION_FETCH_BACKOFF_SECONDS = (0.0, 2.0, 5.0, 10.0)
TRANSLATION_CACHE_SCHEMA_VERSION = 1
DEFAULT_TRANSLATION_CACHE_DIR = ".cache/translations"
# Project-preserved original source identity, not a publisher-signed checksum.
# Keep raw historical references private; no newer text may inherit this pin.
PINNED_TRANSLATION_BODY_HASHES = {
    ("english_rwwad", "1.0.19"): "c3369ca614d376b6f5f81fd4273a74e7379d9c5fc8beccc99b1820206a796dd6",
}
ENGLISH_REFERENCE_PATH_ENV = "QURAN_REELS_ENGLISH_REFERENCE_PATH"
MAX_TRANSLATION_REFERENCE_BYTES = 16 * 1024 * 1024
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

LOGGER = logging.getLogger("quran_reels.translation")

# Standard Hafs ayah counts per Surah (1..114)
STANDARD_SURAH_AYAH_COUNTS: dict[int, int] = {
    1: 7, 2: 286, 3: 200, 4: 176, 5: 120, 6: 165, 7: 206, 8: 75, 9: 129, 10: 109,
    11: 123, 12: 111, 13: 43, 14: 52, 15: 99, 16: 128, 17: 111, 18: 110, 19: 98, 20: 135,
    21: 112, 22: 78, 23: 118, 24: 64, 25: 77, 26: 227, 27: 93, 28: 88, 29: 69, 30: 60,
    31: 34, 32: 30, 33: 73, 34: 54, 35: 45, 36: 83, 37: 182, 38: 88, 39: 75, 40: 85,
    41: 54, 42: 53, 43: 89, 44: 59, 45: 37, 46: 35, 47: 38, 48: 29, 49: 18, 50: 45,
    51: 60, 52: 49, 53: 62, 54: 55, 55: 78, 56: 96, 57: 29, 58: 22, 59: 24, 60: 13,
    61: 14, 62: 11, 63: 11, 64: 18, 65: 12, 66: 12, 67: 30, 68: 52, 69: 52, 70: 44,
    71: 28, 72: 28, 73: 20, 74: 56, 75: 40, 76: 31, 77: 50, 78: 40, 79: 46, 80: 42,
    81: 29, 82: 19, 83: 36, 84: 25, 85: 22, 86: 17, 87: 19, 88: 26, 89: 30, 90: 20,
    91: 15, 92: 21, 93: 11, 94: 8, 95: 8, 96: 19, 97: 5, 98: 8, 99: 8, 100: 11,
    101: 11, 102: 8, 103: 3, 104: 9, 105: 5, 106: 4, 107: 7, 108: 3, 109: 6, 110: 3,
    111: 5, 112: 4, 113: 5, 114: 6,
}


class TranslationSourceError(ValueError):
    """Raised when translation fetching, parsing, or validation fails."""


class TranslationTransientFetchError(TranslationSourceError):
    """A bounded-retry transport error while reading one QuranEnc resource."""

    def __init__(self, category: str, message: str, attempts: int) -> None:
        super().__init__(message)
        self.category = category
        self.attempts = attempts


class TranslationDatasetFetchExhaustedError(TranslationSourceError):
    """The approved full-dataset source could not be fetched after retries."""


@dataclass(frozen=True)
class TranslationAyah:
    """A single immutable ayah with exact source translation and footnotes."""

    surah_number: int
    ayah_number: int
    english_text: str
    footnotes: str

    @property
    def translation_text(self) -> str:
        """Language-neutral alias retained alongside the legacy field name."""
        return self.english_text


@dataclass(frozen=True)
class TranslationSurah:
    """A complete Surah containing all consecutive immutable translation ayahs."""

    number: int
    number_of_ayahs: int
    ayahs: tuple[TranslationAyah, ...]


@dataclass(frozen=True)
class TranslationSourceMetadata:
    """Source provenance and version metadata for retrieved translation data."""

    provider: str
    api_base_url: str
    translation_key: str
    language_iso_code: str
    version: str
    last_update: Union[int, str]
    title: str
    description: Optional[str]


@dataclass(frozen=True)
class TranslationData:
    """Immutable full-Quran translation dataset for one language/source."""

    source: TranslationSourceMetadata
    surahs: tuple[TranslationSurah, ...]

    def get_surah(self, surah_number: int) -> TranslationSurah:
        if (
            not isinstance(surah_number, int)
            or isinstance(surah_number, bool)
            or surah_number < 1
            or surah_number > len(self.surahs)
        ):
            raise TranslationSourceError(
                f"Surah number {surah_number} is out of range (1..{len(self.surahs)})."
            )
        return self.surahs[surah_number - 1]

    def get_ayah(self, surah_number: int, ayah_number: int) -> TranslationAyah:
        surah = self.get_surah(surah_number)
        if (
            not isinstance(ayah_number, int)
            or isinstance(ayah_number, bool)
            or ayah_number < 1
            or ayah_number > surah.number_of_ayahs
        ):
            raise TranslationSourceError(
                f"Ayah number {ayah_number} is out of range for Surah {surah_number} (1..{surah.number_of_ayahs})."
            )
        return surah.ayahs[ayah_number - 1]

    def get_ayah_range(
        self,
        surah_number: int,
        first_ayah: int,
        last_ayah: int,
    ) -> tuple[TranslationAyah, ...]:
        surah = self.get_surah(surah_number)
        if (
            not isinstance(first_ayah, int)
            or isinstance(first_ayah, bool)
            or not isinstance(last_ayah, int)
            or isinstance(last_ayah, bool)
        ):
            raise TranslationSourceError("Ayah numbers must be integers.")
        if first_ayah < 1 or last_ayah > surah.number_of_ayahs or first_ayah > last_ayah:
            raise TranslationSourceError(
                f"Invalid ayah range {first_ayah}..{last_ayah} for Surah {surah_number} (1..{surah.number_of_ayahs})."
            )
        return surah.ayahs[first_ayah - 1 : last_ayah]

    @property
    def total_surahs(self) -> int:
        return len(self.surahs)

    @property
    def total_ayahs(self) -> int:
        return sum(surah.number_of_ayahs for surah in self.surahs)


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def parse_translation_metadata(
    raw_payload: Union[str, bytes, dict],
    target_key: str = DEFAULT_TRANSLATION_KEY,
    api_base_url: str = DEFAULT_API_BASE_URL,
    expected_language_iso: str = EXPECTED_LANGUAGE_ISO,
) -> TranslationSourceMetadata:
    """Parse and validate translation list response and extract target translation metadata."""
    if isinstance(raw_payload, (str, bytes)):
        try:
            payload = json.loads(raw_payload)
        except Exception as err:
            raise TranslationSourceError(f"Malformed JSON in metadata payload: {err}") from err
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        raise TranslationSourceError("Metadata payload must be a JSON string, bytes, or dict.")

    if not isinstance(payload, dict):
        raise TranslationSourceError("Metadata payload top-level element must be an object.")

    translations_list = payload.get("translations")
    if not isinstance(translations_list, list):
        raise TranslationSourceError("Missing or invalid 'translations' list in metadata response.")

    matched_entry: Optional[dict] = None
    for entry in translations_list:
        if isinstance(entry, dict) and entry.get("key") == target_key:
            matched_entry = entry
            break

    if matched_entry is None:
        raise TranslationSourceError(
            f"Requested translation key '{target_key}' not found in official translation list."
        )

    lang_iso = matched_entry.get("language_iso_code")
    if lang_iso != expected_language_iso:
        raise TranslationSourceError(
            f"Unexpected language ISO code for '{target_key}': expected '{expected_language_iso}', got '{lang_iso}'."
        )

    version = matched_entry.get("version")
    if not isinstance(version, str) or not version.strip():
        raise TranslationSourceError(
            f"Missing or empty version in translation metadata for '{target_key}'."
        )

    title = matched_entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TranslationSourceError(
            f"Missing or empty title in translation metadata for '{target_key}'."
        )

    last_update = matched_entry.get("last_update")
    if last_update is None:
        raise TranslationSourceError(
            f"Missing 'last_update' timestamp in translation metadata for '{target_key}'."
        )

    description = matched_entry.get("description")
    if description is not None and not isinstance(description, str):
        raise TranslationSourceError("Translation description must be a string if provided.")

    return TranslationSourceMetadata(
        provider="QuranEnc",
        api_base_url=api_base_url,
        translation_key=target_key,
        language_iso_code=lang_iso,
        version=str(version),
        last_update=last_update,
        title=str(title),
        description=description,
    )


def parse_surah_translation_payload(
    raw_payload: Union[str, bytes, dict],
    expected_surah: int,
) -> TranslationSurah:
    """Parse and strictly validate a single Surah translation payload from QuranEnc."""
    if (
        not isinstance(expected_surah, int)
        or isinstance(expected_surah, bool)
        or expected_surah < 1
        or expected_surah > EXPECTED_SURAH_COUNT
    ):
        raise TranslationSourceError(
            f"Expected Surah number {expected_surah} is out of range (1..{EXPECTED_SURAH_COUNT})."
        )

    if isinstance(raw_payload, (str, bytes)):
        try:
            payload = json.loads(raw_payload)
        except Exception as err:
            raise TranslationSourceError(f"Malformed JSON in Surah payload: {err}") from err
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        raise TranslationSourceError("Surah payload must be a JSON string, bytes, or dict.")

    if not isinstance(payload, dict):
        raise TranslationSourceError("Surah payload top-level element must be an object.")

    result_list = payload.get("result")
    if not isinstance(result_list, list):
        raise TranslationSourceError(f"Missing or invalid 'result' list in Surah {expected_surah} payload.")

    expected_ayah_count = STANDARD_SURAH_AYAH_COUNTS[expected_surah]
    if len(result_list) != expected_ayah_count:
        raise TranslationSourceError(
            f"Surah {expected_surah} contains {len(result_list)} translation items, expected {expected_ayah_count}."
        )

    parsed_ayahs: list[TranslationAyah] = []
    for ayah_idx, item in enumerate(result_list):
        expected_ayah_num = ayah_idx + 1
        if not isinstance(item, dict):
            raise TranslationSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} entry must be an object."
            )

        sura_val = item.get("sura")
        try:
            parsed_sura = int(sura_val)
        except (TypeError, ValueError) as err:
            raise TranslationSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} has non-integer 'sura': {sura_val}"
            ) from err

        if parsed_sura != expected_surah:
            raise TranslationSourceError(
                f"Surah identity mismatch in payload: expected Surah {expected_surah}, got {parsed_sura}."
            )

        aya_val = item.get("aya")
        try:
            parsed_aya = int(aya_val)
        except (TypeError, ValueError) as err:
            raise TranslationSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} has non-integer 'aya': {aya_val}"
            ) from err

        if parsed_aya != expected_ayah_num:
            raise TranslationSourceError(
                f"Surah {expected_surah} ayah number mismatch: expected {expected_ayah_num}, got {parsed_aya}."
            )

        translation_text = item.get("translation")
        if not isinstance(translation_text, str) or len(translation_text) == 0:
            raise TranslationSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} contains empty or non-string translation text."
            )

        footnotes_val = item.get("footnotes")
        if footnotes_val is None:
            footnotes = ""
        elif isinstance(footnotes_val, str):
            footnotes = footnotes_val
        else:
            raise TranslationSourceError(
                f"Surah {expected_surah} ayah {expected_ayah_num} footnotes must be a string if present."
            )

        # Translation text immutability: store exact Unicode string as returned by the API without modification
        parsed_ayahs.append(
            TranslationAyah(
                surah_number=expected_surah,
                ayah_number=expected_ayah_num,
                english_text=translation_text,
                footnotes=footnotes,
            )
        )

    return TranslationSurah(
        number=expected_surah,
        number_of_ayahs=expected_ayah_count,
        ayahs=tuple(parsed_ayahs),
    )


def parse_complete_translation_dataset(
    metadata_payload: Union[str, bytes, dict],
    surah_payloads: Sequence[Union[str, bytes, dict]],
    target_key: str = DEFAULT_TRANSLATION_KEY,
    api_base_url: str = DEFAULT_API_BASE_URL,
    expected_language_iso: str = EXPECTED_LANGUAGE_ISO,
) -> TranslationData:
    """Assemble and strictly validate a complete 114-Surah translation dataset."""
    metadata = parse_translation_metadata(
        metadata_payload,
        target_key=target_key,
        api_base_url=api_base_url,
        expected_language_iso=expected_language_iso,
    )

    if len(surah_payloads) != EXPECTED_SURAH_COUNT:
        raise TranslationSourceError(
            f"Expected {EXPECTED_SURAH_COUNT} Surah payloads, received {len(surah_payloads)}."
        )

    parsed_surahs: list[TranslationSurah] = []
    for surah_idx, payload in enumerate(surah_payloads):
        expected_surah_num = surah_idx + 1
        surah = parse_surah_translation_payload(payload, expected_surah=expected_surah_num)
        parsed_surahs.append(surah)

    total_ayahs = sum(s.number_of_ayahs for s in parsed_surahs)
    if total_ayahs != EXPECTED_TOTAL_AYAH_COUNT:
        raise TranslationSourceError(
            f"Expected total {EXPECTED_TOTAL_AYAH_COUNT} translation ayahs, assembled {total_ayahs}."
        )

    return TranslationData(
        source=metadata,
        surahs=tuple(parsed_surahs),
    )


def _resolve_max_attempts(
    *,
    max_attempts: int,
    max_retries: Optional[int],
) -> int:
    """Retain the legacy retry argument while making the production contract explicit."""
    if max_retries is not None:
        if not isinstance(max_retries, int) or isinstance(max_retries, bool):
            raise TranslationSourceError("max_retries must be an integer when provided.")
        return max(1, max_retries + 1)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise TranslationSourceError("max_attempts must be a positive integer.")
    return max_attempts


def _retry_category_for_exception(err: BaseException) -> Optional[str]:
    if isinstance(err, urllib.error.HTTPError):
        if err.code in RETRYABLE_HTTP_STATUS_CODES:
            return f"http_{err.code}"
        return None
    if isinstance(err, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(reason, socket.gaierror):
            return "dns"
        return "network"
    if isinstance(err, (ConnectionError, ssl.SSLError)):
        return "network"
    return None


def _fetch_url_json(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_attempts: int = TRANSLATION_FETCH_MAX_ATTEMPTS,
    max_retries: Optional[int] = None,
    backoff_seconds: Sequence[float] = TRANSLATION_FETCH_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
    resource_label: str = "dataset_resource",
) -> bytes:
    """Fetch one secure QuranEnc JSON resource with bounded transient retries.

    Only transport faults and documented temporary HTTP statuses retry.  JSON,
    schema, identity, and version validation occur outside this function and
    fail immediately so malformed reference data can never be retried into
    acceptance.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise TranslationSourceError("API request URL must use secure HTTPS scheme.")
    attempts = _resolve_max_attempts(max_attempts=max_attempts, max_retries=max_retries)
    if len(backoff_seconds) < attempts:
        raise TranslationSourceError(
            f"backoff_seconds must provide at least {attempts} entries."
        )
    sleeper = sleep_fn or time.sleep
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    ssl_context = _build_ssl_context()
    last_error: Optional[BaseException] = None
    last_category = "unknown"

    for attempt_number in range(1, attempts + 1):
        LOGGER.info(
            "[TRANSLATION] translation_fetch_attempt=%s/%s resource=%s",
            attempt_number,
            attempts,
            resource_label,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                status_code = getattr(resp, "status", None) or resp.getcode()
                if status_code != 200:
                    response_error = urllib.error.HTTPError(
                        url=url,
                        code=int(status_code),
                        msg=f"HTTP status {status_code}",
                        hdrs=None,
                        fp=None,
                    )
                    category = _retry_category_for_exception(response_error)
                    if category is None:
                        raise TranslationSourceError(
                            f"HTTP request returned status {status_code}."
                        )
                    raise response_error
                return resp.read()
        except TranslationSourceError:
            raise
        except Exception as err:
            category = _retry_category_for_exception(err)
            if isinstance(err, urllib.error.HTTPError):
                # urllib's HTTPError is file-like; close its response body on
                # both retryable and deterministic failure paths.
                err.close()
            if category is None:
                if isinstance(err, urllib.error.HTTPError):
                    raise TranslationSourceError(
                        f"HTTP error {err.code}: {err.reason}"
                    ) from err
                raise TranslationSourceError(
                    f"Unexpected error while fetching translation data: {err}"
                ) from err
            last_error = err
            last_category = category
            if attempt_number >= attempts:
                raise TranslationTransientFetchError(
                    category,
                    "Network error while fetching translation data: "
                    f"category={category}; attempts={attempts}; error={err}",
                    attempts,
                ) from err
            delay_seconds = float(backoff_seconds[attempt_number])
            LOGGER.warning(
                "[TRANSLATION] translation_fetch_retry=%s next_attempt=%s/%s "
                "delay_seconds=%.1f resource=%s",
                category,
                attempt_number + 1,
                attempts,
                delay_seconds,
                resource_label,
            )
            if delay_seconds > 0:
                sleeper(delay_seconds)

    raise TranslationTransientFetchError(
        last_category,
        f"Network error while fetching translation data: {last_error}",
        attempts,
    )


def fetch_translation_metadata(
    api_base_url: str = DEFAULT_API_BASE_URL,
    target_key: str = DEFAULT_TRANSLATION_KEY,
    expected_language_iso: str = EXPECTED_LANGUAGE_ISO,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_attempts: int = TRANSLATION_FETCH_MAX_ATTEMPTS,
    max_retries: Optional[int] = None,
    backoff_seconds: Sequence[float] = TRANSLATION_FETCH_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> TranslationSourceMetadata:
    """Fetch official translation list and extract verified metadata for target translation."""
    url = (
        f"{api_base_url.rstrip('/')}/translations/list/"
        f"{expected_language_iso}?localization=en"
    )
    raw_body = _fetch_url_json(
        url,
        timeout=timeout,
        user_agent=user_agent,
        max_attempts=max_attempts,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        sleep_fn=sleep_fn,
        resource_label=f"metadata:{target_key}",
    )
    return parse_translation_metadata(
        raw_body,
        target_key=target_key,
        api_base_url=api_base_url,
        expected_language_iso=expected_language_iso,
    )


def fetch_surah_translation(
    surah_number: int,
    translation_key: str = DEFAULT_TRANSLATION_KEY,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_attempts: int = TRANSLATION_FETCH_MAX_ATTEMPTS,
    max_retries: Optional[int] = None,
    backoff_seconds: Sequence[float] = TRANSLATION_FETCH_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> TranslationSurah:
    """Fetch and validate translation for a single Surah."""
    if (
        not isinstance(surah_number, int)
        or isinstance(surah_number, bool)
        or surah_number < 1
        or surah_number > EXPECTED_SURAH_COUNT
    ):
        raise TranslationSourceError(
            f"Surah number {surah_number} is out of range (1..{EXPECTED_SURAH_COUNT})."
        )

    url = f"{api_base_url.rstrip('/')}/translation/sura/{translation_key}/{surah_number}"
    raw_body = _fetch_url_json(
        url,
        timeout=timeout,
        user_agent=user_agent,
        max_attempts=max_attempts,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        sleep_fn=sleep_fn,
        resource_label=f"surah:{translation_key}:{surah_number}",
    )
    return parse_surah_translation_payload(raw_body, expected_surah=surah_number)


def fetch_complete_translation_data(
    api_base_url: str = DEFAULT_API_BASE_URL,
    translation_key: str = DEFAULT_TRANSLATION_KEY,
    language_iso_code: str = EXPECTED_LANGUAGE_ISO,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_attempts: int = TRANSLATION_FETCH_MAX_ATTEMPTS,
    max_retries: Optional[int] = None,
    backoff_seconds: Sequence[float] = TRANSLATION_FETCH_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
    expected_version: Optional[str] = None,
) -> TranslationData:
    """Fetch metadata and all 114 Surahs, failing closed if any Surah fails."""
    metadata = fetch_translation_metadata(
        api_base_url=api_base_url,
        target_key=translation_key,
        expected_language_iso=language_iso_code,
        timeout=timeout,
        user_agent=user_agent,
        max_attempts=max_attempts,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        sleep_fn=sleep_fn,
    )

    # Reject an upstream replacement before downloading any of its 114 Surahs.
    if expected_version is not None and metadata.version != expected_version:
        raise TranslationSourceError(
            f"Translation version mismatch for '{translation_key}': "
            f"expected '{expected_version}', got '{metadata.version}'. "
            "Provide an explicitly provisioned verified reference; never relabel newer text."
        )

    parsed_surahs: list[TranslationSurah] = []
    for surah_num in range(1, EXPECTED_SURAH_COUNT + 1):
        surah = fetch_surah_translation(
            surah_number=surah_num,
            translation_key=translation_key,
            api_base_url=api_base_url,
            timeout=timeout,
            user_agent=user_agent,
            max_attempts=max_attempts,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            sleep_fn=sleep_fn,
        )
        parsed_surahs.append(surah)

    total_ayahs = sum(s.number_of_ayahs for s in parsed_surahs)
    if total_ayahs != EXPECTED_TOTAL_AYAH_COUNT:
        raise TranslationSourceError(
            f"Expected total {EXPECTED_TOTAL_AYAH_COUNT} translation ayahs, assembled {total_ayahs}."
        )

    return TranslationData(
        source=metadata,
        surahs=tuple(parsed_surahs),
    )


def _default_translation_cache_dir() -> Path:
    return Path(os.environ.get("QURAN_REELS_TRANSLATION_CACHE_DIR", DEFAULT_TRANSLATION_CACHE_DIR))


def _cache_path(
    cache_dir: Path,
    *,
    translation_key: str,
    translation_version: str,
) -> Path:
    safe_key = translation_key.replace("/", "_")
    safe_version = translation_version.replace("/", "_")
    return cache_dir / (
        f"quranenc-{safe_key}-{safe_version}-schema{TRANSLATION_CACHE_SCHEMA_VERSION}.json"
    )


def _cache_body_for_data(data: TranslationData) -> dict:
    return {
        "source": {
            "provider": data.source.provider,
            "api_base_url": data.source.api_base_url,
            "translation_key": data.source.translation_key,
            "language_iso_code": data.source.language_iso_code,
            "version": data.source.version,
            "last_update": data.source.last_update,
            "title": data.source.title,
            "description": data.source.description,
        },
        "surahs": [
            {
                "number": surah.number,
                "ayahs": [
                    {
                        "sura": ayah.surah_number,
                        "aya": ayah.ayah_number,
                        "translation": ayah.translation_text,
                        "footnotes": ayah.footnotes,
                    }
                    for ayah in surah.ayahs
                ],
            }
            for surah in data.surahs
        ],
    }


def _cache_body_sha256(body: dict) -> str:
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_pinned_translation_body(data: TranslationData) -> None:
    """Bind cached, imported and fetched English text to the original corpus."""
    identity = (data.source.translation_key, data.source.version)
    expected = PINNED_TRANSLATION_BODY_HASHES.get(identity)
    if expected is not None and _cache_body_sha256(_cache_body_for_data(data)) != expected:
        raise TranslationSourceError(
            f"Pinned translation body hash mismatch for '{identity[0]}' version '{identity[1]}'."
        )


def _translation_data_from_cache_payload(
    payload: object,
    *,
    translation_key: str,
    translation_version: str,
    language_iso_code: str,
    api_base_url: str,
) -> TranslationData:
    """Strictly revalidate a cache document before its text can be used."""
    if not isinstance(payload, dict):
        raise TranslationSourceError("Translation cache document must be an object.")
    if payload.get("schema_version") != TRANSLATION_CACHE_SCHEMA_VERSION:
        raise TranslationSourceError("Translation cache schema version mismatch.")
    if payload.get("translation_key") != translation_key:
        raise TranslationSourceError("Translation cache key mismatch.")
    if payload.get("translation_version") != translation_version:
        raise TranslationSourceError("Translation cache version mismatch.")
    if payload.get("language_iso_code") != language_iso_code:
        raise TranslationSourceError("Translation cache language mismatch.")
    body = payload.get("body")
    if not isinstance(body, dict):
        raise TranslationSourceError("Translation cache body is missing or invalid.")
    if payload.get("body_sha256") != _cache_body_sha256(body):
        raise TranslationSourceError("Translation cache integrity hash mismatch.")

    source = body.get("source")
    surahs = body.get("surahs")
    if not isinstance(source, dict) or not isinstance(surahs, list):
        raise TranslationSourceError("Translation cache source or Surah data is invalid.")
    if source.get("provider") != "QuranEnc":
        raise TranslationSourceError("Translation cache provider mismatch.")
    if source.get("api_base_url") != api_base_url:
        raise TranslationSourceError("Translation cache API base URL mismatch.")
    if source.get("translation_key") != translation_key:
        raise TranslationSourceError("Translation cache source key mismatch.")
    if source.get("language_iso_code") != language_iso_code:
        raise TranslationSourceError("Translation cache source language mismatch.")
    if source.get("version") != translation_version:
        raise TranslationSourceError("Translation cache source version mismatch.")
    if payload.get("last_update") != source.get("last_update"):
        raise TranslationSourceError("Translation cache last_update mismatch.")

    metadata_payload = {
        "translations": [
            {
                "key": source.get("translation_key"),
                "language_iso_code": source.get("language_iso_code"),
                "version": source.get("version"),
                "last_update": source.get("last_update"),
                "title": source.get("title"),
                "description": source.get("description"),
            }
        ]
    }
    surah_payloads: list[dict] = []
    for expected_surah, cached_surah in enumerate(surahs, start=1):
        if not isinstance(cached_surah, dict) or cached_surah.get("number") != expected_surah:
            raise TranslationSourceError("Translation cache Surah ordering mismatch.")
        ayahs = cached_surah.get("ayahs")
        if not isinstance(ayahs, list):
            raise TranslationSourceError("Translation cache ayah list is invalid.")
        surah_payloads.append({"result": ayahs})

    data = parse_complete_translation_dataset(
        metadata_payload,
        surah_payloads,
        target_key=translation_key,
        api_base_url=api_base_url,
        expected_language_iso=language_iso_code,
    )
    if data.source.version != translation_version:
        raise TranslationSourceError("Translation cache parsed version mismatch.")
    return data


def _load_cached_translation_data(
    cache_path: Path,
    *,
    translation_key: str,
    translation_version: str,
    language_iso_code: str,
    api_base_url: str,
) -> Optional[TranslationData]:
    if not cache_path.is_file():
        LOGGER.info(
            "[TRANSLATION] translation_cache=MISS key=%s version=%s reason=absent",
            translation_key,
            translation_version,
        )
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        data = _translation_data_from_cache_payload(
            payload,
            translation_key=translation_key,
            translation_version=translation_version,
            language_iso_code=language_iso_code,
            api_base_url=api_base_url,
        )
        _require_pinned_translation_body(data)
    except (OSError, json.JSONDecodeError, TranslationSourceError) as err:
        LOGGER.warning(
            "[TRANSLATION] translation_cache=MISS key=%s version=%s reason=invalid",
            translation_key,
            translation_version,
        )
        return None
    LOGGER.info(
        "[TRANSLATION] translation_cache=HIT key=%s version=%s",
        translation_key,
        translation_version,
    )
    LOGGER.info("[TRANSLATION] translation_dataset_validated=true source=cache")
    return data


def _write_validated_translation_cache(cache_path: Path, data: TranslationData) -> None:
    body = _cache_body_for_data(data)
    payload = {
        "schema_version": TRANSLATION_CACHE_SCHEMA_VERSION,
        "translation_key": data.source.translation_key,
        "translation_version": data.source.version,
        "language_iso_code": data.source.language_iso_code,
        "last_update": data.source.last_update,
        "body_sha256": _cache_body_sha256(body),
        "body": body,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{cache_path.name}.",
        suffix=".tmp",
        dir=str(cache_path.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as cache_file:
            json.dump(payload, cache_file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            cache_file.flush()
            os.fsync(cache_file.fileno())
        os.replace(temporary_path, cache_path)
    except OSError:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def _load_pinned_translation_reference(
    reference_path: Union[Path, str],
    *,
    translation_key: str,
    translation_version: str,
    language_iso_code: str,
    api_base_url: str,
) -> TranslationData:
    """Read an explicitly supplied private JSON/gzip reference without network."""
    if (translation_key, translation_version) not in PINNED_TRANSLATION_BODY_HASHES:
        raise TranslationSourceError("Pinned translation reference is unsupported for this source/version.")
    path = Path(reference_path)
    try:
        if path.stat().st_size > MAX_TRANSLATION_REFERENCE_BYTES:
            raise TranslationSourceError("Translation reference size limit exceeded.")
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as reference:
            raw = reference.read(MAX_TRANSLATION_REFERENCE_BYTES + 1)
        if len(raw) > MAX_TRANSLATION_REFERENCE_BYTES:
            raise TranslationSourceError("Translation reference size limit exceeded.")
        payload = json.loads(raw)
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, zlib.error) as error:
        raise TranslationSourceError("Pinned translation reference is missing or invalid.") from error
    data = _translation_data_from_cache_payload(
        payload, translation_key=translation_key, translation_version=translation_version,
        language_iso_code=language_iso_code, api_base_url=api_base_url,
    )
    _require_pinned_translation_body(data)
    return data


def fetch_translation_data_for_language(
    language_iso_code: str,
    *,
    cache_dir: Optional[Union[Path, str]] = None,
    reference_path: Optional[Union[Path, str]] = None,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_attempts: int = TRANSLATION_FETCH_MAX_ATTEMPTS,
    max_retries: Optional[int] = None,
    backoff_seconds: Sequence[float] = TRANSLATION_FETCH_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> TranslationData:
    """Load the exact pinned source from cache, explicit private reference or API."""
    translation_key = SUPPORTED_TRANSLATION_SOURCES.get(language_iso_code)
    if translation_key is None:
        raise TranslationSourceError(
            f"Unsupported translation language '{language_iso_code}'. "
            f"Expected one of {tuple(SUPPORTED_TRANSLATION_SOURCES)}."
        )
    expected_version = EXPECTED_TRANSLATION_VERSIONS.get(translation_key)
    if expected_version is None:
        raise TranslationSourceError(f"No pinned version configured for '{translation_key}'.")
    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else _default_translation_cache_dir()
    cache_path = _cache_path(
        resolved_cache_dir,
        translation_key=translation_key,
        translation_version=expected_version,
    )
    cached = _load_cached_translation_data(
        cache_path,
        translation_key=translation_key,
        translation_version=expected_version,
        language_iso_code=language_iso_code,
        api_base_url=api_base_url,
    )
    if cached is not None:
        return cached

    resolved_reference = reference_path
    if resolved_reference is None and translation_key == DEFAULT_TRANSLATION_KEY:
        resolved_reference = os.environ.get(ENGLISH_REFERENCE_PATH_ENV, "").strip() or None
    if resolved_reference is not None:
        data = _load_pinned_translation_reference(
            resolved_reference, translation_key=translation_key,
            translation_version=expected_version, language_iso_code=language_iso_code,
            api_base_url=api_base_url,
        )
        source_kind = "private_reference"
    else:
        try:
            data = fetch_complete_translation_data(
                api_base_url=api_base_url,
                translation_key=translation_key,
                language_iso_code=language_iso_code,
                timeout=timeout,
                user_agent=user_agent,
                max_attempts=max_attempts,
                max_retries=max_retries,
                backoff_seconds=backoff_seconds,
                sleep_fn=sleep_fn,
                expected_version=expected_version,
            )
        except TranslationTransientFetchError as err:
            raise TranslationDatasetFetchExhaustedError(
                "TRANSLATION_DATASET_FETCH_EXHAUSTED "
                f"translation_key={translation_key} version={expected_version} "
                f"attempts={err.attempts} final_network_error={err.category}"
            ) from err
        source_kind = "remote"
    if data.source.version != expected_version:
        raise TranslationSourceError(
            f"Translation version mismatch for '{translation_key}': "
            f"expected '{expected_version}', got '{data.source.version}'."
        )
    _require_pinned_translation_body(data)
    try:
        _write_validated_translation_cache(cache_path, data)
    except OSError as err:
        # The dataset was fully validated; cache persistence is an
        # optimization and must never make a valid generation fail.
        LOGGER.warning(
            "[TRANSLATION] translation_cache_write=FAILED key=%s version=%s error=%s",
            translation_key,
            expected_version,
            err.__class__.__name__,
        )
    LOGGER.info("[TRANSLATION] translation_dataset_validated=true source=%s", source_kind)
    return data
