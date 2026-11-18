"""Tests for worker-side encrypted Meta credential loading."""

from __future__ import annotations

import base64
import io
import json
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from engine.meta_credentials import (
    META_CREDENTIAL_AAD,
    MetaCredentialError,
    build_meta_credentials_key,
    decrypt_meta_credentials,
    load_meta_config_from_b2,
)
from engine.storage import B2Config


class FakeB2Client:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.request = None

    def get_object(self, **kwargs):
        self.request = kwargs
        return {"Body": io.BytesIO(self.body)}


def encrypted_fixture(key: bytes, token: str = "page_access_token_secret") -> dict:
    payload = {
        "schema_version": 1,
        "graph_api_version": "v26.0",
        "user_id": "user-1",
        "page_id": "page-1",
        "page_name": "Quran Page",
        "page_access_token": token,
        "instagram": None,
    }
    iv = bytes(range(12))
    encrypted = AESGCM(key).encrypt(iv, json.dumps(payload).encode(), META_CREDENTIAL_AAD)
    return {
        "format": "aes-256-gcm",
        "version": 1,
        "iv": base64.urlsafe_b64encode(iv).decode().rstrip("="),
        "ciphertext": base64.urlsafe_b64encode(encrypted[:-16]).decode().rstrip("="),
        "tag": base64.urlsafe_b64encode(encrypted[-16:]).decode().rstrip("="),
    }


class TestMetaCredentials(unittest.TestCase):
    def setUp(self) -> None:
        self.key = bytes(range(32))
        self.encoded_key = base64.b64encode(self.key).decode()

    def test_decrypts_authenticated_payload(self) -> None:
        payload = decrypt_meta_credentials(encrypted_fixture(self.key), self.encoded_key)
        self.assertEqual(payload["page_id"], "page-1")

    def test_wrong_key_fails_without_token_leakage(self) -> None:
        token = "_".join(("never", "echo", "this", "page", "fixture"))
        with self.assertRaises(MetaCredentialError) as ctx:
            decrypt_meta_credentials(
                encrypted_fixture(self.key, token),
                base64.b64encode(bytes(reversed(range(32)))).decode(),
            )
        self.assertNotIn(token, str(ctx.exception))

    def test_loads_page_credentials_and_keeps_instagram_optional(self) -> None:
        client = FakeB2Client(json.dumps(encrypted_fixture(self.key)).encode())
        b2 = B2Config(
            endpoint_url="https://s3.example.test",
            region_name="test",
            application_key_id="id",
            application_key="key",
            bucket_name="bucket",
            prefix="quran-reels/",
        )
        config = load_meta_config_from_b2(client, b2, self.encoded_key)
        self.assertEqual(config.page_id, "page-1")
        self.assertIsNone(config.ig_user_id)
        self.assertEqual(
            client.request["Key"],
            build_meta_credentials_key("quran-reels/"),
        )


if __name__ == "__main__":
    unittest.main()
