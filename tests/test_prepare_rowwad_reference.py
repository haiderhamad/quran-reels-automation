"""Hermetic read-only provisioning of the original translation reference."""
from __future__ import annotations

import contextlib
from dataclasses import replace
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import translation as tr
def make_valid_complete_dataset():
    meta = {"translations": [{"key": "english_rwwad", "language_iso_code": "en",
            "version": "1.0.19", "last_update": 1, "title": "Fixture title",
            "description": "Fixture description"}]}
    rows = []
    for surah, count in tr.STANDARD_SURAH_AYAH_COUNTS.items():
        rows.append({"result": [{"sura": str(surah), "aya": str(ayah),
                     "translation": f"English meaning of verse {surah}:{ayah}.",
                     "footnotes": "Synthetic fixture note."}
                    for ayah in range(1, count + 1)]})
    return tr.parse_complete_translation_dataset(meta, rows)


class FakeReferenceClient:
    def __init__(self, payload: bytes, *, content_length=None, failure=None):
        self.payload = payload
        self.content_length = len(payload) if content_length is None else content_length
        self.failure = failure
        self.calls = []
        self.body = io.BytesIO(payload)

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return {"ContentLength": self.content_length, "Body": self.body}


class PrepareRowwadReferenceTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.prepare_rowwad_reference")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "cold-run"
        self.data = make_valid_complete_dataset()
        reference = self.root / "source.json"
        tr._write_validated_translation_cache(reference, self.data)
        self.payload = gzip.compress(reference.read_bytes(), mtime=0)
        self.original_reference = reference.read_bytes()
        for guard in (
            patch.dict(tr.PINNED_TRANSLATION_BODY_HASHES,
                {("english_rwwad", "1.0.19"): tr._cache_body_sha256(tr._cache_body_for_data(self.data))}),
            patch.dict(os.environ, {tr.ENGLISH_REFERENCE_PATH_ENV: ""}),
            patch("urllib.request.urlopen", side_effect=AssertionError("Translation HTTP is forbidden")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def prepare(self, client, *, digest=None):
        return self.module.prepare_reference(client=client, bucket_name="private-reference-test",
            destination_dir=self.destination, object_key="example/authorized-source.gz",
            gzip_sha256=digest or hashlib.sha256(self.payload).hexdigest())

    def cache_file(self):
        return tr._cache_path(self.destination / "cache", translation_key="english_rwwad",
            translation_version="1.0.19")

    def test_cold_get_restores_exact_original_and_notes(self):
        client = FakeReferenceClient(self.payload)
        report = self.prepare(client)
        self.assertEqual(client.calls, [{"Bucket": "private-reference-test", "Key": "example/authorized-source.gz"}])
        self.assertEqual((report["version"], report["surahs"], report["ayahs"]), ("1.0.19", 114, 6236))
        loaded = tr.fetch_translation_data_for_language("en", cache_dir=self.destination / "cache")
        self.assertEqual(loaded, self.data)
        self.assertTrue(client.body.closed)
        self.assertEqual((self.root / "source.json").read_bytes(), self.original_reference)
        self.assertEqual(sorted(p.name for p in self.destination.iterdir()), ["cache"])

    def test_corrupt_truncated_or_oversized_download_preserves_existing_cache(self):
        tr._write_validated_translation_cache(self.cache_file(), self.data)
        before = self.cache_file().read_bytes()
        for payload, length in ((b"x" * len(self.payload), len(self.payload)),
                                (self.payload[:-1], len(self.payload)),
                                (self.payload + b"x", len(self.payload)),
                                (self.payload, self.module.MAX_COMPRESSED_BYTES + 1)):
            with self.subTest(size=len(payload), length=length):
                client = FakeReferenceClient(payload, content_length=length)
                with self.assertRaises(self.module.ReferencePreparationError):
                    self.prepare(client)
                self.assertEqual(self.cache_file().read_bytes(), before)
                self.assertTrue(client.body.closed)

    def test_denied_missing_or_timed_out_reference_has_no_upstream_fallback(self):
        for message in ("403 AccessDenied", "404 NoSuchKey", "expired reader", "timed out"):
            with self.subTest(message=message):
                client = FakeReferenceClient(self.payload, failure=RuntimeError(message))
                with self.assertRaisesRegex(self.module.ReferencePreparationError, "FETCH_FAILED"):
                    self.prepare(client)
                self.assertEqual(len(client.calls), 1)
                self.assertFalse(self.cache_file().exists())

    def test_version_or_recomputed_body_checksum_cannot_relabel_text(self):
        for change in ("version", "text", "footnotes"):
            with self.subTest(change=change):
                payload = json.loads(self.original_reference)
                if change == "version":
                    payload["translation_version"] = "1.0.21"
                    payload["body"]["source"]["version"] = "1.0.21"
                else:
                    payload["body"]["surahs"][1]["ayahs"][24][
                        "translation" if change == "text" else "footnotes"] = "Unapproved source change"
                payload["body_sha256"] = tr._cache_body_sha256(payload["body"])
                compressed = gzip.compress(json.dumps(payload).encode(), mtime=0)
                with self.assertRaises(self.module.ReferencePreparationError):
                    self.prepare(FakeReferenceClient(compressed), digest=hashlib.sha256(compressed).hexdigest())
                self.assertFalse(self.cache_file().exists())

    def test_warm_cache_cannot_mask_an_invalid_incoming_reference(self):
        tr._write_validated_translation_cache(self.cache_file(), self.data)
        before = self.cache_file().read_bytes()
        changed = replace(self.data, source=replace(self.data.source, version="1.0.21"))
        invalid = self.root / "newer.json"
        tr._write_validated_translation_cache(invalid, changed)
        compressed = gzip.compress(invalid.read_bytes(), mtime=0)
        with self.assertRaises(self.module.ReferencePreparationError):
            self.prepare(FakeReferenceClient(compressed), digest=hashlib.sha256(compressed).hexdigest())
        self.assertEqual(self.cache_file().read_bytes(), before)

    def test_invalid_gzip_is_typed_and_never_promoted(self):
        compressed = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\x03\x07"
        with self.assertRaises(self.module.ReferencePreparationError):
            self.prepare(FakeReferenceClient(compressed), digest=hashlib.sha256(compressed).hexdigest())
        self.assertFalse(self.cache_file().exists())

    def test_errors_and_report_omit_credentials_and_raw_text(self):
        secret = "example-private-access-value"
        client = FakeReferenceClient(self.payload, failure=RuntimeError(secret + self.original_reference.decode()))
        with self.assertRaises(self.module.ReferencePreparationError) as failure:
            self.prepare(client)
        self.assertNotIn(secret, str(failure.exception))
        self.assertNotIn("English meaning of verse", str(failure.exception))
        report = self.prepare(FakeReferenceClient(self.payload))
        self.assertNotIn("English meaning of verse", json.dumps(report))
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(self.module, "create_reference_client", return_value=client), patch.dict(
                os.environ, {"B2_BUCKET_NAME": "private-reference-test", "ROWWAD_REFERENCE_OBJECT_KEY": "example/authorized-source.gz",
                             "ROWWAD_REFERENCE_GZIP_SHA256": hashlib.sha256(self.payload).hexdigest()}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(self.module.main(["--destination-dir", str(self.destination)]), 1)
        self.assertNotIn(secret, output.getvalue() + errors.getvalue())
        self.assertNotIn("English meaning of verse", output.getvalue() + errors.getvalue())

    def test_atomic_promotion_failure_preserves_existing_cache(self):
        tr._write_validated_translation_cache(self.cache_file(), self.data)
        before = self.cache_file().read_bytes()
        original_replace = os.replace
        def deny_final(source, target):
            if Path(target) == self.cache_file():
                raise PermissionError("Synthetic final cache refusal")
            return original_replace(source, target)
        with patch.object(self.module.os, "replace", side_effect=deny_final):
            with self.assertRaises(self.module.ReferencePreparationError):
                self.prepare(FakeReferenceClient(self.payload))
        self.assertEqual(self.cache_file().read_bytes(), before)

    def test_response_close_failure_does_not_promote_cache(self):
        class FailingClose(io.BytesIO):
            def close(self):
                super().close()
                raise OSError("Synthetic response close failure")
        client = FakeReferenceClient(self.payload)
        client.body = FailingClose(self.payload)
        with self.assertRaises(self.module.ReferencePreparationError):
            self.prepare(client)
        self.assertFalse(self.cache_file().exists())

    def test_sdk_client_has_bounded_read_only_transport_and_no_endpoint_override(self):
        import boto3
        settings = {"B2_ENDPOINT": "https://s3.eu-central-003.backblazeb2.com", "B2_REGION": "eu-central-003",
            "B2_APPLICATION_KEY_ID": "fake-key-id", "B2_APPLICATION_KEY": "fake-key", "B2_BUCKET_NAME": "private-reference-test"}
        with patch.object(boto3, "client") as factory:
            self.module.create_reference_client(settings)
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["config"].connect_timeout, 10)
        self.assertEqual(kwargs["config"].read_timeout, 20)
        self.assertEqual(kwargs["config"].retries["total_max_attempts"], 2)
        for endpoint in ("http://s3.eu-central-003.backblazeb2.com", "https://example.invalid",
                         "https://s3.eu-central-003.backblazeb2.com/path", "https://[invalid"):
            with self.subTest(endpoint=endpoint), patch.object(boto3, "client") as factory:
                with self.assertRaises(self.module.ReferencePreparationError):
                    self.module.create_reference_client(dict(settings, B2_ENDPOINT=endpoint))
                factory.assert_not_called()

    def test_public_preparation_uses_the_pinned_loader_without_b2(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(self.module, "create_reference_client") as b2, \
             patch.object(self.module, "fetch_translation_data_for_language", return_value=self.data) as load, \
             contextlib.redirect_stdout(io.StringIO()):
            tr._write_validated_translation_cache(self.cache_file(), self.data)
            self.assertEqual(self.module.main(["--public-if-unconfigured", "--destination-dir", str(self.destination)]), 0)
        load.assert_called_once_with("en", cache_dir=self.destination / "cache")
        b2.assert_not_called()

    def test_public_preparation_requires_the_validated_cache_to_be_on_disk(self):
        cache = self.cache_file()
        cache.parent.mkdir(parents=True)
        cache.write_text("{invalid", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(tr, "_write_validated_translation_cache", side_effect=OSError("no space")), \
             patch.object(tr, "_fetch_pinned_archive", return_value=self.data), \
             contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(["--public-if-unconfigured", "--destination-dir", str(self.destination)]), 1)

    def test_partial_operator_configuration_never_downgrades_to_public(self):
        with patch.dict(os.environ, {"ROWWAD_REFERENCE_OBJECT_KEY": "example/source.gz"}, clear=True), \
             patch.object(self.module, "fetch_translation_data_for_language") as load, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(["--public-if-unconfigured", "--destination-dir", str(self.destination)]), 1)
        load.assert_not_called()

    def test_cli_accepts_only_destination_and_never_writes_provider_state(self):
        client = FakeReferenceClient(self.payload)
        with patch.object(self.module, "create_reference_client", return_value=client), patch.dict(
                os.environ, {"B2_BUCKET_NAME": "private-reference-test", "ROWWAD_REFERENCE_OBJECT_KEY": "example/authorized-source.gz",
                             "ROWWAD_REFERENCE_GZIP_SHA256": hashlib.sha256(self.payload).hexdigest()}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(["--destination-dir", str(self.destination)]), 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.module.main(["--destination-dir", str(self.destination), "--object-key", "arbitrary"])
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
