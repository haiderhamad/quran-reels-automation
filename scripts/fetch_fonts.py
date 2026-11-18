"""Deterministic font fetcher and SHA-256 verifier for verified Arabic and English typography."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from typing import Optional


DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".cache",
    "fonts",
)
DEFAULT_BUNDLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts",
)

PINNED_FONTS: dict[str, dict[str, str]] = {
    "AmiriQuran.ttf": {
        "source_type": "zip_entry",
        "archive_url": "https://api.github.com/repos/aliftype/amiri/releases/assets/86889246",
        "archive_member": "Amiri-1.000/AmiriQuran.ttf",
        "sha256": "8dcb435b14cb706d79024df4b994221e18474d4be1ec18b04d8120eeaf1844f5",
        "license": "OFL-1.1",
        "version": "1.000",
        "description": "Amiri Quran font for Arabic Quranic typesetting",
    },
    "Amiri-Regular.ttf": {
        "source_type": "zip_entry",
        "archive_url": "https://api.github.com/repos/aliftype/amiri/releases/assets/86889246",
        "archive_member": "Amiri-1.000/Amiri-Regular.ttf",
        "sha256": "b092096eb992aebe59084b0cb5cf0015ae82c7e76b0aac5e7d60ae5113af5f54",
        "license": "OFL-1.1",
        "version": "1.000",
        "description": "Amiri Regular font for Arabic titles and secondary text",
    },
    "Amiri-Bold.ttf": {
        "source_type": "zip_entry",
        "archive_url": "https://api.github.com/repos/aliftype/amiri/releases/assets/86889246",
        "archive_member": "Amiri-1.000/Amiri-Bold.ttf",
        "sha256": "c20c5fda0b648bfd7672fd3ded17488daa3a4810673b9b012fbb4a0af6a26aab",
        "license": "OFL-1.1",
        "version": "1.000",
        "description": "Amiri Bold font for Arabic headings",
    },
    "NotoSans-Regular.ttf": {
        "source_type": "direct",
        "url": "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
        "sha256": "b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5",
        "license": "OFL-1.1",
        "version": "2.013",
        "description": "Noto Sans Regular font for English translation and metadata",
    },
}


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def compute_file_sha256(filepath: str, buffer_size: int = 64 * 1024) -> str:
    """Compute SHA-256 hex digest of a file on disk."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(buffer_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def verify_font_file(filename: str, dest_dir: str) -> bool:
    """Verify that a font file exists in dest_dir and matches its pinned SHA-256 checksum."""
    if filename not in PINNED_FONTS:
        raise ValueError(f"Unknown font filename '{filename}'.")

    filepath = os.path.join(dest_dir, filename)
    if not os.path.isfile(filepath):
        return False

    expected_sha256 = PINNED_FONTS[filename]["sha256"].lower()
    actual_sha256 = compute_file_sha256(filepath)
    return actual_sha256 == expected_sha256


def _download_font_bytes(url: str, *, timeout: float, user_agent: str) -> bytes:
    """Retry only transient transport failures; callers still require pinned hashes."""
    headers = {"User-Agent": user_agent}
    if url.startswith("https://api.github.com/"):
        headers.update({
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    request = urllib.request.Request(url, headers=headers)
    context = _build_ssl_context()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                status_code = getattr(response, "status", None) or response.getcode()
                if status_code != 200:
                    raise urllib.error.HTTPError(url, status_code, "Font source HTTP error", None, None)
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            reason = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
            if (isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError)
                    or attempt == 2):
                raise
            reason = type(error).__name__
        print(f"Retrying font source download: attempt={attempt + 2}/3 reason={reason}",
              file=sys.stderr, flush=True)
        time.sleep(2 ** attempt)
    raise RuntimeError("Font download attempts exhausted.")


def fetch_font(
    filename: str,
    dest_dir: str = DEFAULT_CACHE_DIR,
    force: bool = False,
    timeout: float = 60.0,
    user_agent: str = "QuranReelsAutomation/1.0",
    *,
    archive_cache: Optional[dict[str, bytes]] = None,
) -> str:
    """Download a pinned font file and verify its SHA-256 checksum.

    Returns the absolute path to the verified font file.
    """
    if filename not in PINNED_FONTS:
        raise ValueError(f"Unknown font filename '{filename}'.")

    meta = PINNED_FONTS[filename]
    expected_sha256 = meta["sha256"].lower()
    os.makedirs(dest_dir, exist_ok=True)
    target_path = os.path.join(dest_dir, filename)

    if not force and verify_font_file(filename, dest_dir):
        return target_path

    source_type = meta.get("source_type", "direct")

    if source_type == "direct":
        url = meta["url"]
        if not url.startswith("https://"):
            raise ValueError(f"Font URL must be HTTPS: {url}")

        temp_fd, temp_path = tempfile.mkstemp(dir=dest_dir, prefix=f"{filename}.tmp.")
        try:
            with os.fdopen(temp_fd, "wb") as f_out:
                f_out.write(_download_font_bytes(url, timeout=timeout, user_agent=user_agent))

            actual_sha256 = compute_file_sha256(temp_path)
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"SHA-256 mismatch for {filename}:\n"
                    f"  Expected: {expected_sha256}\n"
                    f"  Actual:   {actual_sha256}"
                )
            os.replace(temp_path, target_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    elif source_type == "zip_entry":
        archive_url = meta["archive_url"]
        archive_member = meta["archive_member"]
        if not archive_url.startswith("https://"):
            raise ValueError(f"Archive URL must be HTTPS: {archive_url}")

        zip_bytes = archive_cache.get(archive_url) if archive_cache is not None else None
        if zip_bytes is None:
            zip_bytes = _download_font_bytes(archive_url, timeout=timeout, user_agent=user_agent)
            if archive_cache is not None:
                archive_cache[archive_url] = zip_bytes

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            if archive_member not in z.namelist():
                raise KeyError(f"Archive member '{archive_member}' not found in {archive_url}")
            font_bytes = z.read(archive_member)

        actual_sha256 = hashlib.sha256(font_bytes).hexdigest().lower()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"SHA-256 mismatch for {filename} from {archive_member}:\n"
                f"  Expected: {expected_sha256}\n"
                f"  Actual:   {actual_sha256}"
            )

        temp_fd, temp_path = tempfile.mkstemp(dir=dest_dir, prefix=f"{filename}.tmp.")
        try:
            with os.fdopen(temp_fd, "wb") as f_out:
                f_out.write(font_bytes)
            os.replace(temp_path, target_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    else:
        raise ValueError(f"Unsupported font source_type '{source_type}'.")

    return target_path


def fetch_all_fonts(
    dest_dir: Optional[str] = None,
    force: bool = False,
) -> dict[str, str]:
    """Download and verify all pinned typography fonts."""
    target_dir = dest_dir or DEFAULT_CACHE_DIR
    results: dict[str, str] = {}
    archive_cache: dict[str, bytes] = {}
    for filename in PINNED_FONTS:
        results[filename] = fetch_font(
            filename, dest_dir=target_dir, force=force, archive_cache=archive_cache,
        )
    return results


def install_bundled_fonts(dest_dir: str) -> dict[str, str]:
    """Validate the complete bundled set before atomically installing any font."""
    contents = {}
    for filename, metadata in PINNED_FONTS.items():
        with open(os.path.join(DEFAULT_BUNDLE_DIR, filename), "rb") as source:
            contents[filename] = source.read()
        if hashlib.sha256(contents[filename]).hexdigest() != metadata["sha256"]:
            raise ValueError(f"Bundled font SHA-256 mismatch for {filename}")

    os.makedirs(dest_dir, exist_ok=True)
    results = {}
    for filename, font_bytes in contents.items():
        target = os.path.join(dest_dir, filename)
        if not verify_font_file(filename, dest_dir):
            temp_fd, temp_path = tempfile.mkstemp(dir=dest_dir, prefix=f"{filename}.tmp.")
            try:
                with os.fdopen(temp_fd, "wb") as output:
                    output.write(font_bytes)
                os.replace(temp_path, target)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        results[filename] = target
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify pinned typography fonts.")
    parser.add_argument("--dest", default=DEFAULT_CACHE_DIR, help="Destination directory for font files.")
    parser.add_argument("--check-only", action="store_true", help="Only verify existing fonts without downloading.")
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument("--force", action="store_true", help="Force an upstream download even when bundled or cached fonts exist.")
    source_mode.add_argument("--offline", action="store_true", help="Require checksum-valid bundled fonts; never download.")
    args = parser.parse_args()

    dest = os.path.abspath(args.dest)
    print(f"Target font directory: {dest}")

    if args.check_only:
        all_ok = True
        for name in PINNED_FONTS:
            ok = verify_font_file(name, dest)
            status = "OK" if ok else "MISSING OR MISMATCHED"
            print(f"  {name}: {status}")
            if not ok:
                all_ok = False
        return 0 if all_ok else 1

    try:
        if args.offline or (not args.force and os.path.isdir(DEFAULT_BUNDLE_DIR)):
            paths = install_bundled_fonts(dest)
        else:
            paths = fetch_all_fonts(dest_dir=dest, force=args.force)
        for name, path in paths.items():
            print(f"  {name}: verified {path}")
        print("All typography fonts verified successfully.")
        return 0
    except Exception as err:
        print(f"Error fetching fonts: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
