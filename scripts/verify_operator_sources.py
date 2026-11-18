"""Check a legally supplied Rowwad source and an optional reviewed panel manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from engine import alignments, quran, translation


def verify(reference: Path, verse: str | None, root: Path) -> dict:
    record = None
    if verse:
        record = alignments.load_alignment_record(verse, root=root)
        if record is None:
            raise ValueError("The requested reviewed panel manifest is missing")
    # A warm cache must never hide a corrupt supplied document.
    with tempfile.TemporaryDirectory() as fresh:
        source = translation.fetch_translation_data_for_language(
            "en", cache_dir=fresh, reference_path=reference
        )
    result = {"key": source.source.translation_key, "version": source.source.version,
              "surahs": source.total_surahs, "ayahs": source.total_ayahs,
              "body_sha256": translation._cache_body_sha256(translation._cache_body_for_data(source))}
    if reference.suffix == ".gz":
        with reference.open("rb") as stream:
            result["gzip_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    if record is not None:
        dataset = quran.fetch_quran_data(qf_env="production")
        quran.validate_quran_source_for_run("manual_production", dataset)
        surah, ayah = (int(number) for number in verse.split(":"))
        validated = alignments.validate_alignment_record(
            record, verse_key=verse, arabic_text=dataset.get_ayah(surah, ayah).arabic_text,
            english_text=source.get_ayah(surah, ayah).translation_text,
            english_translation_key=source.source.translation_key,
            english_translation_version=source.source.version,
        )
        result.update(verse_key=verse, panels=len(validated.panels))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--verse", help="Also authenticate the Arabic corpus and check this reviewed manifest")
    parser.add_argument("--manifest-root", type=Path, default=alignments.DEFAULT_ALIGNMENT_ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.reference, args.verse, args.manifest_root), sort_keys=True))
    except (ValueError, OSError, quran.QuranSourceError, translation.TranslationSourceError) as error:
        parser.exit(1, f"Source verification failed ({type(error).__name__}).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
