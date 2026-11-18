"""Buffer GraphQL adapter for one Facebook Reel per final-safe RunRecord.

The adapter intentionally knows nothing about Quran selection, MediaStore
credentials, or social scheduling.  It receives a stable public delivery URL
only after the caller has durably recorded final safety success.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

import requests

from engine.alignments import normalized_sha256
from engine.buffer_lifecycle import ReconciliationResult
from engine.publisher import (
    CANONICAL_SURAH_NAMES,
    RECITER_ATTRIBUTION_AR,
    RECITER_ATTRIBUTION_EN,
    RECITER_HASHTAG,
    PlatformPublishResult,
    PublishResult,
)
from engine.storage import RunRecord
from engine.translation import EXPECTED_TRANSLATION_VERSIONS, SUPPORTED_TRANSLATION_SOURCES


BUFFER_API_URL = "https://api.buffer.com"
BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS = 5000
BUFFER_MAX_ATTEMPTS = 3
BUFFER_RETRY_DELAYS_SECONDS = (0.0, 2.0, 5.0)
BUFFER_BACKEND = "buffer"
TRANSLATION_CREDIT_LABELS = {"en": "English", "de": "German"}


class BufferPublisherError(Exception):
    """A safe, classified Buffer integration failure."""

    def __init__(
        self, code: str, message: str, *, retryable: bool = False, possibly_dispatched: bool = False
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.possibly_dispatched = possibly_dispatched
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class BufferConfig:
    api_key: str
    organization_id: Optional[str] = None
    facebook_channel_id: Optional[str] = None
    instagram_channel_id: Optional[str] = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise BufferPublisherError("AUTH_ERROR", "BUFFER_API_KEY is required.")
        if self.timeout_seconds <= 0:
            raise BufferPublisherError("BUFFER_INVALID_POST", "Buffer timeout must be positive.")


@dataclass(frozen=True)
class BufferOrganization:
    id: str
    name: str


@dataclass(frozen=True)
class BufferChannel:
    id: str
    display_name: str
    service: str
    descriptor: str = ""
    channel_type: str = ""
    external_link: Optional[str] = None
    is_disconnected: bool = False
    is_locked: bool = False

    @property
    def is_facebook_page(self) -> bool:
        descriptor = self.descriptor.strip().lower()
        channel_type = self.channel_type.strip().lower()
        return self.service.strip().lower() == "facebook" and (
            descriptor == "facebook page" or channel_type == "page"
        )

    @property
    def is_instagram(self) -> bool:
        """Whether this read-only Buffer channel is an Instagram account."""
        return self.service.strip().lower() == "instagram"

    @property
    def is_available(self) -> bool:
        """Whether Buffer reports the connected channel as usable."""
        return not self.is_disconnected and not self.is_locked


@dataclass(frozen=True)
class BufferPost:
    id: str
    status: Optional[str] = None
    external_link: Optional[str] = None


@dataclass(frozen=True)
class BufferPublishRequest:
    run_id: str
    caption: str
    public_media_url: str
    save_to_draft: bool = False
    source: Optional[str] = None
    editorial_record: Any = None


def _env_true(source: Mapping[str, str], name: str) -> bool:
    return source.get(name, "false").strip().lower() in {"true", "1", "yes"}


def buffer_facebook_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    source = os.environ if env is None else env
    return (
        source.get("PUBLISHER_BACKEND", "").strip().lower() == BUFFER_BACKEND
        and _env_true(source, "BUFFER_PUBLISHING_ENABLED")
        and _env_true(source, "BUFFER_FACEBOOK_ENABLED")
    )


def buffer_instagram_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    source = os.environ if env is None else env
    return (
        source.get("PUBLISHER_BACKEND", "").strip().lower() == BUFFER_BACKEND
        and _env_true(source, "BUFFER_PUBLISHING_ENABLED")
        and _env_true(source, "BUFFER_INSTAGRAM_ENABLED")
    )


def buffer_publishing_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return true only for explicitly selected and explicitly enabled Buffer."""
    source = os.environ if env is None else env
    return buffer_facebook_enabled(source) or buffer_instagram_enabled(source)


def load_buffer_config_from_env(env: Optional[Mapping[str, str]] = None) -> BufferConfig:
    source = os.environ if env is None else env
    return BufferConfig(
        api_key=source.get("BUFFER_API_KEY", "").strip(),
        organization_id=source.get("BUFFER_ORGANIZATION_ID", "").strip() or None,
        facebook_channel_id=source.get("BUFFER_FACEBOOK_CHANNEL_ID", "").strip() or None,
        instagram_channel_id=source.get("BUFFER_INSTAGRAM_CHANNEL_ID", "").strip() or None,
        timeout_seconds=float(source.get("BUFFER_API_TIMEOUT_SECONDS", "30")),
    )


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _hashtag_safe_surah_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9]", "", ascii_name)
    if not normalized:
        raise BufferPublisherError("BUFFER_INVALID_POST", "Surah name cannot form a hashtag.")
    return normalized


def build_buffer_hashtags(surah_name_en: str, first_ayah: int, last_ayah: int) -> tuple[str, ...]:
    """Return the fixed six-hashtag policy, deterministically ordered."""
    return (
        "#Quran",
        "#QuranDaily",
        "#Ayah",
        f"#{_hashtag_safe_surah_name(surah_name_en)}",
        "#QuranVerse" if first_ayah == last_ayah else "#QuranVerses",
        RECITER_HASHTAG,
    )


def _caption_ayahs_from_record(record: RunRecord) -> tuple[Mapping[str, Any], ...]:
    raw = record.provenance.get("caption_ayahs") if isinstance(record.provenance, Mapping) else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise BufferPublisherError(
            "BUFFER_INVALID_POST",
            "RunRecord lacks source-bound caption_ayahs provenance.",
        )
    if record.surah_number is None or record.first_ayah is None or record.last_ayah is None:
        raise BufferPublisherError("BUFFER_INVALID_POST", "RunRecord has incomplete ayah reference metadata.")
    expected_keys = [f"{record.surah_number}:{ayah}" for ayah in range(record.first_ayah, record.last_ayah + 1)]
    if len(raw) != len(expected_keys):
        raise BufferPublisherError("BUFFER_INVALID_POST", "Caption provenance ayah count is inconsistent.")
    ayahs: list[Mapping[str, Any]] = []
    for expected_key, item in zip(expected_keys, raw):
        if not isinstance(item, Mapping):
            raise BufferPublisherError("BUFFER_INVALID_POST", "Caption provenance entry is malformed.")
        if item.get("verse_key") != expected_key:
            raise BufferPublisherError("BUFFER_INVALID_POST", "Caption provenance ayah order is inconsistent.")
        arabic = item.get("arabic_text")
        arabic_hash = item.get("arabic_normalized_sha256")
        translations = item.get("translations")
        if not isinstance(arabic, str) or not arabic.strip() or arabic_hash != normalized_sha256(arabic):
            raise BufferPublisherError("BUFFER_INVALID_POST", "Caption Arabic source hash validation failed.")
        if not isinstance(translations, Mapping) or "en" not in translations:
            raise BufferPublisherError("BUFFER_INVALID_POST", "Caption requires exact English Rowwad source data.")
        if ayahs and translations.keys() != ayahs[0]["translations"].keys():
            raise BufferPublisherError("BUFFER_INVALID_POST", "Caption translation languages differ between ayahs.")
        for language, source in translations.items():
            key = SUPPORTED_TRANSLATION_SOURCES.get(language)
            if language not in TRANSLATION_CREDIT_LABELS or key is None or not isinstance(source, Mapping):
                raise BufferPublisherError("BUFFER_INVALID_POST", "Unsupported caption translation source.")
            text = source.get("text")
            if (not isinstance(text, str) or not text.strip() or source.get("translation_key") != key
                    or source.get("translation_version") != EXPECTED_TRANSLATION_VERSIONS[key]
                    or source.get("normalized_sha256") != normalized_sha256(text)):
                raise BufferPublisherError("BUFFER_INVALID_POST", f"Caption {TRANSLATION_CREDIT_LABELS[language]} Rowwad source validation failed.")
        ayahs.append(item)
    return tuple(ayahs)


def build_buffer_facebook_caption(record: RunRecord) -> str:
    """Build the exact-caption contract from source-bound rendering provenance."""
    ayahs = _caption_ayahs_from_record(record)
    assert record.surah_number is not None
    assert record.first_ayah is not None
    assert record.last_ayah is not None
    names = CANONICAL_SURAH_NAMES.get(record.surah_number)
    surah_ar = record.surah_name_ar or (names[0] if names else None)
    surah_en = record.surah_name_en or (names[1] if names else None)
    if not surah_ar or not surah_en:
        raise BufferPublisherError("BUFFER_INVALID_POST", "RunRecord lacks canonical Surah names.")

    is_single = record.first_ayah == record.last_ayah
    ayah_reference = str(record.first_ayah) if is_single else f"{record.first_ayah}–{record.last_ayah}"
    arabic_label = "الآية" if is_single else "الآيات"
    english_label = "Ayah" if is_single else "Ayahs"
    arabic_text = "\n".join(str(item["arabic_text"]) for item in ayahs)
    english_text = "\n".join(str(item["translations"]["en"]["text"]) for item in ayahs)
    hashtags = build_buffer_hashtags(surah_en, record.first_ayah, record.last_ayah)
    sources = ayahs[0]["translations"]
    editions = ", ".join(
        f"{label} {sources[language]['translation_version']}"
        for language, label in TRANSLATION_CREDIT_LABELS.items()
        if language in sources
    )
    # Presentation quotes follow source validation and never enter source hashes.
    caption = (
        f"سورة {surah_ar} — {arabic_label} {ayah_reference}\n\n"
        f"«{arabic_text}»\n\n"
        f"{RECITER_ATTRIBUTION_AR}\n\n\n"
        f"{surah_en}\n\n"
        f"“{english_text}”\n\n"
        f"{RECITER_ATTRIBUTION_EN}\n\n"
        f"{english_label} {ayah_reference}\n\n"
        "Arabic text: Quran Foundation\n"
        f"Translation: Rowwad Translation Center via QuranEnc.com ({editions})\n\n"
        f"{' '.join(hashtags)}"
    )
    if _utf16_units(caption) > BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS:
        raise BufferPublisherError(
            "CAPTION_TOO_LONG_FOR_FACEBOOK",
            f"Caption is {_utf16_units(caption)} UTF-16 units; Buffer Facebook limit is "
            f"{BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS}.",
        )
    return caption


def validate_stable_public_media_url(value: str) -> str:
    """Require an HTTPS, direct, non-presigned delivery URL for Buffer."""
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception as err:  # pragma: no cover - urlparse is defensive
        raise BufferPublisherError("MEDIA_URL_UNREACHABLE", "Public media URL is invalid.") from err
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BufferPublisherError("MEDIA_URL_UNREACHABLE", "Public media URL must be direct HTTPS.")
    query = parsed.query.lower()
    if "x-amz-signature" in query or "x-amz-credential" in query:
        raise BufferPublisherError("MEDIA_URL_UNREACHABLE", "Expiring presigned media URLs are not supported.")
    if not parsed.path.endswith(".mp4"):
        raise BufferPublisherError("MEDIA_URL_UNREACHABLE", "Public media URL must address an MP4.")
    return value


class BufferClient:
    """Small GraphQL client with classified, bounded transport retry."""

    def __init__(
        self,
        config: BufferConfig,
        *,
        http_post: Optional[Callable[..., Any]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._http_post = http_post or requests.post
        self._sleep = sleep_fn

    def _post_once(
        self, query: str, variables: Optional[Mapping[str, Any]], *, write: bool = False
    ) -> Mapping[str, Any]:
        try:
            response = self._http_post(
                BUFFER_API_URL,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": dict(variables or {})},
                timeout=self.config.timeout_seconds,
            )
        except (requests.Timeout, requests.ConnectionError, TimeoutError, OSError) as err:
            if write:
                raise BufferPublisherError(
                    "BUFFER_PUBLISH_OUTCOME_UNKNOWN",
                    "Buffer write outcome is unknown after a transport failure.",
                    possibly_dispatched=True,
                ) from err
            raise BufferPublisherError("BUFFER_TRANSIENT_ERROR", "Buffer network request failed.", retryable=True) from err

        status_code = getattr(response, "status_code", 200)
        if status_code in (401, 403):
            raise BufferPublisherError("AUTH_ERROR", "Buffer authentication was rejected.")
        if status_code == 429:
            raise BufferPublisherError("BUFFER_RATE_LIMITED", "Buffer rate limit reached.", retryable=True)
        if status_code in (500, 502, 503, 504):
            if write:
                raise BufferPublisherError(
                    "BUFFER_PUBLISH_OUTCOME_UNKNOWN",
                    "Buffer write outcome is unknown after a gateway failure.",
                    possibly_dispatched=True,
                )
            raise BufferPublisherError("BUFFER_TRANSIENT_ERROR", f"Buffer HTTP {status_code}.", retryable=True)
        if status_code < 200 or status_code >= 300:
            raise BufferPublisherError("BUFFER_PUBLISH_FAILED", f"Buffer HTTP {status_code}.")
        try:
            payload = response if isinstance(response, Mapping) else response.json()
        except Exception as err:
            if write:
                raise BufferPublisherError(
                    "BUFFER_PUBLISH_OUTCOME_UNKNOWN",
                    "Buffer write outcome is unknown because its response was unreadable.",
                    possibly_dispatched=True,
                ) from err
            raise BufferPublisherError("BUFFER_TRANSIENT_ERROR", "Buffer returned unreadable JSON.", retryable=True) from err
        if not isinstance(payload, Mapping):
            raise BufferPublisherError("BUFFER_PUBLISH_FAILED", "Buffer returned an invalid response.")
        graph_errors = payload.get("errors")
        if graph_errors:
            first = graph_errors[0] if isinstance(graph_errors, Sequence) and graph_errors else {}
            extensions = first.get("extensions") if isinstance(first, Mapping) else {}
            code = str(extensions.get("code", "")).upper() if isinstance(extensions, Mapping) else ""
            error_msg = str(first.get("message") or "")
            if code in {"UNAUTHORIZED", "FORBIDDEN", "AUTHENTICATION_ERROR"}:
                raise BufferPublisherError("AUTH_ERROR", f"Buffer authentication was rejected: {error_msg}")
            if code in {"RATE_LIMITED", "TOO_MANY_REQUESTS"}:
                raise BufferPublisherError("BUFFER_RATE_LIMITED", f"Buffer rate limit reached: {error_msg}", retryable=True)
            if code in {"INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE", "TIMEOUT"}:
                if write:
                    raise BufferPublisherError(
                        "BUFFER_PUBLISH_OUTCOME_UNKNOWN",
                        f"Buffer write outcome is unknown after a temporary GraphQL failure: {error_msg}",
                        possibly_dispatched=True,
                    )
                raise BufferPublisherError("BUFFER_TRANSIENT_ERROR", f"Buffer temporary GraphQL error: {error_msg}", retryable=True)
            raise BufferPublisherError("BUFFER_PUBLISH_FAILED", f"Buffer GraphQL request failed: {error_msg}")
        return payload

    def graphql(self, query: str, variables: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        last_error: Optional[BufferPublisherError] = None
        for attempt in range(BUFFER_MAX_ATTEMPTS):
            if attempt:
                self._sleep(BUFFER_RETRY_DELAYS_SECONDS[attempt])
            try:
                return self._post_once(query, variables, write=False)
            except BufferPublisherError as err:
                last_error = err
                if not err.retryable or attempt + 1 >= BUFFER_MAX_ATTEMPTS:
                    raise
        assert last_error is not None  # pragma: no cover - loop always returns/raises
        raise last_error

    def organizations(self) -> tuple[BufferOrganization, ...]:
        payload = self.graphql(
            "query BufferOrganizations { account { organizations { id name } } }"
        )
        account = payload.get("data", {}).get("account", {}) if isinstance(payload.get("data"), Mapping) else {}
        raw = account.get("organizations", []) if isinstance(account, Mapping) else []
        organizations = tuple(
            BufferOrganization(id=str(item.get("id", "")), name=str(item.get("name", "")))
            for item in raw if isinstance(item, Mapping) and item.get("id")
        )
        if not organizations:
            raise BufferPublisherError("CHANNEL_NOT_FOUND", "Buffer account has no accessible organizations.")
        return organizations

    def resolve_organization(self) -> BufferOrganization:
        """Resolve the configured organization, or the sole accessible one."""
        organizations = self.organizations()
        if self.config.organization_id:
            matched = [org for org in organizations if org.id == self.config.organization_id]
            if len(matched) != 1:
                raise BufferPublisherError("CHANNEL_NOT_FOUND", "Configured Buffer organization was not found.")
            return matched[0]
        if len(organizations) != 1:
            raise BufferPublisherError("CHANNEL_NOT_FOUND", "Multiple Buffer organizations require BUFFER_ORGANIZATION_ID.")
        return organizations[0]

    def channels(self, organization_id: str) -> tuple[BufferChannel, ...]:
        payload = self.graphql(
            "query BufferChannels($organizationId: OrganizationId!) { "
            "channels(input: { organizationId: $organizationId }) { "
            "id name displayName service descriptor type isDisconnected isLocked externalLink } }",
            {"organizationId": organization_id},
        )
        data = payload.get("data", {}) if isinstance(payload.get("data"), Mapping) else {}
        raw = data.get("channels", []) if isinstance(data, Mapping) else []
        return tuple(
            BufferChannel(
                id=str(item.get("id", "")),
                display_name=str(item.get("displayName") or item.get("name") or ""),
                service=str(item.get("service", "")),
                descriptor=str(item.get("descriptor") or ""),
                channel_type=str(item.get("type") or ""),
                external_link=str(item["externalLink"]) if item.get("externalLink") else None,
                is_disconnected=bool(item.get("isDisconnected")),
                is_locked=bool(item.get("isLocked")),
            )
            for item in raw if isinstance(item, Mapping) and item.get("id")
        )

    def read_connected_channels(self) -> tuple[BufferOrganization, tuple[BufferChannel, ...]]:
        """Read all Buffer channels for the selected organization without mutation."""
        organization = self.resolve_organization()
        return organization, self.channels(organization.id)

    def select_facebook_channel(self, channels: Sequence[BufferChannel]) -> BufferChannel:
        """Select the one configured/intended Facebook Page from an already-read list."""
        candidates = [channel for channel in channels if channel.is_facebook_page]
        if self.config.facebook_channel_id:
            candidates = [channel for channel in candidates if channel.id == self.config.facebook_channel_id]
            if not candidates:
                raise BufferPublisherError("CHANNEL_NOT_FOUND", "Configured Buffer Facebook Page channel was not found.")
        if not candidates:
            raise BufferPublisherError("CHANNEL_NOT_FACEBOOK", "No Facebook Page channel is connected in Buffer.")
        if len(candidates) > 1:
            names = ", ".join(channel.display_name or channel.id for channel in candidates)
            raise BufferPublisherError("CHANNEL_NOT_FOUND", f"Multiple Facebook Page channels require explicit selection: {names}")
        channel = candidates[0]
        if not channel.is_available:
            raise BufferPublisherError("CHANNEL_UNAVAILABLE", "Configured Buffer Facebook Page channel is unavailable.")
        return channel

    def select_instagram_channel(self, channels: Sequence[BufferChannel]) -> BufferChannel:
        """Select one connected Instagram account for read-only readiness validation."""
        candidates = [channel for channel in channels if channel.is_instagram]
        if self.config.instagram_channel_id:
            candidates = [channel for channel in candidates if channel.id == self.config.instagram_channel_id]
        if not candidates:
            raise BufferPublisherError("INSTAGRAM_CHANNEL_NOT_VISIBLE", "No Instagram channel is visible to Buffer.")
        if len(candidates) > 1:
            raise BufferPublisherError(
                "MULTIPLE_INSTAGRAM_CHANNELS",
                "Multiple Instagram channels require explicit selection.",
            )
        channel = candidates[0]
        if not channel.is_available:
            raise BufferPublisherError("INSTAGRAM_CHANNEL_UNAVAILABLE", "Instagram channel is disconnected or locked.")
        return channel

    def resolve_facebook_channel(self) -> tuple[BufferOrganization, BufferChannel]:
        organization, channels = self.read_connected_channels()
        return organization, self.select_facebook_channel(channels)

    def resolve_instagram_channel(self) -> tuple[BufferOrganization, BufferChannel]:
        organization, channels = self.read_connected_channels()
        return organization, self.select_instagram_channel(channels)

    def prepare_owner_v1_targets(
        self, record: RunRecord, *, stored_config: Any, env: Mapping[str, str],
    ) -> tuple[BufferChannel, BufferChannel, BufferPublishRequest]:
        """Read-only preflight for the same final-safe Reel on both Buffer channels.

        Does not claim, publish, save a run, or treat a Buffer ACK as delivery.
        """
        from engine.calm_reels_dispatch import require_owner_v1_dispatch_record
        from engine.public_media_delivery import ensure_public_media_delivery, PublicMediaDeliveryError
        from engine.publisher import check_reciter_reuse_rights

        if (env.get("PUBLISHER_BACKEND", "").strip().lower() != BUFFER_BACKEND
            or not buffer_facebook_enabled(env) or not buffer_instagram_enabled(env)
            or not getattr(stored_config, "facebook_enabled", False)
            or not getattr(stored_config, "instagram_enabled", False)
            or not self.config.facebook_channel_id or not self.config.instagram_channel_id):
            raise BufferPublisherError("OWNER_V1_CHANNELS_NOT_READY", "Both intended Buffer channels must be explicitly enabled.")
        if not check_reciter_reuse_rights(
            getattr(stored_config, "reciter_reuse_confirmed", False), env=env,
        ):
            raise BufferPublisherError("OWNER_V1_RIGHTS_NOT_CONFIRMED", "Reciter reuse rights are not confirmed.")

        require_owner_v1_dispatch_record(record)
        if not record.public_media_delivery_id:
            raise BufferPublisherError("OWNER_V1_INVALID_MEDIA", "Public final-safe Reel identity is required.")
        try:
            verified, public_url = ensure_public_media_delivery(record, env)
        except PublicMediaDeliveryError as error:
            raise BufferPublisherError("OWNER_V1_INVALID_MEDIA", "Final-safe public delivery is unavailable.") from error
        if verified != record:
            raise BufferPublisherError("OWNER_V1_INVALID_MEDIA", "Reel delivery identity changed.")

        _organization, channels = self.read_connected_channels()
        facebook = self.select_facebook_channel(channels)
        instagram = self.select_instagram_channel(channels)
        if facebook.id == instagram.id:
            raise BufferPublisherError("OWNER_V1_CHANNELS_NOT_READY", "Facebook and Instagram must have distinct Buffer targets.")
        request = BufferPublishRequest(
            run_id=record.run_id,
            caption=build_buffer_facebook_caption(record),
            public_media_url=public_url,
            editorial_record=record,
        )
        return facebook, instagram, request

    def submit_owner_v1_pair(
        self, record: RunRecord, *, store: Any, stored_config: Any, env: Mapping[str, str],
    ) -> RunRecord:
        """One-shot Buffer send to both selected channels; no platform retry.

        The verified first-pending Supabase claim is acquired before Facebook.
        Persist the Facebook Buffer acknowledgement before attempting Instagram.
        Neither acknowledgement is a confirmed published Facebook receipt.
        """
        from dataclasses import replace
        from datetime import datetime, timezone
        from engine.buffer_lifecycle import (
            BufferPublicationCoordinator, publication_fingerprint, publication_source,
        )

        if (env.get("GITHUB_ACTIONS", "").strip().lower() != "true"
            or env.get("STATE_AUTHORITY", "").strip().lower() != "supabase"):
            raise BufferPublisherError(
                "OWNER_V1_GITHUB_SUPABASE_REQUIRED",
                "Owner-v1 public Buffer send is restricted to GitHub Actions and Supabase.",
            )
        required = (
            "get_run", "save_run", "claim_calm_reels_v1_next_ayah",
            "claim_buffer_publication_dispatch", "transition_buffer_publication_claim",
        )
        if any(not callable(getattr(store, name, None)) for name in required):
            raise BufferPublisherError("OWNER_V1_STORE_UNAVAILABLE", "Durable owner-v1 state is required.")
        if store.get_run(record.run_id) != record:
            raise BufferPublisherError("OWNER_V1_SAVED_RUN_CHANGED", "Final-safe run changed before publication.")
        facebook, instagram, request = self.prepare_owner_v1_targets(
            record, stored_config=stored_config, env=env,
        )
        # The existing Facebook method validates the source again, obtains the
        # exclusive first-pending DB claim and performs exactly one Buffer write.
        fb_post = self.create_owner_v1_facebook_reel(
            facebook.id, request, store=store, stored_config=stored_config, env=env,
        )
        accepted_at = datetime.now(timezone.utc).isoformat()
        accepted_fb = replace(
            record, publisher_backend=BUFFER_BACKEND,
            stage="STAGE_BUFFER_PUBLISHING_ACCEPTED",
            facebook_status="ACCEPTED", facebook_media_id=fb_post.id,
            buffer_post_id=fb_post.id, buffer_post_status=fb_post.status,
            buffer_accepted_at=accepted_at, facebook_reel_url=fb_post.external_link,
            facebook_failure_reason=None,
        )
        try:
            store.save_run(accepted_fb)
        except Exception as error:
            # The Facebook write may already be public. Never send Instagram
            # or retry Facebook until the saved state is reconciled.
            raise BufferPublisherError(
                "OWNER_V1_FACEBOOK_ACK_CHECKPOINT_UNKNOWN",
                "Facebook Buffer acknowledgement could not be durably saved.",
                possibly_dispatched=True,
            ) from error

        fingerprint = publication_fingerprint(
            run_id=record.run_id, platform="instagram", channel_id=instagram.id,
            media_identity=str(record.provenance.get("media_sha256") or record.media_key),
            caption=request.caption,
        )
        source = publication_source(record.run_id, fingerprint)
        coordinator = BufferPublicationCoordinator(
            store, platform="instagram", publisher_backend=BUFFER_BACKEND,
            channel_id=instagram.id,
        )
        try:
            claim = coordinator.claim(run_id=record.run_id, fingerprint=fingerprint, source=source)
        except Exception as error:
            raise BufferPublisherError(
                "OWNER_V1_INSTAGRAM_CLAIM_UNKNOWN", "Instagram dispatch claim could not be verified.",
            ) from error
        if claim.get("_claim_created") is not True:
            raise BufferPublisherError(
                "OWNER_V1_INSTAGRAM_NO_REDISPATCH",
                "A prior Instagram dispatch may already exist; reconcile instead of retrying.",
            )
        try:
            ig_post = self._create_reel_after_policy(instagram.id, "instagram", request)
        except Exception as error:
            # Even an unreadable provider result can follow a successful send.
            try:
                coordinator.mark_unknown_outcome(record.run_id, "OWNER_V1_INSTAGRAM_SEND_UNKNOWN")
                store.save_run(replace(
                    accepted_fb, instagram_status="UNKNOWN",
                    instagram_failure_reason="OWNER_V1_INSTAGRAM_SEND_UNKNOWN",
                ))
            except Exception:
                pass
            raise BufferPublisherError(
                "OWNER_V1_INSTAGRAM_SEND_UNKNOWN",
                "Instagram Buffer delivery requires reconciliation; do not retry.",
                possibly_dispatched=True,
            ) from error
        try:
            coordinator.mark_success(
                record.run_id, post_id=ig_post.id, external_link=ig_post.external_link,
                accepted_at=datetime.now(timezone.utc).isoformat(),
                buffer_post_status=ig_post.status,
            )
            accepted_both = replace(
                accepted_fb, instagram_status="ACCEPTED",
                instagram_media_id=ig_post.id, instagram_failure_reason=None,
            )
            store.save_run(accepted_both)
        except Exception as error:
            raise BufferPublisherError(
                "OWNER_V1_INSTAGRAM_ACK_CHECKPOINT_UNKNOWN",
                "Instagram Buffer acknowledgement could not be durably saved.",
                possibly_dispatched=True,
            ) from error
        return accepted_both

    def create_facebook_reel(self, channel_id: str, request: BufferPublishRequest) -> BufferPost:
        return self.create_reel(channel_id, "facebook", request)

    def create_instagram_reel(self, channel_id: str, request: BufferPublishRequest) -> BufferPost:
        return self.create_reel(channel_id, "instagram", request)

    def create_reel(self, channel_id: str, platform: str, request: BufferPublishRequest) -> BufferPost:
        """Create one Buffer Reel for a target-scoped platform.

        The external mutation remains one-shot.  The caller owns durable
        target-specific idempotency; no generic HTTP retry may wrap this call.
        """
        if not request.save_to_draft:
            from engine.reel_publication_policy import require_publication_request
            from engine.reel_theme_policy import EditorialPolicyError
            try:
                require_publication_request(request.editorial_record, request.caption, backend="buffer")
            except EditorialPolicyError as err:
                raise BufferPublisherError("THEME_POLICY_REJECTED", str(err)) from err
        return self._create_reel_after_policy(channel_id, platform, request)

    def create_owner_v1_facebook_reel(
        self,
        channel_id: str,
        request: BufferPublishRequest,
        *,
        store: Any,
        stored_config: Any,
        env: Mapping[str, str],
    ) -> BufferPost:
        """One claimed v1 Buffer send; never equate acceptance with delivery.

        The old direct create_reel remains protected by the legacy editorial
        policy. This narrow owner-v1 path first verifies source/safety,
        resolves the intended Facebook Page, atomically claims the first
        pending owner ayah, and THEN invokes the one-shot Buffer mutation.
        An uncertain outcome must be reconciled, not retried.
        """
        from engine.calm_reels_dispatch import claim_owner_v1_before_facebook_dispatch

        if request.save_to_draft:
            raise BufferPublisherError("OWNER_V1_INVALID_TARGET", "V1 publication cannot create drafts.")
        record = request.editorial_record
        if not isinstance(record, RunRecord) or request.run_id != record.run_id:
            raise BufferPublisherError("OWNER_V1_INVALID_TARGET", "Run identity does not match the final Reel.")
        if request.caption != build_buffer_facebook_caption(record):
            raise BufferPublisherError("OWNER_V1_INVALID_CAPTION", "Caption must match final source evidence.")
        validate_stable_public_media_url(request.public_media_url)
        # The public MP4 URL must be the final-safe RunRecord's own immutable
        # delivery capability, never another caller-supplied video URL.
        from engine.public_media_delivery import (
            PublicMediaDeliveryError, ensure_public_media_delivery,
        )
        if not record.public_media_delivery_id:
            raise BufferPublisherError(
                "OWNER_V1_INVALID_MEDIA", "Final Reel lacks a persisted public delivery identity.",
            )
        try:
            verified_record, expected_url = ensure_public_media_delivery(record, env)
        except PublicMediaDeliveryError as error:
            raise BufferPublisherError(
                "OWNER_V1_INVALID_MEDIA", "Stable public MP4 identity is unavailable.",
            ) from error
        if verified_record != record or request.public_media_url != expected_url:
            raise BufferPublisherError(
                "OWNER_V1_INVALID_MEDIA", "Public MP4 URL does not match the final Reel.",
            )
        # Channel resolution is read-only; verify Page type BEFORE any claim.
        _organization, page = self.resolve_facebook_channel()
        if page.id != channel_id:
            raise BufferPublisherError("OWNER_V1_INVALID_TARGET", "Facebook Page identity mismatch.")
        claim_owner_v1_before_facebook_dispatch(
            record, store=store, stored_config=stored_config, env=env,
        )
        # A successful Buffer post is only an asynchronous acceptance. The
        # immutable Supabase claim remains CLAIMED until separately verified
        # Facebook delivery is written by an authenticated evidence flow.
        return self._create_reel_after_policy(channel_id, "facebook", request)

    def _create_reel_after_policy(
        self, channel_id: str, platform: str, request: BufferPublishRequest,
    ) -> BufferPost:
        """Internal, one-shot transport used only after a publication gate."""
        validate_stable_public_media_url(request.public_media_url)
        if platform not in {"facebook", "instagram"}:
            raise BufferPublisherError("BUFFER_INVALID_POST", "Unsupported Buffer Reel platform.")
        metadata: dict[str, Any]
        if platform == "instagram":
            metadata = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        else:
            metadata = {platform: {"type": "reel"}}
        input_payload = {
            "channelId": channel_id,
            "text": request.caption,
            "schedulingType": "automatic",
            "mode": "addToQueue" if request.save_to_draft else "shareNow",
            "saveToDraft": request.save_to_draft,
            "assets": [{"video": {"url": request.public_media_url}}],
            "metadata": metadata,
            "source": request.source or f"quran-reels:{request.run_id}",
        }
        # A createPost can succeed even if the caller times out waiting for its
        # response.  It intentionally bypasses the retrying read-only helper.
        payload = self._post_once(
            "mutation CreateBufferReel($input: CreatePostInput!) { "
            "createPost(input: $input) { "
            "... on PostActionSuccess { post { id status externalLink text } } "
            "... on MutationError { message } } }",
            {"input": input_payload},
            write=True,
        )
        result = payload.get("data", {}).get("createPost") if isinstance(payload.get("data"), Mapping) else None
        if not isinstance(result, Mapping):
            raise BufferPublisherError("BUFFER_PUBLISH_FAILED", f"Buffer did not return createPost data: {payload}")
        post = result.get("post")
        if not isinstance(post, Mapping) or not post.get("id"):
            mutation_msg = result.get("message") or str(result)
            raise BufferPublisherError("BUFFER_INVALID_POST", f"Buffer rejected {platform} Reel input: {mutation_msg}")
        return BufferPost(
            id=str(post["id"]),
            status=str(post["status"]) if post.get("status") else None,
            external_link=str(post["externalLink"]) if post.get("externalLink") else None,
        )

    def reconcile_publication(
        self,
        organization_id: str,
        channel_id: str,
        caption: str,
        public_media_url: str,
        *,
        not_before: Optional[str] = None,
        drafts_only: bool = False,
    ) -> ReconciliationResult:
        """Read Buffer post history to resolve an interrupted logical write.

        ``CreatePostInput.source`` is only documented as composer tracking
        metadata, not as a retrievable Post field.  Reconciliation therefore
        uses only documented Post fields: target ``channelId``, exact text,
        and the returned asset ``source`` URL.  The stable delivery URL is a
        run-scoped capability created only after final safety succeeds, so the
        complete triple is a deterministic publication identity.

        Absence is proved only after every cursor page in the bounded claim
        time window has been fetched.  Missing cursors, an API error, or a
        page cap all remain INDETERMINATE and never authorize another write.
        """
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        try:
            for _page in range(100):
                filter_input: dict[str, Any] = {"channelIds": [channel_id]}
                if not_before:
                    filter_input["startDate"] = not_before
                if drafts_only:
                    filter_input["status"] = ["draft"]
                payload = self.graphql(
                    "query ReconcileBufferPublication($first: Int!, $after: String, $input: PostsInput!) { "
                    "posts(first: $first, after: $after, input: $input) { "
                    "edges { node { id channelId text status externalLink createdAt assets { source } } } "
                    "pageInfo { hasNextPage endCursor } } }",
                    {
                        "first": 100,
                        "after": cursor,
                        "input": {
                            "organizationId": organization_id,
                            "filter": filter_input,
                            "sort": [{"field": "createdAt", "direction": "desc"}],
                        },
                    },
                )
                data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
                posts = data.get("posts") if isinstance(data, Mapping) else None
                if not isinstance(posts, Mapping):
                    return ReconciliationResult("INDETERMINATE")
                edges = posts.get("edges")
                if not isinstance(edges, Sequence):
                    return ReconciliationResult("INDETERMINATE")
                for edge in edges:
                    node = edge.get("node") if isinstance(edge, Mapping) else None
                    assets = node.get("assets") if isinstance(node, Mapping) else None
                    has_media = isinstance(assets, Sequence) and any(
                        isinstance(asset, Mapping) and asset.get("source") == public_media_url
                        for asset in assets
                    )
                    if (
                        isinstance(node, Mapping)
                        and str(node.get("channelId")) == channel_id
                        and node.get("text") == caption
                        and has_media
                        and node.get("id")
                    ):
                        return ReconciliationResult(
                        "FOUND", post_id=str(node["id"]),
                        external_link=str(node["externalLink"]) if node.get("externalLink") else None,
                        accepted_at=str(node["createdAt"]) if node.get("createdAt") else None,
                        buffer_post_status=str(node["status"]) if node.get("status") else None,
                        )
                page_info = posts.get("pageInfo")
                if not isinstance(page_info, Mapping):
                    return ReconciliationResult("INDETERMINATE")
                if page_info.get("hasNextPage") is False:
                    return ReconciliationResult("ABSENT")
                next_cursor = page_info.get("endCursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    return ReconciliationResult("INDETERMINATE")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except BufferPublisherError:
            pass
        return ReconciliationResult("INDETERMINATE")

    def delete_post(self, post_id: str) -> None:
        payload = self.graphql(
            "mutation DeleteBufferPost($input: DeletePostInput!) { deletePost(input: $input) { "
            "... on DeletePostSuccess { id } ... on MutationError { message } } }",
            {"input": {"id": post_id}},
        )
        result = payload.get("data", {}).get("deletePost") if isinstance(payload.get("data"), Mapping) else None
        if not isinstance(result, Mapping) or not result.get("id"):
            raise BufferPublisherError("BUFFER_PUBLISH_FAILED", "Buffer did not confirm draft deletion.")


class BufferPublisher:
    """Legacy Buffer facade retained for non-public adapter tests only.

    Public publishing must enter through ``engine.job`` so the Supabase
    target-scoped claim is durable before the Buffer mutation.  This facade
    intentionally refuses an uncoordinated public write rather than leaving
    an alternate duplicate-prone production path.
    """

    def __init__(self, client: BufferClient) -> None:
        self.client = client
        self.last_post: Optional[BufferPost] = None

    def publish_facebook_reel(
        self,
        record: RunRecord,
        *,
        public_media_url: str,
        save_to_draft: bool = False,
    ) -> PublishResult:
        if not save_to_draft:
            return PublishResult(
                facebook=PlatformPublishResult(
                    "facebook",
                    "FAILED",
                    failure_reason="BUFFER_DURABLE_LIFECYCLE_REQUIRED",
                ),
                instagram=PlatformPublishResult("instagram", "NOT_ATTEMPTED"),
                fully_successful=False,
                primary_successful=False,
            )
        if record.final_safety_passed is not True or not record.media_key:
            return PublishResult(
                facebook=PlatformPublishResult("facebook", "FAILED", failure_reason="Final safety-passed media is required before Buffer publishing."),
                instagram=PlatformPublishResult("instagram", "NOT_ATTEMPTED"),
                fully_successful=False,
                primary_successful=False,
            )
        if record.facebook_status == "SUCCESS" and (record.buffer_post_id or record.facebook_media_id):
            existing_id = record.buffer_post_id or record.facebook_media_id
            return PublishResult(
                facebook=PlatformPublishResult("facebook", "SUCCESS", media_id=existing_id),
                instagram=PlatformPublishResult("instagram", "NOT_ATTEMPTED"),
                fully_successful=True,
                primary_successful=True,
            )
        if record.facebook_status == "UNKNOWN":
            return PublishResult(
                facebook=PlatformPublishResult("facebook", "UNKNOWN", failure_reason="A previous Buffer outcome is ambiguous; automatic republish is blocked."),
                instagram=PlatformPublishResult("instagram", "NOT_ATTEMPTED"),
                fully_successful=False,
                primary_successful=False,
            )
        try:
            _, channel = self.client.resolve_facebook_channel()
            post = self.client.create_facebook_reel(
                channel.id,
                BufferPublishRequest(
                    run_id=record.run_id,
                    caption=build_buffer_facebook_caption(record),
                    public_media_url=public_media_url,
                    save_to_draft=save_to_draft,
                ),
            )
            self.last_post = post
            return PublishResult(
                facebook=PlatformPublishResult("facebook", "SUCCESS", media_id=post.id),
                instagram=PlatformPublishResult("instagram", "NOT_ATTEMPTED"),
                fully_successful=True,
                primary_successful=True,
            )
        except BufferPublisherError as err:
            return PublishResult(
                facebook=PlatformPublishResult(
                    "facebook", "UNKNOWN" if err.possibly_dispatched else "FAILED", failure_reason=str(err)
                ),
                instagram=PlatformPublishResult("instagram", "NOT_ATTEMPTED"),
                fully_successful=False,
                primary_successful=False,
            )
