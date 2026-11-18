"""Package an operator's lawfully obtained, exact QuranEnc English source offline."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

from engine import translation as tr
from scripts.prepare_rowwad_reference import MAX_COMPRESSED_BYTES


def package(directory: Path, output: Path) -> dict:
    if output.suffix != ".gz":
        raise ValueError("The output must have a .gz suffix")
    names = ["metadata.json"] + [f"{n:03d}.json" for n in range(1, tr.EXPECTED_SURAH_COUNT + 1)]
    remaining = tr.MAX_TRANSLATION_REFERENCE_BYTES
    parts = []
    for name in names:
        with (directory / name).open("rb") as stream:
            payload = stream.read(remaining + 1)
        remaining -= len(payload)
        if remaining < 0:
            raise ValueError("Source exceeds the expected size")
        parts.append(payload)
    data = tr.parse_complete_translation_dataset(parts[0], parts[1:])
    identity = data.source.translation_key, data.source.version
    digest = tr._cache_body_sha256(tr._cache_body_for_data(data))
    if identity != (tr.DEFAULT_TRANSLATION_KEY, "1.0.19") or digest != tr.PINNED_TRANSLATION_BODY_HASHES[identity]:
        raise ValueError("The operator input differs from the pinned source")
    with tempfile.TemporaryDirectory(dir=output.parent) as name:
        staged = Path(name) / "validated.json"
        tr._write_validated_translation_cache(staged, data)
        compressed = gzip.compress(staged.read_bytes(), mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ValueError("Compressed source exceeds the runner limit")
        artifact = Path(name) / "validated.gz"
        with artifact.open("xb") as stream:
            artifact.chmod(0o600)
            stream.write(compressed)
        os.link(artifact, output)
    return {"body_sha256": digest, "gzip_sha256": hashlib.sha256(compressed).hexdigest(),
            "version": data.source.version, "ayahs": data.total_ayahs, "surahs": data.total_surahs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.inputs, args.output), sort_keys=True))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Source packaging failed ({type(error).__name__}).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
