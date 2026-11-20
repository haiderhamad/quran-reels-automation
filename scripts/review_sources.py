"""Read public Quran sources for local review without installation credentials."""
import argparse
from pathlib import Path
from urllib.request import Request, urlopen

from engine import audio, quran, translation
from engine.renderer import RenderTranslation

PUBLIC_QURAN_API = "https://api.quran.com/api/v4"


def parse_verse_key(value: str) -> tuple[int, int]:
    try:
        surah, ayah = map(int, value.split(":"))
        if 1 <= ayah <= quran.STANDARD_SURAH_AYAH_COUNTS[surah]:
            return surah, ayah
    except (ValueError, KeyError):
        pass
    raise argparse.ArgumentTypeError("Expected a canonical verse in SURAH:AYAH form.")


def fetch_public_quran() -> quran.QuranData:
    request = Request(f"{PUBLIC_QURAN_API}/quran/verses/uthmani",
                      headers={"Accept": "application/json", "User-Agent": audio.DEFAULT_USER_AGENT})
    with urlopen(request, timeout=30, context=quran._build_ssl_context()) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("Unexpected size for the public Uthmani source.")
    return quran.parse_quran_payload(raw, qf_env="public-verification", api_base_url=PUBLIC_QURAN_API)


def read_review_text(verse: tuple[int, int], cache: Path) -> tuple[str, str, str, RenderTranslation]:
    corpus = fetch_public_quran()
    surah = corpus.get_surah(verse[0])
    arabic = corpus.get_ayah(*verse).arabic_text
    dataset = translation.fetch_translation_data_for_language("en", cache_dir=cache)
    ayah = dataset.get_ayah(*verse)
    return surah.arabic_name, surah.english_name, arabic, RenderTranslation(
        "en", ayah.translation_text, ayah.footnotes, dataset.source.translation_key, dataset.source.version)
