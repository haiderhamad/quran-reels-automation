"""Unit tests for the font management, acquisition, and SHA-256 verification module."""

from __future__ import annotations

import hashlib
import http.client
import io
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts.fetch_fonts import (
    DEFAULT_CACHE_DIR,
    PINNED_FONTS,
    compute_file_sha256,
    fetch_all_fonts,
    fetch_font,
    main,
    verify_font_file,
)


class FontManifestTests(unittest.TestCase):
    def test_pinned_fonts_manifest_structure(self) -> None:
        required_fonts = [
            "AmiriQuran.ttf",
            "Amiri-Regular.ttf",
            "Amiri-Bold.ttf",
            "NotoSans-Regular.ttf",
        ]
        for font_name in required_fonts:
            self.assertIn(font_name, PINNED_FONTS)
            meta = PINNED_FONTS[font_name]
            self.assertIn("sha256", meta)
            self.assertEqual(len(meta["sha256"]), 64)
            self.assertEqual(meta["license"], "OFL-1.1")
            self.assertIn("description", meta)
            source_type = meta.get("source_type")
            self.assertIn(source_type, ("direct", "zip_entry"))
            if source_type == "direct":
                self.assertTrue(meta["url"].startswith("https://"))
            elif source_type == "zip_entry":
                self.assertTrue(meta["archive_url"].startswith("https://"))
                self.assertTrue(bool(meta.get("archive_member")))

    def test_compute_file_sha256(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"Quran Reels Automation Font Test Data")
            f_path = f.name
        try:
            expected = hashlib.sha256(b"Quran Reels Automation Font Test Data").hexdigest().lower()
            actual = compute_file_sha256(f_path)
            self.assertEqual(actual, expected)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_verify_font_file_missing_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(verify_font_file("AmiriQuran.ttf", tmpdir))

    def test_verify_font_file_corrupt_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = os.path.join(tmpdir, "AmiriQuran.ttf")
            with open(bad_file, "wb") as f:
                f.write(b"corrupt font data")
            self.assertFalse(verify_font_file("AmiriQuran.ttf", tmpdir))

    def test_verify_font_file_valid_returns_true(self) -> None:
        # Check if local cache has fonts
        if os.path.isdir(DEFAULT_CACHE_DIR):
            for name in PINNED_FONTS:
                if os.path.isfile(os.path.join(DEFAULT_CACHE_DIR, name)):
                    self.assertTrue(verify_font_file(name, DEFAULT_CACHE_DIR))

    def test_unknown_font_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                verify_font_file("UnknownFont.ttf", tmpdir)

            with self.assertRaises(ValueError):
                fetch_font("UnknownFont.ttf", dest_dir=tmpdir)


class FontResponse(io.BytesIO):
    status = 200


class FontDownloadRecoveryTests(unittest.TestCase):
    """Cold-cache acquisition must survive bounded transport errors, never bad hashes."""

    def font_metadata(self, filename: str, contents: bytes) -> dict:
        return {**PINNED_FONTS[filename], "sha256": hashlib.sha256(contents).hexdigest()}

    def test_amiri_download_uses_the_official_release_asset_api(self):
        filename = "AmiriQuran.ttf"
        contents = b"synthetic exact release font bytes"
        metadata = self.font_metadata(filename, contents)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr(metadata["archive_member"], contents)

        def official_asset(request, **_kwargs):
            headers = {key.lower(): value for key, value in request.header_items()}
            if (request.full_url == "https://api.github.com/repos/aliftype/amiri/releases/assets/86889246"
                    and headers.get("accept") == "application/octet-stream"
                    and headers.get("x-github-api-version") == "2022-11-28"):
                return FontResponse(archive.getvalue())
            raise urllib.error.HTTPError(request.full_url, 400, "Wrong release API URL or download headers", None, None)

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(PINNED_FONTS, {filename: metadata}),
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=official_asset),
        ):
            try:
                path = fetch_font(filename, dest_dir=temp)
            except urllib.error.HTTPError as error:
                self.fail(f"Downloader must request the official asset API with download headers: {error}")
            with open(path, "rb") as font:
                self.assertEqual(font.read(), contents)

    def test_archive_504_retries_and_installs_only_the_pinned_font(self):
        filename = "AmiriQuran.ttf"
        contents = b"synthetic pinned Quran font bytes"
        metadata = self.font_metadata(filename, contents)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr(metadata["archive_member"], contents)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(PINNED_FONTS, {filename: metadata}),
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=[
                urllib.error.HTTPError(metadata["archive_url"], 504, "Gateway timeout", None, None),
                FontResponse(archive.getvalue()),
            ]) as remote,
            patch("time.sleep"),
        ):
            try:
                path = fetch_font(filename, dest_dir=temp)
            except urllib.error.HTTPError as error:
                self.fail(f"Transient archive HTTP504 should recover: {error}")
            with open(path, "rb") as font:
                self.assertEqual(font.read(), contents)
            self.assertTrue(verify_font_file(filename, temp))
            self.assertEqual(remote.call_count, 2)

    def test_direct_font_timeout_retries_before_atomic_install(self):
        filename = "NotoSans-Regular.ttf"
        contents = b"synthetic pinned English font bytes"
        metadata = self.font_metadata(filename, contents)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(PINNED_FONTS, {filename: metadata}),
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=[
                urllib.error.URLError(TimeoutError("timed out")), FontResponse(contents),
            ]) as remote,
            patch("time.sleep"),
        ):
            try:
                path = fetch_font(filename, dest_dir=temp)
            except urllib.error.URLError as error:
                self.fail(f"Transient direct download should recover: {error}")
            with open(path, "rb") as font:
                self.assertEqual(font.read(), contents)
            self.assertTrue(verify_font_file(filename, temp))
            self.assertEqual(remote.call_count, 2)

    def test_persistent_504_is_bounded_and_preserves_existing_target(self):
        filename = "NotoSans-Regular.ttf"
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=
                  urllib.error.HTTPError(PINNED_FONTS[filename]["url"], 504, "Gateway timeout", None, None)) as remote,
            patch("time.sleep"),
        ):
            target = os.path.join(temp, filename)
            with open(target, "wb") as font:
                font.write(b"previous file must survive failed recovery")
            with self.assertRaises(urllib.error.HTTPError):
                fetch_font(filename, dest_dir=temp)
            self.assertEqual(remote.call_count, 3)
            with open(target, "rb") as font:
                self.assertEqual(font.read(), b"previous file must survive failed recovery")
            self.assertEqual(os.listdir(temp), [filename])

    def test_interrupted_response_body_retries_the_complete_download(self):
        filename = "NotoSans-Regular.ttf"
        contents = b"complete pinned font bytes"
        metadata = self.font_metadata(filename, contents)

        class InterruptedResponse(FontResponse):
            def read(self, *args):
                raise http.client.IncompleteRead(b"partial", len(contents))

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(PINNED_FONTS, {filename: metadata}),
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=[
                InterruptedResponse(), FontResponse(contents),
            ]) as remote,
            patch("time.sleep"),
        ):
            path = fetch_font(filename, dest_dir=temp)
            with open(path, "rb") as font:
                self.assertEqual(font.read(), contents)
            self.assertTrue(verify_font_file(filename, temp))
            self.assertEqual(remote.call_count, 2)
            self.assertEqual(os.listdir(temp), [filename])

    def test_nontransient_http_error_is_not_retried(self):
        filename = "NotoSans-Regular.ttf"
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=
                  urllib.error.HTTPError(PINNED_FONTS[filename]["url"], 404, "Not found", None, None)) as remote,
            patch("time.sleep"),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                fetch_font(filename, dest_dir=temp)
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(os.listdir(temp), [])

    def test_checksum_failure_is_not_retried_and_never_replaces_target(self):
        filename = "NotoSans-Regular.ttf"
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("scripts.fetch_fonts.urllib.request.urlopen", return_value=FontResponse(b"wrong font")) as remote,
            patch("time.sleep"),
        ):
            target = os.path.join(temp, filename)
            with open(target, "wb") as font:
                font.write(b"original file")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                fetch_font(filename, dest_dir=temp)
            self.assertEqual(remote.call_count, 1)
            with open(target, "rb") as font:
                self.assertEqual(font.read(), b"original file")
            self.assertEqual(os.listdir(temp), [filename])

    def test_verified_cache_never_needs_network(self):
        filename = "NotoSans-Regular.ttf"
        contents = b"synthetic already pinned font"
        metadata = self.font_metadata(filename, contents)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(PINNED_FONTS, {filename: metadata}),
            patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=AssertionError("No network for valid cache")) as remote,
        ):
            target = os.path.join(temp, filename)
            with open(target, "wb") as font:
                font.write(contents)
            self.assertEqual(fetch_font(filename, dest_dir=temp), target)
            remote.assert_not_called()


class FontBulkDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.original_metadata = {name: dict(meta) for name, meta in PINNED_FONTS.items()}
        self.enterContext(patch("scripts.fetch_fonts.DEFAULT_BUNDLE_DIR", os.path.join(self.directory, "missing-bundle"), create=True))
        self.contents = {
            "AmiriQuran.ttf": b"synthetic Quran font",
            "Amiri-Regular.ttf": b"synthetic regular font",
            "Amiri-Bold.ttf": b"synthetic bold font",
            "NotoSans-Regular.ttf": b"synthetic translation font",
        }
        metadata = {
            name: {**PINNED_FONTS[name], "sha256": hashlib.sha256(contents).hexdigest()}
            for name, contents in self.contents.items()
        }
        self.enterContext(patch.dict(PINNED_FONTS, metadata))
        self.archive = self.build_archive(self.contents)
        self.requests = []
        self.enterContext(patch("scripts.fetch_fonts.urllib.request.urlopen", side_effect=self.source))

    def build_archive(self, contents):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            for name in ("AmiriQuran.ttf", "Amiri-Regular.ttf", "Amiri-Bold.ttf"):
                zipped.writestr(PINNED_FONTS[name]["archive_member"], contents[name])
        return archive.getvalue()

    def source(self, request, **_kwargs):
        self.requests.append(request.full_url)
        if request.full_url == "https://api.github.com/repos/aliftype/amiri/releases/assets/86889246":
            return FontResponse(self.archive)
        if request.full_url == "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf":
            return FontResponse(self.contents["NotoSans-Regular.ttf"])
        raise AssertionError("Unexpected font source")

    def populate_cache(self):
        for name, contents in self.contents.items():
            with open(os.path.join(self.directory, name), "wb") as font:
                font.write(contents)

    def run_cli(self, *arguments):
        with (
            patch("sys.argv", ["fetch_fonts.py", "--dest", self.directory, *arguments]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            try:
                return main()
            except SystemExit as error:
                self.fail(f"Font installation CLI must support these arguments: {error}")

    def assert_all_fonts_valid(self):
        for name, contents in self.contents.items():
            with open(os.path.join(self.directory, name), "rb") as font:
                self.assertEqual(font.read(), contents)
            self.assertTrue(verify_font_file(name, self.directory))
        self.assertEqual(set(os.listdir(self.directory)), set(self.contents))

    def test_cold_cli_downloads_the_shared_amiri_archive_only_once(self):
        self.assertEqual(self.run_cli(), 0)
        self.assert_all_fonts_valid()
        self.assertEqual(len(self.requests), 2)  # One archive and one direct font.

    def test_force_bulk_refresh_still_downloads_each_source_once(self):
        self.populate_cache()
        paths = fetch_all_fonts(dest_dir=self.directory, force=True)
        self.assertEqual(set(paths), set(self.contents))
        self.assert_all_fonts_valid()
        self.assertEqual(len(self.requests), 2)

    def test_warm_cli_verifies_every_cached_font_without_network(self):
        self.populate_cache()
        self.assertEqual(self.run_cli(), 0)
        self.assert_all_fonts_valid()
        self.assertEqual(self.requests, [])

    def test_corrupt_cached_font_cannot_pass_with_a_bad_upstream_member(self):
        self.populate_cache()
        target = os.path.join(self.directory, "Amiri-Bold.ttf")
        with open(target, "wb") as font:
            font.write(b"corrupt restored cache")
        self.archive = self.build_archive({**self.contents, "Amiri-Bold.ttf": b"bad source member"})
        self.assertEqual(self.run_cli(), 1)
        self.assertFalse(verify_font_file("Amiri-Bold.ttf", self.directory))
        with open(target, "rb") as font:
            self.assertEqual(font.read(), b"corrupt restored cache")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(set(os.listdir(self.directory)), set(self.contents))

    def test_reusing_an_archive_still_checks_each_members_own_hash(self):
        self.populate_cache()
        self.archive = self.build_archive({**self.contents, "Amiri-Regular.ttf": b"bad regular font"})
        self.assertEqual(self.run_cli("--force"), 1)
        self.assert_all_fonts_valid()  # A failed replacement preserves the previous font.

    def create_bundle(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        for name, contents in self.contents.items():
            Path(directory, name).write_bytes(contents)
        self.enterContext(patch("scripts.fetch_fonts.DEFAULT_BUNDLE_DIR", directory, create=True))
        return Path(directory)

    def test_offline_cli_installs_every_pinned_font_without_network(self):
        self.create_bundle()
        self.assertEqual(self.run_cli("--offline"), 0)
        self.assert_all_fonts_valid()
        self.assertEqual(self.requests, [])

    def test_offline_missing_bundle_member_fails_before_installation(self):
        bundle = self.create_bundle()
        (bundle / "Amiri-Bold.ttf").unlink()
        self.assertEqual(self.run_cli("--offline"), 1)
        self.assertEqual(os.listdir(self.directory), [])
        self.assertEqual(self.requests, [])

    def test_offline_corrupt_bundle_preserves_existing_cache(self):
        self.populate_cache()
        bundle = self.create_bundle()
        (bundle / "Amiri-Bold.ttf").write_bytes(b"bad bundled font")
        self.assertEqual(self.run_cli("--offline"), 1)
        self.assert_all_fonts_valid()
        self.assertEqual(self.requests, [])

    def test_offline_valid_bundle_repairs_corrupt_cache_without_network(self):
        self.populate_cache()
        self.create_bundle()
        Path(self.directory, "Amiri-Bold.ttf").write_bytes(b"bad restored font")
        self.assertEqual(self.run_cli("--offline"), 0)
        self.assert_all_fonts_valid()
        self.assertEqual(self.requests, [])

    def test_actual_repository_bundle_installs_all_original_pins_offline(self):
        bundle = Path(__file__).resolve().parents[1] / "assets" / "fonts"
        with (
            patch.dict(PINNED_FONTS, self.original_metadata),
            patch("scripts.fetch_fonts.DEFAULT_BUNDLE_DIR", str(bundle), create=True),
        ):
            self.assertEqual(self.run_cli("--offline"), 0)
            for name in self.original_metadata:
                self.assertTrue(verify_font_file(name, self.directory))
            self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
