"""Bind German token ranges to an existing reviewed Arabic/English record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from engine import translation
from engine.alignments import (
    AlignmentValidationError, DIGEST_ALIGNMENT_SCHEMA_VERSION, ValidatedAlignment, alignment_path_for_verse,
    alignment_source_path, normalized_sha256, parse_alignment_record,
    read_alignment_json, validate_alignment_record,
)

if TYPE_CHECKING:
    from engine.renderer import RenderTranslation


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BILINGUAL_ALIGNMENT_ROOT = REPOSITORY_ROOT / "alignments/english_german_rwwad/v1"
PACKAGED_BILINGUAL_ALIGNMENT_ROOT = REPOSITORY_ROOT / "data/alignments/english_german_rwwad/v1"


@dataclass(frozen=True)
class BilingualAlignment:
    english: ValidatedAlignment
    german_panels: tuple[str, ...]
    provenance: dict[str, Any]


def load_bilingual_alignment(
    verse_key: str,
    arabic_text: str,
    english: RenderTranslation,
    german: RenderTranslation,
    *,
    root: Path,
    english_root: Path,
    preview: bool,
) -> BilingualAlignment | None:
    path = alignment_path_for_verse(verse_key, root)
    if not path.exists() and not path.is_symlink():
        return None
    raw, digest = read_alignment_json(path)
    parent_path = alignment_path_for_verse(verse_key, english_root)
    parent, parent_digest = read_alignment_json(parent_path)
    alignment = validate_alignment_record(
        parse_alignment_record(parent, source_path=alignment_source_path(parent_path)),
        verse_key=verse_key, arabic_text=arabic_text, english_text=english.text,
        english_translation_key=english.translation_key,
        english_translation_version=english.translation_version,
    )
    status = "unverified" if preview else "verified"
    version = "english_german_rwwad-candidate-v1" if preview else "english_german_rwwad-v1"
    expected = {
        "schema_version": DIGEST_ALIGNMENT_SCHEMA_VERSION, "data_version": version, "verse_key": verse_key,
        "english_alignment_sha256": parent_digest, "arabic": parent["arabic"],
        "english": parent["english"], "tokenization_version": parent["tokenization_version"],
        "timing_strategy": parent["timing_strategy"], "status": status,
        "german": {
            "translation_key": translation.GERMAN_TRANSLATION_KEY,
            "translation_version": translation.EXPECTED_TRANSLATION_VERSIONS[translation.GERMAN_TRANSLATION_KEY],
            "source_url": translation.GERMAN_ARCHIVE_URL,
            "source_file_sha256": translation.GERMAN_ARCHIVE_SHA256,
            "normalized_sha256": normalized_sha256(german.text),
            "footnotes_sha256": sha256(german.footnotes.encode("utf-8")).hexdigest(),
        },
    }
    fields = set(expected) | {"review_note", "panels"}
    if not preview:
        fields.add("reviewed_at")
        try:
            date.fromisoformat(raw.get("reviewed_at"))
        except (TypeError, ValueError) as err:
            raise AlignmentValidationError("A verified bilingual alignment needs a review date.") from err
    if set(raw) != fields or type(raw["schema_version"]) is not int:
        raise AlignmentValidationError("Invalid bilingual alignment fields.")
    if not isinstance(raw["review_note"], str) or not raw["review_note"].strip():
        raise AlignmentValidationError("Bilingual review note is missing.")
    for key, value in expected.items():
        if raw[key] != value:
            raise AlignmentValidationError(f"Bilingual alignment {key} mismatch.")
    if (german.translation_key != raw["german"]["translation_key"]
            or german.translation_version != raw["german"]["translation_version"]):
        raise AlignmentValidationError("German translation source or version mismatch.")

    panels = raw["panels"]
    if not isinstance(panels, list) or len(panels) != len(alignment.panels):
        raise AlignmentValidationError("Bilingual panel count must match the verified English record.")
    tokens = german.text.split()
    start = 0
    slices = []
    for index, panel in enumerate(panels):
        if not isinstance(panel, Mapping) or set(panel) != {
            "panel_index", "german_token_start", "german_token_end", "german_sha256",
        }:
            raise AlignmentValidationError("Invalid German panel fields.")
        index_fields = (panel["panel_index"], panel["german_token_start"], panel["german_token_end"])
        if any(type(value) is not int for value in index_fields):
            raise AlignmentValidationError("German panel indices must be integers.")
        actual_index, actual_start, end = index_fields
        if actual_index != index or actual_start != start or not start < end <= len(tokens):
            raise AlignmentValidationError("German ranges must be ordered, contiguous and nonempty.")
        text = " ".join(tokens[start:end])
        if panel["german_sha256"] != normalized_sha256(text):
            raise AlignmentValidationError("German panel text differs from its exact source range.")
        slices.append(text)
        start = end
    if start != len(tokens):
        raise AlignmentValidationError("German ranges must cover the entire verse.")
    provenance = {
        **alignment.provenance(),
        "alignment_schema_version": DIGEST_ALIGNMENT_SCHEMA_VERSION,
        "alignment_data_version": version, "alignment_status": status,
        "alignment_path": alignment_source_path(path), "alignment_sha256": digest,
        "english_alignment_path": alignment.record.source_path,
        "english_alignment_sha256": parent_digest,
        "german_translation_key": german.translation_key,
        "german_translation_version": german.translation_version,
        "german_source_hash": expected["german"]["normalized_sha256"],
    }
    return BilingualAlignment(alignment, tuple(slices), provenance)
