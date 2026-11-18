"""Deterministic downloader and SHA-256 verifier for pinned OpenCV Zoo visual safety models."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Optional


DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".cache",
    "cv_models",
)

PINNED_MODELS: dict[str, dict[str, str]] = {
    "face_detection_yunet_2023mar.onnx": {
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "license": "MIT",
        "source": "OpenCV Zoo (Shiqi Yu)",
        "description": "YuNet face detector ONNX model (2023mar)",
    },
    "person_detection_mediapipe_2023mar.onnx": {
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx",
        "sha256": "47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f",
        "license": "Apache 2.0",
        "source": "OpenCV Zoo (MediaPipe)",
        "description": "MediaPipe MP-PersonDet person detector ONNX model (2023mar)",
    },
    "object_detection_yolox_2022nov.onnx": {
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/object_detection_yolox/object_detection_yolox_2022nov.onnx",
        "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        "license": "Apache 2.0",
        "source": "OpenCV Zoo (YOLOX)",
        "description": "YOLOX COCO object detector used for explicit common-animal classes",
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


def verify_model_file(filename: str, dest_dir: str) -> bool:
    """Verify that a model file exists in dest_dir and its SHA-256 matches the pinned checksum."""
    if filename not in PINNED_MODELS:
        raise ValueError(f"Unknown model filename '{filename}'.")

    filepath = os.path.join(dest_dir, filename)
    if not os.path.isfile(filepath):
        return False

    expected_sha256 = PINNED_MODELS[filename]["sha256"].lower()
    actual_sha256 = compute_file_sha256(filepath)
    return actual_sha256 == expected_sha256


def fetch_model(
    filename: str,
    dest_dir: str = DEFAULT_CACHE_DIR,
    force: bool = False,
    timeout: float = 60.0,
    user_agent: str = "QuranReelsAutomation/1.0",
) -> str:
    """Download a pinned model file and verify its SHA-256 checksum.

    Returns the absolute path to the verified model file.
    """
    if filename not in PINNED_MODELS:
        raise ValueError(f"Unknown model filename '{filename}'.")

    meta = PINNED_MODELS[filename]
    url = meta["url"]
    expected_sha256 = meta["sha256"].lower()

    if not url.startswith("https://"):
        raise ValueError(f"Model URL must be HTTPS: {url}")

    os.makedirs(dest_dir, exist_ok=True)
    target_path = os.path.join(dest_dir, filename)

    if not force and verify_model_file(filename, dest_dir):
        return target_path

    ssl_context = _build_ssl_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent},
    )

    # Download to temporary file first to avoid corrupted partial files
    temp_fd, temp_path = tempfile.mkstemp(dir=dest_dir, prefix=f"{filename}.tmp.")
    try:
        with os.fdopen(temp_fd, "wb") as f_out:
            for attempt in range(3):
                f_out.seek(0)
                f_out.truncate()
                try:
                    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                        status_code = getattr(resp, "status", None) or resp.getcode()
                        if status_code != 200:
                            raise urllib.error.HTTPError(url, status_code, "Model source HTTP error", None, None)
                        expected_length = resp.headers.get("Content-Length")
                        downloaded = 0
                        while True:
                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break
                            f_out.write(chunk)
                            downloaded += len(chunk)
                        if expected_length is not None and downloaded < int(expected_length):
                            raise http.client.IncompleteRead(b"", int(expected_length) - downloaded)
                    break
                except urllib.error.HTTPError as error:
                    if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                        raise
                    reason = f"HTTP {error.code}"
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
                    if (isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError)
                            or attempt == 2):
                        raise
                    reason = type(error).__name__
                print(f"Retrying model download: file={filename} attempt={attempt + 2}/3 reason={reason}",
                      file=sys.stderr, flush=True)
                time.sleep(2 ** attempt)

        actual_sha256 = compute_file_sha256(temp_path)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"SHA-256 mismatch for {filename}:\n"
                f"  Expected: {expected_sha256}\n"
                f"  Actual:   {actual_sha256}"
            )

        # Atomic replace
        os.replace(temp_path, target_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return target_path


def fetch_all_models(
    dest_dir: Optional[str] = None,
    force: bool = False,
) -> dict[str, str]:
    """Download and verify all pinned visual safety models."""
    target_dir = dest_dir or DEFAULT_CACHE_DIR
    results: dict[str, str] = {}
    for filename in PINNED_MODELS:
        results[filename] = fetch_model(filename, dest_dir=target_dir, force=force)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify pinned OpenCV Zoo visual safety models.")
    parser.add_argument("--dest", default=DEFAULT_CACHE_DIR, help="Destination directory for model files.")
    parser.add_argument("--check-only", action="store_true", help="Only verify existing models without downloading.")
    parser.add_argument("--force", action="store_true", help="Force redownload even if models exist and verify.")
    args = parser.parse_args()

    dest = os.path.abspath(args.dest)
    print(f"Target model directory: {dest}")

    if args.check_only:
        all_ok = True
        for name in PINNED_MODELS:
            ok = verify_model_file(name, dest)
            status = "OK" if ok else "MISSING OR MISMATCHED"
            print(f"  {name}: {status}")
            if not ok:
                all_ok = False
        return 0 if all_ok else 1

    try:
        for name in PINNED_MODELS:
            print(f"Fetching {name}...")
            path = fetch_model(name, dest_dir=dest, force=args.force)
            print(f"  Verified: {path}")
        print("All visual safety models verified successfully.")
        return 0
    except Exception as err:
        print(f"Error fetching models: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
