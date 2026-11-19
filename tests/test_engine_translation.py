"""Unit tests for verified English translation source module (QuranEnc Rowwad)."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine.quran import STANDARD_SURAH_AYAH_COUNTS as CANONICAL_ARABIC_AYAH_COUNTS
from engine.translation import (
    DEFAULT_API_BASE_URL,
    DEFAULT_TRANSLATION_KEY,
    GERMAN_TRANSLATION_KEY,
    EXPECTED_LANGUAGE_ISO,
    EXPECTED_SURAH_COUNT,
    EXPECTED_TOTAL_AYAH_COUNT,
    STANDARD_SURAH_AYAH_COUNTS,
    TranslationAyah,
    TranslationData,
    TranslationDatasetFetchExhaustedError,
    TranslationSourceError,
    TranslationSourceMetadata,
    TranslationSurah,
    _cache_path,
    _cache_body_for_data,
    _cache_body_sha256,
    _write_validated_translation_cache,
    fetch_complete_translation_data,
    fetch_surah_translation,
    fetch_translation_metadata,
    fetch_translation_data_for_language,
    parse_complete_translation_dataset,
    parse_surah_translation_payload,
    parse_translation_metadata,
)


def make_valid_metadata_payload(
    key: str = DEFAULT_TRANSLATION_KEY,
    lang: str = EXPECTED_LANGUAGE_ISO,
    version: str = "1.0.19",
    title: str = "English Translation - Rowwad Translation Center",
    description: str = "Translated by Rowwad team",
    last_update: int = 1773348325,
) -> dict:
    return {
        "translations": [
            {
                "key": key,
                "direction": "ltr",
                "language_iso_code": lang,
                "version": version,
                "last_update": last_update,
                "title": title,
                "description": description,
            },
            {
                "key": "english_saheeh",
                "direction": "ltr",
                "language_iso_code": "en",
                "version": "1.1.2",
                "last_update": 1750772247,
                "title": "English Translation - Noor International Center",
                "description": "Issued by Noor International Center.",
            },
        ]
    }


def make_valid_surah_payload(surah_number: int) -> dict:
    ayah_count = STANDARD_SURAH_AYAH_COUNTS[surah_number]
    result = []
    for a_num in range(1, ayah_count + 1):
        result.append(
            {
                "id": str(a_num),
                "sura": str(surah_number),
                "aya": str(a_num),
                "arabic_text": f"نص {surah_number}:{a_num}",
                "translation": f"English meaning of verse {surah_number}:{a_num}.",
                "footnotes": f"[{a_num}] Footnote explanation" if a_num % 2 == 0 else "",
            }
        )
    return {"result": result}


def make_valid_complete_dataset() -> TranslationData:
    return parse_complete_translation_dataset(
        make_valid_metadata_payload(),
        [make_valid_surah_payload(surah) for surah in range(1, EXPECTED_SURAH_COUNT + 1)],
    )


class TranslationModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_source_hash = _cache_body_sha256(_cache_body_for_data(make_valid_complete_dataset()))

    def setUp(self):
        # Synthetic cache fixtures use their own injected identity. The real
        # source-integrity partition retains the immutable production pin.
        source_pin = patch.dict("engine.translation.PINNED_TRANSLATION_BODY_HASHES",
            {("english_rwwad", "1.0.19"): self.fixture_source_hash})
        source_pin.start()
        self.addCleanup(source_pin.stop)
        reference_env = patch.dict("os.environ", {"QURAN_REELS_ENGLISH_REFERENCE_PATH": ""})
        reference_env.start()
        self.addCleanup(reference_env.stop)

    def test_parse_valid_rowwad_metadata_succeeds(self) -> None:
        raw = make_valid_metadata_payload()
        meta = parse_translation_metadata(raw)

        self.assertIsInstance(meta, TranslationSourceMetadata)
        self.assertEqual(meta.provider, "QuranEnc")
        self.assertEqual(meta.translation_key, "english_rwwad")
        self.assertEqual(meta.language_iso_code, "en")
        self.assertEqual(meta.version, "1.0.19")
        self.assertEqual(meta.last_update, 1773348325)
        self.assertEqual(meta.title, "English Translation - Rowwad Translation Center")
        self.assertEqual(meta.description, "Translated by Rowwad team")

    def test_parse_german_ruwwad_metadata_version_and_identity(self) -> None:
        payload = make_valid_metadata_payload(
            key=GERMAN_TRANSLATION_KEY,
            lang="de",
            version="1.0.15",
            title="German Translation - Rowwad Translation Center",
        )
        metadata = parse_translation_metadata(
            payload,
            target_key=GERMAN_TRANSLATION_KEY,
            expected_language_iso="de",
        )
        self.assertEqual(metadata.translation_key, "german_rwwad")
        self.assertEqual(metadata.language_iso_code, "de")
        self.assertEqual(metadata.version, "1.0.15")

    def test_german_text_quotes_punctuation_and_footnote_are_exact(self) -> None:
        payload = make_valid_surah_payload(1)
        exact = "Auf dem Schild steht: „Öffne Fach zwei.“ [1]"
        exact_note = "[1] Eine Erklärung – unverändert."
        payload["result"][0]["translation"] = exact
        payload["result"][0]["footnotes"] = exact_note
        ayah = parse_surah_translation_payload(payload, expected_surah=1).ayahs[0]
        self.assertEqual(ayah.translation_text, exact)
        self.assertEqual(ayah.footnotes, exact_note)

    def test_german_fetch_fails_closed_on_unexpected_version(self) -> None:
        source = TranslationSourceMetadata(
            provider="QuranEnc",
            api_base_url=DEFAULT_API_BASE_URL,
            translation_key=GERMAN_TRANSLATION_KEY,
            language_iso_code="de",
            version="0.0.1",
            last_update=1,
            title="German Translation - Rowwad Translation Center",
            description=None,
        )
        with patch(
            "engine.translation._fetch_pinned_archive",
            return_value=TranslationData(source=source, surahs=()),
        ):
            with self.assertRaisesRegex(TranslationSourceError, "version mismatch"):
                with tempfile.TemporaryDirectory() as cache_dir:
                    fetch_translation_data_for_language("de", cache_dir=cache_dir)

    def test_metadata_rejects_missing_target_key(self) -> None:
        raw = {"translations": [{"key": "english_saheeh", "language_iso_code": "en", "version": "1.0", "title": "Saheeh", "last_update": 1}]}
        with self.assertRaisesRegex(TranslationSourceError, "not found"):
            parse_translation_metadata(raw, target_key="english_rwwad")

    def test_metadata_rejects_wrong_language(self) -> None:
        raw = make_valid_metadata_payload(lang="fr")
        with self.assertRaisesRegex(TranslationSourceError, "Unexpected language ISO code"):
            parse_translation_metadata(raw)

    def test_metadata_rejects_missing_version(self) -> None:
        raw = make_valid_metadata_payload(version="")
        with self.assertRaisesRegex(TranslationSourceError, "Missing or empty version"):
            parse_translation_metadata(raw)

    def test_metadata_rejects_missing_title(self) -> None:
        raw = make_valid_metadata_payload(title="  ")
        with self.assertRaisesRegex(TranslationSourceError, "Missing or empty title"):
            parse_translation_metadata(raw)

    def test_metadata_rejects_missing_last_update(self) -> None:
        raw = make_valid_metadata_payload()
        raw["translations"][0]["last_update"] = None
        with self.assertRaisesRegex(TranslationSourceError, "last_update"):
            parse_translation_metadata(raw)

    def test_parse_valid_surah_payload_succeeds(self) -> None:
        surah_1 = parse_surah_translation_payload(make_valid_surah_payload(1), expected_surah=1)

        self.assertIsInstance(surah_1, TranslationSurah)
        self.assertEqual(surah_1.number, 1)
        self.assertEqual(surah_1.number_of_ayahs, 7)
        self.assertEqual(len(surah_1.ayahs), 7)
        self.assertEqual(surah_1.ayahs[0].surah_number, 1)
        self.assertEqual(surah_1.ayahs[0].ayah_number, 1)

    def test_translation_text_immutability_preserves_unicode_and_punctuation(self) -> None:
        payload = make_valid_surah_payload(1)
        quote_text = 'The label reads, “Sample number two[1],”'
        space_text = "   Synthetic padding test   "
        footnote_text = "[1] The editor's note."

        payload["result"][0]["translation"] = quote_text
        payload["result"][0]["footnotes"] = footnote_text
        payload["result"][1]["translation"] = space_text

        surah = parse_surah_translation_payload(payload, expected_surah=1)

        # 1. Quotes and bracketed footnote markers are preserved exactly
        ayah_1 = surah.ayahs[0]
        self.assertEqual(ayah_1.english_text, quote_text)
        self.assertIn("“", ayah_1.english_text)
        self.assertIn("”", ayah_1.english_text)
        self.assertIn("[1]", ayah_1.english_text)
        self.assertEqual(ayah_1.footnotes, footnote_text)

        # 2. Leading and trailing whitespace is NOT stripped
        ayah_2 = surah.ayahs[1]
        self.assertEqual(ayah_2.english_text, space_text)
        self.assertTrue(ayah_2.english_text.startswith("   "))
        self.assertTrue(ayah_2.english_text.endswith("   "))

    def test_rejects_empty_translation_text(self) -> None:
        payload = make_valid_surah_payload(1)
        payload["result"][0]["translation"] = ""
        with self.assertRaisesRegex(TranslationSourceError, "empty or non-string translation"):
            parse_surah_translation_payload(payload, expected_surah=1)

    def test_rejects_non_string_translation_text(self) -> None:
        payload = make_valid_surah_payload(1)
        payload["result"][0]["translation"] = 12345
        with self.assertRaisesRegex(TranslationSourceError, "empty or non-string translation"):
            parse_surah_translation_payload(payload, expected_surah=1)

    def test_rejects_surah_identity_mismatch(self) -> None:
        payload = make_valid_surah_payload(1)
        payload["result"][0]["sura"] = "2"
        with self.assertRaisesRegex(TranslationSourceError, "Surah identity mismatch"):
            parse_surah_translation_payload(payload, expected_surah=1)

    def test_rejects_ayah_number_mismatch_or_non_consecutive(self) -> None:
        payload = make_valid_surah_payload(1)
        payload["result"][1]["aya"] = "3"
        with self.assertRaisesRegex(TranslationSourceError, "ayah number mismatch"):
            parse_surah_translation_payload(payload, expected_surah=1)

    def test_rejects_missing_ayahs_count_mismatch(self) -> None:
        payload = make_valid_surah_payload(1)
        payload["result"] = payload["result"][:6]
        with self.assertRaisesRegex(TranslationSourceError, "contains 6 translation items, expected 7"):
            parse_surah_translation_payload(payload, expected_surah=1)

    def test_rejects_invalid_surah_number_range(self) -> None:
        with self.assertRaisesRegex(TranslationSourceError, "out of range"):
            parse_surah_translation_payload(make_valid_surah_payload(1), expected_surah=0)
        with self.assertRaisesRegex(TranslationSourceError, "out of range"):
            parse_surah_translation_payload(make_valid_surah_payload(1), expected_surah=115)

    def test_rejects_malformed_json_string(self) -> None:
        with self.assertRaisesRegex(TranslationSourceError, "Malformed JSON"):
            parse_surah_translation_payload("<html>Server Error</html>", expected_surah=1)

    def test_complete_translation_dataset_assembly(self) -> None:
        meta_payload = make_valid_metadata_payload()
        surah_payloads = [make_valid_surah_payload(s) for s in range(1, EXPECTED_SURAH_COUNT + 1)]

        data = parse_complete_translation_dataset(meta_payload, surah_payloads)

        self.assertIsInstance(data, TranslationData)
        self.assertEqual(data.total_surahs, 114)
        self.assertEqual(data.total_ayahs, 6236)
        self.assertEqual(len(data.surahs), 114)

    def test_complete_dataset_rejects_incomplete_surahs(self) -> None:
        meta_payload = make_valid_metadata_payload()
        surah_payloads = [make_valid_surah_payload(s) for s in range(1, 114)]  # Only 113 surahs

        with self.assertRaisesRegex(TranslationSourceError, "Expected 114 Surah payloads"):
            parse_complete_translation_dataset(meta_payload, surah_payloads)

    def test_lookup_helpers_and_range_selection(self) -> None:
        meta_payload = make_valid_metadata_payload()
        surah_payloads = [make_valid_surah_payload(s) for s in range(1, EXPECTED_SURAH_COUNT + 1)]
        data = parse_complete_translation_dataset(meta_payload, surah_payloads)

        surah_1 = data.get_surah(1)
        self.assertEqual(surah_1.number, 1)
        self.assertEqual(surah_1.number_of_ayahs, 7)

        ayah_1_1 = data.get_ayah(1, 1)
        self.assertEqual(ayah_1_1.surah_number, 1)
        self.assertEqual(ayah_1_1.ayah_number, 1)

        ayah_range = data.get_ayah_range(1, 1, 3)
        self.assertEqual(len(ayah_range), 3)
        self.assertEqual([a.ayah_number for a in ayah_range], [1, 2, 3])

        # Bounds checks
        with self.assertRaises(TranslationSourceError):
            data.get_surah(0)
        with self.assertRaises(TranslationSourceError):
            data.get_surah(115)
        with self.assertRaises(TranslationSourceError):
            data.get_ayah(1, 0)
        with self.assertRaises(TranslationSourceError):
            data.get_ayah(1, 8)
        with self.assertRaises(TranslationSourceError):
            data.get_ayah_range(1, 4, 3)
        with self.assertRaises(TranslationSourceError):
            data.get_ayah_range(1, 1, 10)

    def test_insecure_http_rejected(self) -> None:
        with self.assertRaisesRegex(TranslationSourceError, "secure HTTPS"):
            fetch_translation_metadata(api_base_url="http://quranenc.com/api/v1")
        with self.assertRaisesRegex(TranslationSourceError, "secure HTTPS"):
            fetch_surah_translation(1, api_base_url="http://quranenc.com/api/v1")

    @patch("urllib.request.urlopen")
    def test_mocked_fetch_metadata_and_surah_success(self, mock_urlopen: MagicMock) -> None:
        meta_bytes = json.dumps(make_valid_metadata_payload()).encode("utf-8")
        s1_bytes = json.dumps(make_valid_surah_payload(1)).encode("utf-8")

        mock_resp_meta = MagicMock()
        mock_resp_meta.status = 200
        mock_resp_meta.read.return_value = meta_bytes
        mock_resp_meta.__enter__.return_value = mock_resp_meta

        mock_resp_s1 = MagicMock()
        mock_resp_s1.status = 200
        mock_resp_s1.read.return_value = s1_bytes
        mock_resp_s1.__enter__.return_value = mock_resp_s1

        mock_urlopen.side_effect = [mock_resp_meta, mock_resp_s1]

        meta = fetch_translation_metadata()
        self.assertEqual(meta.translation_key, "english_rwwad")

        surah_1 = fetch_surah_translation(1)
        self.assertEqual(surah_1.number, 1)
        self.assertEqual(surah_1.number_of_ayahs, 7)

    @patch("urllib.request.urlopen")
    def test_mocked_fetch_http_error_rejected(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://quranenc.com/api/v1/translation/sura/english_rwwad/1",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

        with self.assertRaisesRegex(TranslationSourceError, "HTTP error 404"):
            fetch_surah_translation(1)

    @patch("urllib.request.urlopen")
    def test_mocked_fetch_timeout_rejected(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = TimeoutError("Connection timed out")

        with self.assertRaisesRegex(TranslationSourceError, "Network error"):
            fetch_surah_translation(1, max_retries=0)

    @patch("urllib.request.urlopen")
    def test_timeout_then_success_uses_bounded_backoff_without_sleeping(self, mock_urlopen: MagicMock) -> None:
        payload = json.dumps(make_valid_surah_payload(1)).encode("utf-8")
        response = MagicMock()
        response.status = 200
        response.read.return_value = payload
        response.__enter__.return_value = response
        mock_urlopen.side_effect = [TimeoutError("read timed out"), response]
        delays: list[float] = []

        result = fetch_surah_translation(
            1,
            max_attempts=4,
            sleep_fn=delays.append,
        )

        self.assertEqual(result.number, 1)
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(delays, [2.0])

    @patch("urllib.request.urlopen")
    def test_multiple_transient_failures_then_success_uses_exponential_backoff(
        self,
        mock_urlopen: MagicMock,
    ) -> None:
        payload = json.dumps(make_valid_surah_payload(1)).encode("utf-8")
        response = MagicMock()
        response.status = 200
        response.read.return_value = payload
        response.__enter__.return_value = response
        mock_urlopen.side_effect = [
            urllib.error.URLError("temporary dns failure"),
            TimeoutError("read timed out"),
            response,
        ]
        delays: list[float] = []

        result = fetch_surah_translation(
            1,
            max_attempts=4,
            sleep_fn=delays.append,
        )

        self.assertEqual(result.number, 1)
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(delays, [2.0, 5.0])

    @patch("urllib.request.urlopen")
    def test_http_503_then_success_retries(self, mock_urlopen: MagicMock) -> None:
        payload = json.dumps(make_valid_surah_payload(1)).encode("utf-8")
        response = MagicMock()
        response.status = 200
        response.read.return_value = payload
        response.__enter__.return_value = response
        mock_urlopen.side_effect = [
            urllib.error.HTTPError(
                url="https://quranenc.com/api/v1/translation/sura/english_rwwad/1",
                code=503,
                msg="Unavailable",
                hdrs={},
                fp=None,
            ),
            response,
        ]
        delays: list[float] = []

        result = fetch_surah_translation(1, max_attempts=4, sleep_fn=delays.append)

        self.assertEqual(result.number, 1)
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(delays, [2.0])

    @patch("urllib.request.urlopen")
    def test_invalid_json_fails_without_retry(self, mock_urlopen: MagicMock) -> None:
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"not-json"
        response.__enter__.return_value = response
        mock_urlopen.return_value = response

        with self.assertRaisesRegex(TranslationSourceError, "Malformed JSON"):
            fetch_surah_translation(1, max_attempts=4, sleep_fn=lambda _seconds: None)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_all_transient_attempts_surface_dataset_exhaustion(
        self,
        mock_urlopen: MagicMock,
    ) -> None:
        mock_urlopen.side_effect = TimeoutError("read timed out")
        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaisesRegex(
                TranslationDatasetFetchExhaustedError,
                "TRANSLATION_DATASET_FETCH_EXHAUSTED.*attempts=4.*final_network_error=timeout",
            ):
                fetch_translation_data_for_language(
                    "en",
                    cache_dir=cache_dir,
                    max_attempts=4,
                    sleep_fn=lambda _seconds: None,
                )
        self.assertEqual(mock_urlopen.call_count, 4)

    def test_valid_cache_hit_uses_zero_network_calls(self) -> None:
        data = make_valid_complete_dataset()
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = _cache_path(
                Path(cache_dir),
                translation_key="english_rwwad",
                translation_version="1.0.19",
            )
            _write_validated_translation_cache(cache_path, data)
            with patch("engine.translation._fetch_pinned_archive") as fetch_complete:
                cached = fetch_translation_data_for_language("en", cache_dir=cache_dir)
        self.assertEqual(cached.total_ayahs, EXPECTED_TOTAL_AYAH_COUNT)
        fetch_complete.assert_not_called()

    def test_corrupt_cache_is_rejected_then_remote_dataset_is_validated(self) -> None:
        data = make_valid_complete_dataset()
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = _cache_path(
                Path(cache_dir),
                translation_key="english_rwwad",
                translation_version="1.0.19",
            )
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text("{not-json", encoding="utf-8")
            with patch(
                "engine.translation._fetch_pinned_archive",
                return_value=data,
            ) as fetch_complete:
                loaded = fetch_translation_data_for_language("en", cache_dir=cache_dir)
        self.assertEqual(loaded.total_surahs, EXPECTED_SURAH_COUNT)
        fetch_complete.assert_called_once()

    def test_cache_miss_remote_success_creates_validated_versioned_cache(self) -> None:
        data = make_valid_complete_dataset()
        with tempfile.TemporaryDirectory() as cache_dir:
            with patch(
                "engine.translation._fetch_pinned_archive",
                return_value=data,
            ) as fetch_complete:
                first = fetch_translation_data_for_language("en", cache_dir=cache_dir)
            with patch("engine.translation._fetch_pinned_archive") as second_fetch:
                second = fetch_translation_data_for_language("en", cache_dir=cache_dir)
        self.assertEqual(first.get_ayah(2, 21).translation_text, second.get_ayah(2, 21).translation_text)
        fetch_complete.assert_called_once()
        second_fetch.assert_not_called()

    def test_cache_with_wrong_pinned_version_is_rejected(self) -> None:
        data = make_valid_complete_dataset()
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = _cache_path(
                Path(cache_dir),
                translation_key="english_rwwad",
                translation_version="1.0.19",
            )
            _write_validated_translation_cache(cache_path, data)
            cache_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cache_payload["translation_version"] = "0.0.1"
            cache_path.write_text(json.dumps(cache_payload), encoding="utf-8")
            with patch(
                "engine.translation._fetch_pinned_archive",
                return_value=data,
            ) as fetch_complete:
                fetch_translation_data_for_language("en", cache_dir=cache_dir)
        fetch_complete.assert_called_once()

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_mocked_fetch_retries_transient_error(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        s1_bytes = json.dumps(make_valid_surah_payload(1)).encode("utf-8")
        mock_success_resp = MagicMock()
        mock_success_resp.status = 200
        mock_success_resp.read.return_value = s1_bytes
        mock_success_resp.__enter__.return_value = mock_success_resp

        mock_urlopen.side_effect = [
            urllib.error.URLError("Temporary network timeout"),
            mock_success_resp,
        ]

        surah_1 = fetch_surah_translation(1, max_retries=1)
        self.assertEqual(surah_1.number, 1)
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()

    def test_canonical_surah_ayah_space_cross_check(self) -> None:
        """Confirm that canonical Surah and Ayah counts match the Arabic Quran module."""
        self.assertEqual(len(STANDARD_SURAH_AYAH_COUNTS), EXPECTED_SURAH_COUNT)
        self.assertEqual(len(CANONICAL_ARABIC_AYAH_COUNTS), EXPECTED_SURAH_COUNT)
        self.assertEqual(sum(STANDARD_SURAH_AYAH_COUNTS.values()), EXPECTED_TOTAL_AYAH_COUNT)
        self.assertEqual(sum(CANONICAL_ARABIC_AYAH_COUNTS.values()), EXPECTED_TOTAL_AYAH_COUNT)

        for surah_num in range(1, EXPECTED_SURAH_COUNT + 1):
            with self.subTest(surah=surah_num):
                self.assertEqual(
                    STANDARD_SURAH_AYAH_COUNTS[surah_num],
                    CANONICAL_ARABIC_AYAH_COUNTS[surah_num],
                )


if __name__ == "__main__":
    unittest.main()
