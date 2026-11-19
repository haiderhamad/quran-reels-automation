"""Offline regression tests for the versioned QuranEnc sources."""
import csv
from dataclasses import replace
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import translation as tr


URL = "https://quranenc.com/downloads/csv/german_rwwad_v1.0.15-csv.1.csv"
TITLE = "Translation of the meanings of the Noble Qur'an"
UPDATED = "2026-07-26 17:27:33"
INFO = "\r\n".join([
    "Translation Info:", "# " + TITLE, "# Language: German",
    "# Translation ID: german_rwwad", "# Source: https://quranenc.com",
    f"# Last update: {UPDATED} (v1.0.15-csv.1)",
])


class ArchiveResponse(io.BytesIO):
    status = 200


class GermanTranslationArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow([INFO, "", "", "", ""])
        writer.writerow(["id", "sura", "aya", "translation", "footnotes"])
        surahs = []
        row_id = 0
        for surah, count in tr.STANDARD_SURAH_AYAH_COUNTS.items():
            ayahs = []
            for ayah in range(1, count + 1):
                row_id += 1
                text = f'Sample „{surah}:{ayah}“, preserved.'
                notes = 'Note with "quotes"\r\nsecond line' if ayah == 35 else ""
                writer.writerow([row_id, surah, ayah, text, notes])
                ayahs.append(tr.TranslationAyah(surah, ayah, text, notes))
            surahs.append(tr.TranslationSurah(surah, count, tuple(ayahs)))
        cls.raw = stream.getvalue().encode("utf-8")
        source = tr.TranslationSourceMetadata("QuranEnc", URL, "german_rwwad", "de",
                                              "1.0.15", UPDATED, TITLE, INFO)
        cls.data = tr.TranslationData(source, tuple(surahs))
        cls.body_hash = tr._cache_body_sha256(tr._cache_body_for_data(cls.data))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)

    def test_default_cold_and_warm_load_preserve_all_rows_and_notes(self):
        with patch.dict(os.environ, {tr.ENGLISH_REFERENCE_PATH_ENV: str(self.cache / "en-only.json")}), \
             patch.object(tr, "GERMAN_ARCHIVE_SHA256", hashlib.sha256(self.raw).hexdigest()), \
             patch.object(tr, "GERMAN_ARCHIVE_BODY_SHA256", self.body_hash), \
             patch("urllib.request.urlopen", return_value=ArchiveResponse(self.raw)) as remote:
            cold = tr.fetch_translation_data_for_language("de", cache_dir=self.cache)
            warm = tr.fetch_translation_data_for_language("de", cache_dir=self.cache)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(remote.call_args.args[0].full_url, URL)
        self.assertEqual((cold, warm), (self.data, self.data))

    def test_public_english_archive_rebuilds_original_api_cache_identity(self):
        raw = self.raw.replace(b'German', b'English').replace(b'german_rwwad', b'english_rwwad')
        raw = raw.replace(b'1.0.15', b'1.0.19').replace(UPDATED.encode(), b'2026-03-12 23:45:25')
        original = tr.TranslationSourceMetadata(
            'QuranEnc', tr.DEFAULT_API_BASE_URL, 'english_rwwad', 'en', '1.0.19', 1773348325,
            'English Translation - Rowwad Translation Center',
            'Translated by the team of the Rowwad Translation Center, in cooperation with the Rabwah Dawah '
            'Association, the Islamic Content Service Association in Languages, and the IslamHouse.com website.',
        )
        expected = replace(self.data, source=original)
        digest = tr._cache_body_sha256(tr._cache_body_for_data(expected))
        with patch.dict(os.environ, {tr.ENGLISH_REFERENCE_PATH_ENV: ''}), \
             patch.object(tr, 'ENGLISH_ARCHIVE_SHA256', hashlib.sha256(raw).hexdigest(), create=True), \
             patch.dict(tr.PINNED_TRANSLATION_BODY_HASHES, {('english_rwwad','1.0.19'): digest}), \
             patch('urllib.request.urlopen', return_value=ArchiveResponse(raw)) as remote:
            self.assertEqual(tr.fetch_translation_data_for_language('en', cache_dir=self.cache), expected)
            self.assertEqual(tr.fetch_translation_data_for_language('en', cache_dir=self.cache), expected)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(remote.call_args.args[0].full_url,
                         'https://quranenc.com/downloads/csv/english_rwwad_v1.0.19-csv.1.csv')

    def test_archive_bytes_must_match_checked_in_pin(self):
        with patch("urllib.request.urlopen", return_value=ArchiveResponse(self.raw)) as remote:
            with self.assertRaisesRegex(tr.TranslationSourceError, "archive hash mismatch"):
                tr.fetch_translation_data_for_language("de", cache_dir=self.cache)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_cold_download_bounds_response_before_hashing(self):
        response = ArchiveResponse(b"x" * 64)
        with patch.object(tr, "MAX_TRANSLATION_REFERENCE_BYTES", 32), \
             patch.object(response, "read", wraps=response.read) as read, \
             patch("urllib.request.urlopen", return_value=response) as remote:
            with self.assertRaisesRegex(tr.TranslationSourceError, "size limit exceeded"):
                tr.fetch_translation_data_for_language("de", cache_dir=self.cache)
        read.assert_called_once_with(33)
        self.assertEqual(remote.call_count, 1)

    def test_archive_transport_exhaustion_never_falls_back_to_latest(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError) as remote:
            with self.assertRaisesRegex(tr.TranslationDatasetFetchExhaustedError, "attempts=2"):
                tr.fetch_translation_data_for_language(
                    "de", cache_dir=self.cache, max_attempts=2, backoff_seconds=(0, 0))
        self.assertEqual([call.args[0].full_url for call in remote.call_args_list], [URL, URL])
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_correct_checksum_does_not_override_version_order_or_completeness(self):
        for modified in (
            self.raw.replace(b"1.0.15", b"1.0.16"),
            self.raw.replace(b"2,1,2,", b"2,1,1,"),
            self.raw.replace(b"2,1,2,", b"1,1,2,"),
            self.raw.split(b"6236,114,6,")[0],
        ):
            with self.subTest(), patch.object(tr, "GERMAN_ARCHIVE_SHA256",
                    hashlib.sha256(modified).hexdigest()), \
                 patch("urllib.request.urlopen", return_value=ArchiveResponse(modified)) as remote:
                with self.assertRaisesRegex(tr.TranslationSourceError, "German archive"):
                    tr.fetch_translation_data_for_language("de", cache_dir=self.cache)
                self.assertEqual(remote.call_args.args[0].full_url, URL)

    def test_custom_endpoint_is_rejected_before_accepting_same_version_data(self):
        untrusted_url = "https://example.test/v1"
        labeled = replace(self.data, source=replace(self.data.source, api_base_url=untrusted_url))
        path = tr._cache_path(self.cache, translation_key="german_rwwad", translation_version="1.0.15")
        with patch.object(tr, "fetch_complete_translation_data", return_value=labeled) as remote:
            for warm in [False, True]:
                if warm:
                    tr._write_validated_translation_cache(path, labeled)
                with self.subTest(warm=warm), self.assertRaisesRegex(tr.TranslationSourceError, "official archive"):
                    tr.fetch_translation_data_for_language("de", cache_dir=self.cache, api_base_url=untrusted_url)
            remote.assert_not_called()

    def test_explicit_reference_override_is_rejected_even_with_valid_warm_cache(self):
        path = tr._cache_path(self.cache, translation_key="german_rwwad", translation_version="1.0.15")
        tr._write_validated_translation_cache(path, self.data)
        with patch.object(tr, "GERMAN_ARCHIVE_BODY_SHA256", self.body_hash), \
             patch("urllib.request.urlopen") as remote, \
             self.assertRaisesRegex(tr.TranslationSourceError, "official archive"):
            tr.fetch_translation_data_for_language("de", cache_dir=self.cache, reference_path=path)
        remote.assert_not_called()

    def test_rechecks_immutable_pin_for_rehashed_warm_cache(self):
        path = tr._cache_path(self.cache, translation_key="german_rwwad", translation_version="1.0.15")
        tr._write_validated_translation_cache(path, self.data)
        payload = json.loads(path.read_bytes())
        payload["body"]["surahs"][1]["ayahs"][34]["footnotes"] = "changed"
        payload["body_sha256"] = tr._cache_body_sha256(payload["body"])
        path.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(tr, "GERMAN_ARCHIVE_BODY_SHA256", self.body_hash), \
             patch("urllib.request.urlopen", return_value=ArchiveResponse(b"corrupt")) as remote:
            with self.assertRaisesRegex(tr.TranslationSourceError, "archive hash mismatch"):
                tr.fetch_translation_data_for_language("de", cache_dir=self.cache)
        self.assertEqual(remote.call_count, 1)
