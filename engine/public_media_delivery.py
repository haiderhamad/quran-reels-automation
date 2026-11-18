"""Stable public delivery URLs for final-safe Reel MP4s.

B2 remains entirely private.  A URL resolves through the Dashboard's dedicated
video-only route only when its RunRecord is final-safety passed and carries a
random delivery identifier.  The identifier is a public delivery capability,
not a B2 credential or signed URL, and has no expiry so Buffer can fetch it.
"""

from __future__ import annotations

import os
import secrets
import urllib.parse
from dataclasses import replace
from typing import Mapping, Optional

from engine.storage import RunRecord


class PublicMediaDeliveryError(Exception):
    """Raised when no safe stable public delivery URL can be formed."""


def _validated_origin(env: Mapping[str, str]) -> str:
    raw = env.get("PUBLIC_MEDIA_DELIVERY_ORIGIN", "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise PublicMediaDeliveryError("STABLE_PUBLIC_MEDIA_DELIVERY_REQUIRED")
    return raw


def ensure_public_media_delivery(
    record: RunRecord,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[RunRecord, str]:
    """Return a record with a durable delivery ID and its stable HTTPS URL."""
    source = os.environ if env is None else env
    if record.final_safety_passed is not True or not record.media_key:
        raise PublicMediaDeliveryError("Final safety-passed private MP4 is required for public delivery.")
    origin = _validated_origin(source)
    delivery_id = record.public_media_delivery_id or secrets.token_urlsafe(32)
    updated = record if record.public_media_delivery_id else replace(
        record,
        public_media_delivery_id=delivery_id,
        stage="STAGE_PUBLIC_MEDIA_DELIVERY_READY",
    )
    run_id = urllib.parse.quote(updated.run_id, safe="")
    token = urllib.parse.quote(delivery_id, safe="")
    return updated, f"{origin}/api/public-media/{run_id}/{token}/video.mp4"
