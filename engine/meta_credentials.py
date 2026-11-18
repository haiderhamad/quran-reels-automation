"""Encrypted Meta publishing credentials stored separately from public runtime config."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from engine.publisher import MetaConfig
from engine.storage import B2Config


META_CREDENTIAL_AAD = b"quran-reels/meta-credentials/v1"


class MetaCredentialError(Exception):
    """Raised when encrypted Meta credentials cannot be safely loaded."""


def build_meta_credentials_key(prefix: str) -> str:
    clean = prefix.strip().strip("/")
    base = f"{clean}/" if clean else ""
    return f"{base}config/secrets/meta-credentials.enc"


def _decode_key(raw_key: str) -> bytes:
    value = raw_key.strip()
    try:
        padding = "=" * (-len(value) % 4)
        key = base64.urlsafe_b64decode(value + padding)
    except Exception as err:
        raise MetaCredentialError("META_CREDENTIAL_ENCRYPTION_KEY is invalid.") from err
    if len(key) != 32:
        raise MetaCredentialError("META_CREDENTIAL_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key


def _decode_field(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise MetaCredentialError(f"Encrypted Meta credential envelope is missing {field}.")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as err:
        raise MetaCredentialError(f"Encrypted Meta credential envelope has invalid {field}.") from err


def decrypt_meta_credentials(envelope: Mapping[str, Any], raw_key: str) -> Mapping[str, Any]:
    if envelope.get("format") != "aes-256-gcm" or envelope.get("version") != 1:
        raise MetaCredentialError("Unsupported encrypted Meta credential format.")
    iv = _decode_field(envelope.get("iv"), "iv")
    ciphertext = _decode_field(envelope.get("ciphertext"), "ciphertext")
    tag = _decode_field(envelope.get("tag"), "tag")
    try:
        plaintext = AESGCM(_decode_key(raw_key)).decrypt(iv, ciphertext + tag, META_CREDENTIAL_AAD)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception as err:
        raise MetaCredentialError("Encrypted Meta credentials could not be authenticated or decrypted.") from err
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise MetaCredentialError("Decrypted Meta credentials are malformed.")
    return payload


def load_meta_config_from_envelope(
    envelope_str: str,
    raw_key: str,
    api_version_override: Optional[str] = None,
) -> MetaConfig:
    try:
        envelope = json.loads(envelope_str) if isinstance(envelope_str, str) else envelope_str
        payload = decrypt_meta_credentials(envelope, raw_key)
    except MetaCredentialError:
        raise
    except Exception as err:
        raise MetaCredentialError("Encrypted Meta credentials are malformed.") from err

    page_id = str(payload.get("page_id", "")).strip()
    page_token = str(payload.get("page_access_token", "")).strip()
    instagram = payload.get("instagram")
    ig_user_id = str(instagram.get("id", "")).strip() if isinstance(instagram, Mapping) else None
    api_version = api_version_override or str(payload.get("graph_api_version", "v26.0"))
    if not page_id or not page_token:
        raise MetaCredentialError("Decrypted Meta credentials are missing required Page credentials.")
    return MetaConfig(
        page_id=page_id,
        page_access_token=page_token,
        ig_user_id=ig_user_id or None,
        api_version=api_version,
    )


def load_meta_config_from_b2(
    client: Any,
    b2_config: B2Config,
    raw_key: str,
    api_version_override: Optional[str] = None,
) -> MetaConfig:
    key = build_meta_credentials_key(b2_config.prefix)
    try:
        response = client.get_object(Bucket=b2_config.bucket_name, Key=key)
        body = response.get("Body")
        raw = body.read() if hasattr(body, "read") else body
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return load_meta_config_from_envelope(raw, raw_key, api_version_override=api_version_override)
    except MetaCredentialError:
        raise
    except Exception as err:
        raise MetaCredentialError("Encrypted Meta credentials are unavailable.") from err


def load_meta_config_from_state_store(
    state_store: Any,
    raw_key: str,
    api_version_override: Optional[str] = None,
) -> Optional[MetaConfig]:
    envelope = state_store.load_encrypted_meta_credentials()
    if not envelope:
        return None
    return load_meta_config_from_envelope(envelope, raw_key, api_version_override=api_version_override)
