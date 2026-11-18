"""Meta Graph API publishing layer for Instagram Reels and Facebook Page Reels."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence

from engine.storage import B2Config, StoredConfig, create_presigned_get_url


# Default Meta Graph API version as of August 2026
DEFAULT_META_API_VERSION = "v26.0"
GRAPH_API_BASE_URL = "https://graph.facebook.com"
RECITER_ATTRIBUTION_AR = "القارئ: ياسر الدوسري"
RECITER_ATTRIBUTION_EN = "Reciter: Yasser Al-Dosari"
RECITER_HASHTAG = "#ياسر_الدوسري"

# Required permissions documented for production operation
REQUIRED_INSTAGRAM_PERMISSIONS = (
    "pages_show_list",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_content_publish",
)
REQUIRED_FACEBOOK_PERMISSIONS = (
    "pages_manage_posts",
    "pages_read_engagement",
)


class PublishError(Exception):
    """Raised when an unrecoverable error occurs during social publishing."""


@dataclass(frozen=True)
class MetaConfig:
    """Connection parameters and authentication for Meta Graph API."""

    page_id: str
    page_access_token: str
    ig_user_id: Optional[str] = None
    api_version: str = DEFAULT_META_API_VERSION
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.api_version or not re.match(r"^v\d+(\.\d+)?$", self.api_version):
            raise PublishError(
                f"Invalid Meta API version '{self.api_version}'. Expected format 'v<major>.0'."
            )
        if not self.page_id.strip():
            raise PublishError("page_id must not be empty.")
        if not self.page_access_token.strip():
            raise PublishError("page_access_token must not be empty.")


@dataclass(frozen=True)
class PublishRequest:
    """Parameters for publishing a rendered Reel to social platforms."""

    media_key: str
    caption: str
    facebook_enabled: bool = True
    instagram_enabled: bool = True
    instagram_share_to_feed: bool = False
    existing_facebook_status: str = "NOT_ATTEMPTED"
    existing_instagram_status: str = "NOT_ATTEMPTED"
    existing_facebook_media_id: Optional[str] = None
    existing_instagram_media_id: Optional[str] = None
    editorial_record: Any = None


@dataclass(frozen=True)
class PlatformPublishResult:
    """Publishing outcome for an individual social platform."""

    platform: str  # "facebook" | "instagram"
    status: str  # "NOT_ATTEMPTED" | "SUCCESS" | "FAILED" | "BLOCKED" | "UNKNOWN" | "NOT_CONFIGURED"
    media_id: Optional[str] = None
    failure_reason: Optional[str] = None


@dataclass(frozen=True)
class PublishResult:
    """Aggregate result of publishing across all requested platforms."""

    facebook: PlatformPublishResult
    instagram: PlatformPublishResult
    fully_successful: bool
    primary_successful: bool = True


def check_reciter_reuse_rights(
    stored_config_confirmed: bool,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Verify that BOTH StoredConfig and runtime environment confirm reciter reuse rights."""
    source = env if env is not None else os.environ
    raw_env = source.get("RECITER_REUSE_CONFIRMED", "false").strip().lower()
    env_confirmed = raw_env in ("true", "1", "yes")
    return bool(stored_config_confirmed and env_confirmed)


def load_meta_config_from_env(env: Optional[Mapping[str, str]] = None) -> MetaConfig:
    """Load Meta configuration parameters from environment variables."""
    source = env if env is not None else os.environ
    page_id = source.get("META_PAGE_ID", "").strip()
    ig_user_id = source.get("META_IG_USER_ID", "").strip() or None
    page_access_token = source.get("META_PAGE_ACCESS_TOKEN", "").strip()
    api_version = source.get("META_API_VERSION", DEFAULT_META_API_VERSION).strip()

    missing = []
    if not page_id:
        missing.append("META_PAGE_ID")
    if not page_access_token:
        missing.append("META_PAGE_ACCESS_TOKEN")

    if missing:
        raise PublishError(f"Missing required Meta environment variables: {', '.join(missing)}")

    return MetaConfig(
        page_id=page_id,
        ig_user_id=ig_user_id,
        page_access_token=page_access_token,
        api_version=api_version,
    )


CANONICAL_SURAH_NAMES: dict[int, tuple[str, str]] = {
    1: ("الفاتحة", "Al-Fatihah"),
    2: ("البقرة", "Al-Baqarah"),
    3: ("آل عمران", "Ali 'Imran"),
    4: ("النساء", "An-Nisa'"),
    5: ("المائدة", "Al-Ma'idah"),
    6: ("الأنعام", "Al-An'am"),
    7: ("الأعراف", "Al-A'raf"),
    8: ("الأنفال", "Al-Anfal"),
    9: ("التوبة", "At-Tawbah"),
    10: ("يونس", "Yunus"),
    11: ("هود", "Hud"),
    12: ("يوسف", "Yusuf"),
    13: ("الرعد", "Ar-Ra'd"),
    14: ("إبراهيم", "Ibrahim"),
    15: ("الحجر", "Al-Hijr"),
    16: ("النحل", "An-Nahl"),
    17: ("الإسراء", "Al-Isra'"),
    18: ("الكهف", "Al-Kahf"),
    19: ("مريم", "Maryam"),
    20: ("طه", "Ta-Ha"),
    21: ("الأنبياء", "Al-Anbiya'"),
    22: ("الحج", "Al-Hajj"),
    23: ("المؤمنون", "Al-Mu'minun"),
    24: ("النور", "An-Nur"),
    25: ("الفرقان", "Al-Furqan"),
    26: ("الشعراء", "Ash-Shu'ara'"),
    27: ("النمل", "An-Naml"),
    28: ("القصص", "Al-Qasas"),
    29: ("العنكبوت", "Al-'Ankabut"),
    30: ("الروم", "Ar-Rum"),
    31: ("لقمان", "Luqman"),
    32: ("السجدة", "As-Sajdah"),
    33: ("الأحزاب", "Al-Ahzab"),
    34: ("سبأ", "Saba'"),
    35: ("فاطر", "Fatir"),
    36: ("يس", "Ya-Sin"),
    37: ("الصافات", "As-Saffat"),
    38: ("ص", "Sad"),
    39: ("الزمر", "Az-Zumar"),
    40: ("غافر", "Ghafir"),
    41: ("فصلت", "Fussilat"),
    42: ("الشورى", "Ash-Shura"),
    43: ("الزخرف", "Az-Zukhruf"),
    44: ("الدخان", "Ad-Dukhan"),
    45: ("الجاثية", "Al-Jathiyah"),
    46: ("الأحقاف", "Al-Ahqaf"),
    47: ("محمد", "Muhammad"),
    48: ("الفتح", "Al-Fath"),
    49: ("الحجرات", "Al-Hujurat"),
    50: ("ق", "Qaf"),
    51: ("الذاريات", "Adh-Dhariyat"),
    52: ("الطور", "At-Tur"),
    53: ("النجم", "An-Najm"),
    54: ("القمر", "Al-Qamar"),
    55: ("الرحمن", "Ar-Rahman"),
    56: ("الواقعة", "Al-Waqi'ah"),
    57: ("الحديد", "Al-Hadid"),
    58: ("المجادلة", "Al-Mujadilah"),
    59: ("الحشر", "Al-Hashr"),
    60: ("الممتحنة", "Al-Mumtahanah"),
    61: ("الصف", "As-Saff"),
    62: ("الجمعة", "Al-Jumu'ah"),
    63: ("المنافقون", "Al-Munafiqun"),
    64: ("التغابن", "At-Taghabun"),
    65: ("الطلاق", "At-Talaq"),
    66: ("التحريم", "At-Tahrim"),
    67: ("الملك", "Al-Mulk"),
    68: ("القلم", "Al-Qalam"),
    69: ("الحاقة", "Al-Haqqah"),
    70: ("المعارج", "Al-Ma'arij"),
    71: ("نوح", "Nuh"),
    72: ("الجن", "Al-Jinn"),
    73: ("المزمل", "Al-Muzzammil"),
    74: ("المدثر", "Al-Muddaththir"),
    75: ("القيامة", "Al-Qiyamah"),
    76: ("الإنسان", "Al-Insan"),
    77: ("المرسلات", "Al-Mursalat"),
    78: ("النبأ", "An-Naba'"),
    79: ("النازعات", "An-Nazi'at"),
    80: ("عبس", "'Abasa"),
    81: ("التكوير", "At-Takwir"),
    82: ("الانفطار", "Al-Infitar"),
    83: ("المطففين", "Al-Mutaffifin"),
    84: ("الانشقاق", "Al-Inshiqaq"),
    85: ("البروج", "Al-Buruj"),
    86: ("الطارق", "At-Tariq"),
    87: ("الأعلى", "Al-A'la"),
    88: ("الغاشية", "Al-Ghashiyah"),
    89: ("الفجر", "Al-Fajr"),
    90: ("البلد", "Al-Balad"),
    91: ("الشمس", "Ash-Shams"),
    92: ("الليل", "Al-Layl"),
    93: ("الضحى", "Ad-Duha"),
    94: ("الشرح", "Ash-Sharh"),
    95: ("التين", "At-Tin"),
    96: ("العلق", "Al-'Alaq"),
    97: ("القدر", "Al-Qadr"),
    98: ("البينة", "Al-Bayyinah"),
    99: ("الزلزلة", "Az-Zalzalah"),
    100: ("العاديات", "Al-'Adiyat"),
    101: ("القارعة", "Al-Qari'ah"),
    102: ("التكاثر", "At-Takathur"),
    103: ("العصر", "Al-'Asr"),
    104: ("الهمزة", "Al-Humazah"),
    105: ("الفيل", "Al-Fil"),
    106: ("قريش", "Quraysh"),
    107: ("الماعون", "Al-Ma'un"),
    108: ("الكوثر", "Al-Kawthar"),
    109: ("الكافرون", "Al-Kafirun"),
    110: ("النصر", "An-Nasr"),
    111: ("المسد", "Al-Masad"),
    112: ("الإخلاص", "Al-Ikhlas"),
    113: ("الفلق", "Al-Falaq"),
    114: ("الناس", "An-Nas"),
}


def build_reel_caption(
    surah_name_ar: Optional[str] = None,
    surah_name_en: Optional[str] = None,
    surah_number: int = 1,
    first_ayah: int = 1,
    last_ayah: int = 1,
    reciter_name: str = "Yasser Al-Dosari",
    translation_source: str = "Rowwad Translation Center / QuranEnc",
    translation_sources: Optional[Sequence[Mapping[str, Any]]] = None,
    videographer_name: Optional[str] = None,
    videographer_names: Optional[Sequence[str]] = None,
    caption_ayahs: Optional[Sequence[Mapping[str, Any]]] = None,
) -> str:
    """Build a deterministic caption, validating exact source text when supplied."""
    if caption_ayahs is not None:
        # Import locally because the Buffer adapter shares our publishing types.
        # Both backends enforce the same source hashes and exact Quran text.
        from engine.buffer_publisher import build_buffer_facebook_caption

        record = SimpleNamespace(
            surah_number=surah_number,
            first_ayah=first_ayah,
            last_ayah=last_ayah,
            surah_name_ar=surah_name_ar,
            surah_name_en=surah_name_en,
            provenance={"caption_ayahs": caption_ayahs},
        )
        return f"{build_buffer_facebook_caption(record)} #القرآن"

    fallback_ar, fallback_en = CANONICAL_SURAH_NAMES.get(
        surah_number, (f"سورة {surah_number}", f"Surah {surah_number}")
    )
    ar_name = surah_name_ar.strip() if (surah_name_ar and surah_name_ar.strip()) else fallback_ar
    en_name = surah_name_en.strip() if (surah_name_en and surah_name_en.strip()) else fallback_en

    if first_ayah == last_ayah:
        ayah_str = f"{surah_number}:{first_ayah}"
    else:
        ayah_str = f"{surah_number}:{first_ayah}–{last_ayah}"

    header = f"سورة {ar_name} | {en_name} {ayah_str}"

    lines = [
        header,
        "",
        RECITER_ATTRIBUTION_AR,
        RECITER_ATTRIBUTION_EN,
    ]
    if translation_sources:
        language_names = {"en": "English", "de": "German"}
        for source in translation_sources:
            language = str(source.get("language", "")).strip()
            source_name = str(source.get("source", translation_source)).strip()
            version = str(source.get("version", "")).strip()
            label = language_names.get(language, language.upper() or "Translation")
            suffix = f" (version {version})" if version else ""
            lines.append(f"{label} translation: {source_name}{suffix}")
    else:
        lines.append(f"English translation: {translation_source}")

    # Process single and multiple videographer credits with deduplication
    names: list[str] = []
    if videographer_names:
        for n in videographer_names:
            if n and isinstance(n, str) and n.strip() and n.strip() not in names:
                names.append(n.strip())
    elif videographer_name and isinstance(videographer_name, str) and videographer_name.strip():
        names.append(videographer_name.strip())

    if len(names) == 1:
        lines.append(f"Background: {names[0]} / Pexels")
    elif len(names) > 1:
        lines.append(f"Background videos: {', '.join(names)} / Pexels")

    lines.extend([
        "",
        f"#Quran #القرآن {RECITER_HASHTAG}",
    ])
    return "\n".join(lines)


def build_reel_caption_from_record(record: Any) -> str:
    """Build a source-bound caption, retaining metadata-only legacy compatibility."""
    provenance = getattr(record, "provenance", {})
    if isinstance(provenance, Mapping) and "caption_ayahs" in provenance:
        if provenance["caption_ayahs"] is None:
            raise PublishError("Source-bound caption_ayahs provenance is missing.")
        return build_reel_caption(
            surah_name_ar=getattr(record, "surah_name_ar", None),
            surah_name_en=getattr(record, "surah_name_en", None),
            surah_number=getattr(record, "surah_number", None),
            first_ayah=getattr(record, "first_ayah", None),
            last_ayah=getattr(record, "last_ayah", None),
            caption_ayahs=provenance["caption_ayahs"],
        )

    surah_num = getattr(record, "surah_number", 1) or 1
    first_a = getattr(record, "first_ayah", 1) or 1
    last_a = getattr(record, "last_ayah", 1) or 1

    prov = getattr(record, "provenance", {})
    if not isinstance(prov, dict):
        prov = {}

    surah_ar = getattr(record, "surah_name_ar", None) or prov.get("surah_name_ar")
    surah_en = getattr(record, "surah_name_en", None) or prov.get("surah_name_en")
    reciter = prov.get("audio_reciter", "Yasser Al-Dosari")
    clean_reciter = reciter.split("(")[0].strip() if "(" in reciter else reciter
    translation_source = prov.get("translation_source", "Rowwad Translation Center / QuranEnc")
    translation_sources = prov.get("translation_sources")
    if not isinstance(translation_sources, (list, tuple)):
        translation_sources = None
        record_sources = getattr(record, "translation_sources", {})
        if isinstance(record_sources, Mapping):
            normalized_sources: list[dict[str, Any]] = []
            for language, metadata in record_sources.items():
                if not isinstance(metadata, Mapping):
                    continue
                source_entry = dict(metadata)
                source_entry.setdefault("language", str(language))
                normalized_sources.append(source_entry)
            if normalized_sources:
                translation_sources = normalized_sources

    pexels_videos = prov.get("pexels_videos", [])
    videographer_names: list[str] = []
    if isinstance(pexels_videos, (list, tuple)):
        for vid in pexels_videos:
            if isinstance(vid, dict):
                vname = vid.get("videographer_name", "")
                if vname and isinstance(vname, str) and vname.strip():
                    videographer_names.append(vname.strip())

    return build_reel_caption(
        surah_name_ar=surah_ar,
        surah_name_en=surah_en,
        surah_number=surah_num,
        first_ayah=first_a,
        last_ayah=last_a,
        reciter_name=clean_reciter,
        translation_source=translation_source,
        translation_sources=translation_sources,
        videographer_names=videographer_names,
    )


def _sanitize_error_message(err_msg: str, token: str) -> str:
    """Remove access token strings from error text to prevent credential leaks."""
    if not token or not token.strip():
        return err_msg
    return err_msg.replace(token, "[REDACTED_PAGE_ACCESS_TOKEN]")


def _default_http_post(
    url: str,
    data: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute HTTP POST request and return parsed JSON dictionary."""
    encoded_data = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as http_err:
        try:
            error_body = http_err.read().decode("utf-8")
            parsed_err = json.loads(error_body)
            meta_msg = (
                parsed_err.get("error", {}).get("message")
                or parsed_err.get("error", {}).get("error_user_msg")
                or str(http_err)
            )
            code = parsed_err.get("error", {}).get("code")
            subcode = parsed_err.get("error", {}).get("error_subcode")
            raise PublishError(
                f"Meta API HTTP {http_err.code} error: {meta_msg} (code={code}, subcode={subcode})"
            )
        except (ValueError, KeyError):
            raise PublishError(f"Meta API HTTP {http_err.code} error: {http_err.reason}")
    except Exception as err:
        raise PublishError(f"HTTP POST request failed: {err}")


def _default_http_get(
    url: str,
    headers: dict[str, str],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute HTTP GET request and return parsed JSON dictionary."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as http_err:
        try:
            error_body = http_err.read().decode("utf-8")
            parsed_err = json.loads(error_body)
            meta_msg = (
                parsed_err.get("error", {}).get("message")
                or parsed_err.get("error", {}).get("error_user_msg")
                or str(http_err)
            )
            code = parsed_err.get("error", {}).get("code")
            subcode = parsed_err.get("error", {}).get("error_subcode")
            raise PublishError(
                f"Meta API HTTP {http_err.code} error: {meta_msg} (code={code}, subcode={subcode})"
            )
        except (ValueError, KeyError):
            raise PublishError(f"Meta API HTTP {http_err.code} error: {http_err.reason}")
    except Exception as err:
        raise PublishError(f"HTTP GET request failed: {err}")


def publish_instagram_reel(
    request: PublishRequest,
    meta_config: MetaConfig,
    video_url: str,
    *,
    http_post_fn: Optional[Callable[..., dict[str, Any]]] = None,
    http_get_fn: Optional[Callable[..., dict[str, Any]]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    poll_interval_seconds: float = 60.0,
    max_wait_seconds: float = 300.0,
) -> PlatformPublishResult:
    """Publish a Reel to Instagram via container creation, polling, and media_publish."""
    from engine.reel_publication_policy import require_publication_request
    from engine.reel_theme_policy import EditorialPolicyError
    try:
        require_publication_request(request.editorial_record, request.caption, backend="meta")
    except EditorialPolicyError as err:
        raise PublishError(str(err)) from err
    if request.existing_instagram_status in ("SUCCESS", "ACCEPTED"):
        return PlatformPublishResult(
            platform="instagram",
            status=request.existing_instagram_status,
            media_id=request.existing_instagram_media_id,
        )
    if request.existing_instagram_status == "UNKNOWN":
        return PlatformPublishResult(
            platform="instagram",
            status="UNKNOWN",
            media_id=request.existing_instagram_media_id,
            failure_reason="Instagram publishing status is UNKNOWN; manual verification in Meta Business Suite is required before retry.",
        )

    if not meta_config.ig_user_id or not meta_config.ig_user_id.strip():
        return PlatformPublishResult(
            platform="instagram",
            status="NOT_CONFIGURED",
            failure_reason="Instagram Professional Account is not configured (META_IG_USER_ID is not set).",
        )

    post_fn = http_post_fn or _default_http_post
    get_fn = http_get_fn or _default_http_get
    sleeper = sleep_fn or time.sleep

    headers = {
        "Authorization": f"Bearer {meta_config.page_access_token}",
        "User-Agent": "QuranReelsPublisher/1.0",
    }

    # Step 1: Create IG media container
    create_url = f"{GRAPH_API_BASE_URL}/{meta_config.api_version}/{meta_config.ig_user_id}/media"
    create_data = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": request.caption,
        "share_to_feed": "true" if request.instagram_share_to_feed else "false",
    }

    try:
        create_resp = post_fn(
            create_url,
            create_data,
            headers=headers,
            timeout_seconds=meta_config.timeout_seconds,
        )
    except Exception as err:
        clean_err = _sanitize_error_message(str(err), meta_config.page_access_token)
        return PlatformPublishResult(
            platform="instagram",
            status="FAILED",
            failure_reason=f"Failed to create Instagram container: {clean_err}",
        )

    container_id = str(create_resp.get("id", "")).strip()
    if not container_id:
        return PlatformPublishResult(
            platform="instagram",
            status="FAILED",
            failure_reason="Instagram container creation returned empty ID.",
        )

    # Step 2: Poll container status
    status_url = (
        f"{GRAPH_API_BASE_URL}/{meta_config.api_version}/{container_id}?fields=status_code,status"
    )
    start_time = time.monotonic()

    while True:
        try:
            status_resp = get_fn(
                status_url,
                headers=headers,
                timeout_seconds=meta_config.timeout_seconds,
            )
        except Exception as err:
            clean_err = _sanitize_error_message(str(err), meta_config.page_access_token)
            return PlatformPublishResult(
                platform="instagram",
                status="FAILED",
                failure_reason=f"Failed to check Instagram container status: {clean_err}",
            )

        status_code = status_resp.get("status_code", "").upper()

        if status_code == "FINISHED":
            break
        elif status_code in ("ERROR", "EXPIRED"):
            err_details = status_resp.get("status", "Container error/expired.")
            return PlatformPublishResult(
                platform="instagram",
                status="FAILED",
                failure_reason=f"Instagram container entered failed state: {status_code} ({err_details})",
            )
        elif status_code in ("IN_PROGRESS", ""):
            elapsed = time.monotonic() - start_time
            if elapsed >= max_wait_seconds:
                return PlatformPublishResult(
                    platform="instagram",
                    status="FAILED",
                    failure_reason=f"Instagram container polling timed out after {int(elapsed)}s.",
                )
            sleeper(poll_interval_seconds)
        else:
            return PlatformPublishResult(
                platform="instagram",
                status="FAILED",
                failure_reason=f"Unknown Instagram container status_code: '{status_code}'.",
            )

    # Step 3: Publish container
    publish_url = (
        f"{GRAPH_API_BASE_URL}/{meta_config.api_version}/{meta_config.ig_user_id}/media_publish"
    )
    publish_data = {"creation_id": container_id}

    try:
        pub_resp = post_fn(
            publish_url,
            publish_data,
            headers=headers,
            timeout_seconds=meta_config.timeout_seconds,
        )
    except Exception as err:
        clean_err = _sanitize_error_message(str(err), meta_config.page_access_token)
        err_lower = clean_err.lower()
        if "timeout" in err_lower or "timed out" in err_lower or "reset" in err_lower:
            return PlatformPublishResult(
                platform="instagram",
                status="UNKNOWN",
                media_id=container_id,
                failure_reason=f"Ambiguous network outcome during Instagram media_publish phase: {clean_err}",
            )
        return PlatformPublishResult(
            platform="instagram",
            status="FAILED",
            failure_reason=f"Failed to publish Instagram media container: {clean_err}",
        )

    media_id = str(pub_resp.get("id", "")).strip()
    if not media_id:
        return PlatformPublishResult(
            platform="instagram",
            status="FAILED",
            failure_reason="Instagram media_publish returned empty ID.",
        )

    return PlatformPublishResult(
        platform="instagram",
        status="SUCCESS",
        media_id=media_id,
    )


def publish_facebook_reel(
    request: PublishRequest,
    meta_config: MetaConfig,
    video_url: str,
    *,
    http_post_fn: Optional[Callable[..., dict[str, Any]]] = None,
    http_get_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> PlatformPublishResult:
    """Publish a Reel to Facebook Page via video_reels upload session and hosted transfer."""
    from engine.reel_publication_policy import require_publication_request
    from engine.reel_theme_policy import EditorialPolicyError
    try:
        require_publication_request(request.editorial_record, request.caption, backend="meta")
    except EditorialPolicyError as err:
        raise PublishError(str(err)) from err
    if request.existing_facebook_status in ("SUCCESS", "ACCEPTED"):
        return PlatformPublishResult(
            platform="facebook",
            status=request.existing_facebook_status,
            media_id=request.existing_facebook_media_id,
        )
    if request.existing_facebook_status == "UNKNOWN":
        return PlatformPublishResult(
            platform="facebook",
            status="UNKNOWN",
            media_id=request.existing_facebook_media_id,
            failure_reason="Facebook publishing status is UNKNOWN; manual verification in Meta Business Suite is required before retry.",
        )

    post_fn = http_post_fn or _default_http_post
    get_fn = http_get_fn or _default_http_get

    headers = {
        "Authorization": f"Bearer {meta_config.page_access_token}",
        "User-Agent": "QuranReelsPublisher/1.0",
    }

    # Step 1: Start Video Reel Session
    start_url = f"{GRAPH_API_BASE_URL}/{meta_config.api_version}/me/video_reels"
    start_data = {"upload_phase": "start"}

    try:
        start_resp = post_fn(
            start_url,
            start_data,
            headers=headers,
            timeout_seconds=meta_config.timeout_seconds,
        )
    except Exception as err:
        clean_err = _sanitize_error_message(str(err), meta_config.page_access_token)
        return PlatformPublishResult(
            platform="facebook",
            status="FAILED",
            failure_reason=f"Failed to start Facebook Reel session: {clean_err}",
        )

    video_id = str(start_resp.get("video_id", "")).strip()
    upload_url = str(start_resp.get("upload_url", "")).strip()

    if not video_id or not upload_url:
        return PlatformPublishResult(
            platform="facebook",
            status="FAILED",
            failure_reason="Facebook Reel session start returned missing video_id or upload_url.",
        )

    # Step 2: Upload hosted video using presigned GET URL
    upload_headers = {
        "Authorization": f"OAuth {meta_config.page_access_token}",
        "file_url": video_url,
        "User-Agent": "QuranReelsPublisher/1.0",
    }

    try:
        post_fn(
            upload_url,
            data={},
            headers=upload_headers,
            timeout_seconds=meta_config.timeout_seconds,
        )
    except Exception as err:
        clean_err = _sanitize_error_message(str(err), meta_config.page_access_token)
        return PlatformPublishResult(
            platform="facebook",
            status="FAILED",
            failure_reason=f"Failed to transfer hosted video to Facebook: {clean_err}",
        )

    # Step 3: Finish and Publish Reel
    finish_url = f"{GRAPH_API_BASE_URL}/{meta_config.api_version}/me/video_reels"
    finish_data = {
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": "PUBLISHED",
        "description": request.caption,
    }

    try:
        finish_resp = post_fn(
            finish_url,
            finish_data,
            headers=headers,
            timeout_seconds=meta_config.timeout_seconds,
        )
    except Exception as err:
        clean_err = _sanitize_error_message(str(err), meta_config.page_access_token)
        err_lower = clean_err.lower()
        if "timeout" in err_lower or "timed out" in err_lower or "reset" in err_lower:
            return PlatformPublishResult(
                platform="facebook",
                status="UNKNOWN",
                media_id=video_id,
                failure_reason=f"Ambiguous network outcome during Facebook Reel finish phase: {clean_err}",
            )
        return PlatformPublishResult(
            platform="facebook",
            status="FAILED",
            failure_reason=f"Failed to finish Facebook Reel publication: {clean_err}",
        )

    if finish_resp.get("success") is False:
        return PlatformPublishResult(
            platform="facebook",
            status="FAILED",
            failure_reason="Facebook Reel finish returned success=False.",
        )

    # Step 4: Status confirmation check
    status_url = f"{GRAPH_API_BASE_URL}/{meta_config.api_version}/{video_id}?fields=status"
    try:
        get_fn(
            status_url,
            headers=headers,
            timeout_seconds=meta_config.timeout_seconds,
        )
    except Exception:
        # Bounded non-fatal confirmation check
        pass

    return PlatformPublishResult(
        platform="facebook",
        status="SUCCESS",
        media_id=video_id,
    )


def publish_reel(
    request: PublishRequest,
    meta_config: MetaConfig,
    stored_config: StoredConfig,
    s3_client: Any,
    b2_config: Optional[B2Config] = None,
    *,
    r2_config: Optional[B2Config] = None,
    env: Optional[Mapping[str, str]] = None,
    on_checkpoint: Optional[Callable[[str, PlatformPublishResult], None]] = None,
    http_post_fn: Optional[Callable[..., dict[str, Any]]] = None,
    http_get_fn: Optional[Callable[..., dict[str, Any]]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    poll_interval_seconds: float = 60.0,
    max_wait_seconds: float = 300.0,
) -> PublishResult:
    """Coordinate multi-platform Reel publishing with checkpointing, Facebook-primary ordering, and rights gating."""
    from engine.reel_publication_policy import require_publication_request
    from engine.reel_theme_policy import EditorialPolicyError
    try:
        require_publication_request(request.editorial_record, request.caption, backend="meta")
    except EditorialPolicyError as err:
        raise PublishError(str(err)) from err
    storage_config = b2_config if b2_config is not None else r2_config
    if storage_config is None:
        raise PublishError("b2_config is required for social publishing.")

    # 1. Double Rights Gate
    if not check_reciter_reuse_rights(stored_config.reciter_reuse_confirmed, env=env):
        blocked_reason = (
            "Publishing is BLOCKED: Reciter reuse rights are not confirmed. "
            "Both StoredConfig.reciter_reuse_confirmed and RECITER_REUSE_CONFIRMED must be true."
        )
        return PublishResult(
            facebook=PlatformPublishResult(
                platform="facebook", status="BLOCKED", failure_reason=blocked_reason
            ),
            instagram=PlatformPublishResult(
                platform="instagram", status="BLOCKED", failure_reason=blocked_reason
            ),
            fully_successful=False,
            primary_successful=False,
        )

    # 2. Check platform enablement
    target_fb = request.facebook_enabled and stored_config.facebook_enabled
    target_ig = request.instagram_enabled and stored_config.instagram_enabled

    if not target_fb and not target_ig:
        return PublishResult(
            facebook=PlatformPublishResult(platform="facebook", status="NOT_ATTEMPTED"),
            instagram=PlatformPublishResult(platform="instagram", status="NOT_ATTEMPTED"),
            fully_successful=True,
            primary_successful=True,
        )

    # 3. Check if all target platforms already succeeded or are UNKNOWN
    fb_already_done = not target_fb or request.existing_facebook_status in ("SUCCESS", "ACCEPTED", "UNKNOWN")
    ig_already_done = not target_ig or request.existing_instagram_status in ("SUCCESS", "ACCEPTED", "UNKNOWN", "NOT_CONFIGURED")

    if fb_already_done and ig_already_done:
        fb_status = request.existing_facebook_status if target_fb else "NOT_ATTEMPTED"
        ig_status = request.existing_instagram_status if target_ig else "NOT_ATTEMPTED"
        fb_ok = (not target_fb) or (fb_status in ("SUCCESS", "ACCEPTED"))
        ig_ok = (not target_ig) or (ig_status in ("SUCCESS", "ACCEPTED", "NOT_CONFIGURED"))
        return PublishResult(
            facebook=PlatformPublishResult(
                platform="facebook",
                status=fb_status,
                media_id=request.existing_facebook_media_id,
            ),
            instagram=PlatformPublishResult(
                platform="instagram",
                status=ig_status,
                media_id=request.existing_instagram_media_id,
            ),
            fully_successful=fb_ok and ig_ok,
            primary_successful=fb_ok if target_fb else ig_ok,
        )

    # 4. Generate fresh temporary presigned GET URL from B2
    try:
        video_presigned_url = create_presigned_get_url(
            client=s3_client,
            b2_config=storage_config,
            media_key=request.media_key,
            expires_seconds=21600,  # 6 hours
        )
    except Exception as err:
        gen_err = f"Failed to generate temporary presigned URL for media key: {err}"
        return PublishResult(
            facebook=PlatformPublishResult(
                platform="facebook",
                status="FAILED" if target_fb else "NOT_ATTEMPTED",
                failure_reason=gen_err,
            ),
            instagram=PlatformPublishResult(
                platform="instagram",
                status="FAILED" if target_ig else "NOT_ATTEMPTED",
                failure_reason=gen_err,
            ),
            fully_successful=False,
            primary_successful=False,
        )

    # 5. Publish to Facebook PRIMARY first
    if target_fb:
        fb_result = publish_facebook_reel(
            request,
            meta_config,
            video_presigned_url,
            http_post_fn=http_post_fn,
            http_get_fn=http_get_fn,
        )
    else:
        fb_result = PlatformPublishResult(platform="facebook", status="NOT_ATTEMPTED")

    # Immediate Facebook Checkpoint: persist Facebook status to R2 before attempting Instagram
    if on_checkpoint is not None:
        on_checkpoint("facebook", fb_result)

    # 6. Publish to Instagram SECONDARY second
    if target_ig:
        ig_result = publish_instagram_reel(
            request,
            meta_config,
            video_presigned_url,
            http_post_fn=http_post_fn,
            http_get_fn=http_get_fn,
            sleep_fn=sleep_fn,
            poll_interval_seconds=poll_interval_seconds,
            max_wait_seconds=max_wait_seconds,
        )
    else:
        ig_result = PlatformPublishResult(platform="instagram", status="NOT_ATTEMPTED")

    # Immediate Instagram Checkpoint: persist Instagram status to R2
    if on_checkpoint is not None:
        on_checkpoint("instagram", ig_result)

    # 7. Evaluate aggregate success (Facebook is PRIMARY, Instagram is OPTIONAL)
    fb_ok = (not target_fb) or (fb_result.status == "SUCCESS")
    ig_ok = (not target_ig) or (ig_result.status == "SUCCESS")
    fully_successful = fb_ok and ig_ok
    primary_successful = fb_ok if target_fb else ig_ok

    return PublishResult(
        facebook=fb_result,
        instagram=ig_result,
        fully_successful=fully_successful,
        primary_successful=primary_successful,
    )
