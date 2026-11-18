"""Prepare an ephemeral original-source cache from a verified installation-owned object."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

from engine.translation import (
    DEFAULT_TRANSLATION_KEY, EXPECTED_TRANSLATION_VERSIONS,
    _cache_body_for_data, _cache_body_sha256, _cache_path,
    fetch_translation_data_for_language,
)

MAX_COMPRESSED_BYTES = 1024 * 1024


class ReferencePreparationError(ValueError):
    """Sanitized source-preparation failure; never contains provider details."""


def create_reference_client(environ: Mapping[str, str] | None = None) -> Any:
    """Create only a bounded B2 transport; credentials are supplied per step."""
    source = os.environ if environ is None else environ
    required = ("B2_ENDPOINT", "B2_REGION", "B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME")
    if any(not source.get(name, "").strip() for name in required):
        raise ReferencePreparationError("ROWWAD_REFERENCE_CONFIG_MISSING")
    try:
        endpoint = urlsplit(source["B2_ENDPOINT"])
    except ValueError:
        raise ReferencePreparationError("ROWWAD_REFERENCE_ENDPOINT_INVALID") from None
    region = source["B2_REGION"]
    if (not re.fullmatch(r"[a-z0-9-]+", region) or endpoint.scheme != "https"
            or endpoint.netloc != f"s3.{region}.backblazeb2.com"
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise ReferencePreparationError("ROWWAD_REFERENCE_ENDPOINT_INVALID")
    try:
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=source["B2_ENDPOINT"], region_name=region,
            aws_access_key_id=source["B2_APPLICATION_KEY_ID"], aws_secret_access_key=source["B2_APPLICATION_KEY"],
            config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=20,
                retries={"mode": "standard", "total_max_attempts": 2}))
    except Exception:
        raise ReferencePreparationError("ROWWAD_REFERENCE_CLIENT_FAILED") from None


def prepare_reference(*, client: Any, bucket_name: str, destination_dir: Path,
                      object_key: str, gzip_sha256: str) -> dict[str, object]:
    """Validate the installation's gzip and the independently pinned original corpus and atomically promote its cache only."""
    if not isinstance(bucket_name, str) or not bucket_name.strip():
        raise ReferencePreparationError("ROWWAD_REFERENCE_BUCKET_MISSING")
    if not object_key or any(part in ("", ".", "..") for part in object_key.split("/")):
        raise ReferencePreparationError("ROWWAD_REFERENCE_OBJECT_KEY_INVALID")
    if not re.fullmatch(r"[a-f0-9]{64}", gzip_sha256):
        raise ReferencePreparationError("ROWWAD_REFERENCE_DIGEST_INVALID")
    destination = Path(destination_dir)
    try:
        response = client.get_object(Bucket=bucket_name, Key=object_key)
    except Exception:
        raise ReferencePreparationError("ROWWAD_REFERENCE_FETCH_FAILED") from None
    try:
        # Even a rejected ContentLength must close the HTTP response body.
        with closing(response["Body"]) as body:
            length = response.get("ContentLength")
            if (type(length) is not int or not 0 < length <= MAX_COMPRESSED_BYTES):
                raise ReferencePreparationError("ROWWAD_REFERENCE_SIZE_INVALID")
            compressed = bytearray()
            digest = hashlib.sha256()
            while chunk := body.read(64 * 1024):
                if len(compressed) + len(chunk) > length:
                    raise ReferencePreparationError("ROWWAD_REFERENCE_SIZE_INVALID")
                compressed.extend(chunk)
                digest.update(chunk)
            if len(compressed) != length or digest.hexdigest() != gzip_sha256:
                raise ReferencePreparationError("ROWWAD_REFERENCE_HASH_INVALID")
        # Close the HTTP body before touching the destination. Even a late
        # close failure cannot turn a failed download into a promoted cache.
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".rowwad-stage-", dir=destination) as stage_name:
            stage = Path(stage_name)
            reference = stage / "original.json.gz"
            with reference.open("xb") as output:
                reference.chmod(0o600)
                output.write(compressed)
            # A new validation cache prevents warm-cache precedence from
            # hiding invalid incoming reference bytes.
            validated_cache = stage / "validated-cache"
            data = fetch_translation_data_for_language("en", cache_dir=validated_cache,
                reference_path=reference)
            identity = {"translation_key": DEFAULT_TRANSLATION_KEY,
                "translation_version": EXPECTED_TRANSLATION_VERSIONS[DEFAULT_TRANSLATION_KEY]}
            staged = _cache_path(validated_cache, **identity)
            cache = destination / "cache"
            cache.mkdir(mode=0o700, exist_ok=True)
            os.replace(staged, _cache_path(cache, **identity))
            return {"translation_key": data.source.translation_key, "version": data.source.version,
                "surahs": data.total_surahs, "ayahs": data.total_ayahs,
                "body_sha256": _cache_body_sha256(_cache_body_for_data(data)),
                "gzip_sha256": digest.hexdigest(), "cache_directory": str(cache)}
    except ReferencePreparationError:
        raise
    except Exception:
        raise ReferencePreparationError("ROWWAD_REFERENCE_PREPARATION_FAILED") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = prepare_reference(client=create_reference_client(),
            bucket_name=os.environ.get("B2_BUCKET_NAME", ""), destination_dir=args.destination_dir,
            object_key=os.environ.get("ROWWAD_REFERENCE_OBJECT_KEY", ""),
            gzip_sha256=os.environ.get("ROWWAD_REFERENCE_GZIP_SHA256", ""))
    except ReferencePreparationError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
