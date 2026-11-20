"""Print an unverified Arabic/English panel proposal for local human review."""
import argparse
import json
from pathlib import Path
import tempfile

from engine.alignments import normalized_sha256
from engine.subtitles import segment_multilingual_phrases
from scripts.review_sources import parse_verse_key, read_review_text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verse", required=True, type=parse_verse_key)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="panel-proposal-") as cache:
        _, _, arabic, english = read_review_text(args.verse, Path(cache))
    panels = segment_multilingual_phrases(arabic, {"en": english.text})
    result = {
        "status": "unverified",
        "publication_approved": False,
        "verse_key": "%d:%d" % args.verse,
        "arabic_normalized_sha256": normalized_sha256(arabic),
        "english_normalized_sha256": normalized_sha256(english.text),
        "english_translation_key": english.translation_key,
        "english_translation_version": english.translation_version,
        "panels": [{"panel_index": i, "arabic_text": text, "english_text": terms[0].text}
                   for i, (text, terms) in enumerate(panels)],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
