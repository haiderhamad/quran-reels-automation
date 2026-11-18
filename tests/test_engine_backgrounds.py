"""Unit tests for the Pexels moving-nature background video source module."""

from __future__ import annotations

import io
import hashlib
import json
import os
import random
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

from engine.backgrounds import (
    ALLOWED_NATURE_QUERIES,
    CORE_NATURE_QUERIES,
    PEXELS_API_SEARCH_URL,
    PIXABAY_API_VIDEO_URL,
    SPECIFIC_NATURE_QUERIES,
    BackgroundCandidate,
    BackgroundSearchConfig,
    BackgroundSourceError,
    DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES,
    DownloadedBackground,
    background_candidate_runtime_rank,
    can_produce_native_vertical_crop,
    candidate_cost_rejection_reason,
    download_background,
    fetch_pexels_video_asset,
    fetch_pixabay_video_asset,
    interleave_nature_query_groups,
    parse_pixabay_video_item,
    parse_pexels_video_item,
    resolve_pexels_api_key,
    resolve_pexels_video_variant,
    resolve_pixabay_api_key,
    resolve_pixabay_video_variant,
    search_background_candidates,
    search_pixabay_background_candidates,
    select_background_candidate,
    validate_nature_query,
)


def make_mock_video_item(
    video_id: int = 12345,
    duration: float = 15.0,
    width: int = 1080,
    height: int = 1920,
    files: list[dict] | None = None,
) -> dict:
    """Create a mock Pexels video item dict."""
    if files is None:
        files = [
            {
                "id": 9901,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1080,
                "height": 1920,
                "fps": 30.0,
                "link": "https://player.vimeo.com/external/12345.hd.mp4",
            }
        ]
    return {
        "id": video_id,
        "width": width,
        "height": height,
        "duration": duration,
        "url": f"https://www.pexels.com/video/nature-scene-{video_id}/",
        "user": {
            "id": 5432,
            "name": "Nature Cinematographer",
            "url": "https://www.pexels.com/@nature",
        },
        "video_files": files,
    }


def make_mock_pixabay_item(
    video_id: int = 54321,
    duration: float = 20.0,
    videos: dict | None = None,
) -> dict:
    if videos is None:
        videos = {
            "large": {
                "url": "https://cdn.pixabay.com/video/2026/08/27/54321-4k.mp4",
                "width": 2160,
                "height": 3840,
                "size": 80_000_000,
                "thumbnail": "https://cdn.pixabay.com/video/2026/08/27/54321-4k.jpg",
            },
            "medium": {
                "url": "https://cdn.pixabay.com/video/2026/08/27/54321-fhd.mp4",
                "width": 1080,
                "height": 1920,
                "size": 18_000_000,
                "thumbnail": "https://cdn.pixabay.com/video/2026/08/27/54321-fhd.jpg",
            },
        }
    return {
        "id": video_id,
        "pageURL": f"https://pixabay.com/videos/id-{video_id}/",
        "duration": duration,
        "user_id": 42,
        "user": "Nature Author",
        "videos": videos,
    }


class BackgroundEngineTests(unittest.TestCase):
    def test_resolve_pexels_api_key(self) -> None:
        # 1. Direct argument
        self.assertEqual(resolve_pexels_api_key("placeholder_direct_key"), "placeholder_direct_key")

        # 2. Primary environment variable PEXELS_API_KEY
        with patch.dict(os.environ, {"PEXELS_API_KEY": "placeholder_env_key"}, clear=True):
            self.assertEqual(resolve_pexels_api_key(), "placeholder_env_key")

        # 3. Legacy fallback PEXELS_API_KEYS (comma separated)
        with patch.dict(os.environ, {"PEXELS_API_KEYS": "placeholder_legacy_1,placeholder_legacy_2"}, clear=True):
            self.assertEqual(resolve_pexels_api_key(), "placeholder_legacy_1")

        # 4. Legacy fallback PEXELS_API_KEYS (JSON list)
        with patch.dict(os.environ, {"PEXELS_API_KEYS": '["placeholder_json_key"]'}, clear=True):
            self.assertEqual(resolve_pexels_api_key(), "placeholder_json_key")

        # 5. Missing key raises error
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(BackgroundSourceError, "Missing Pexels API key"):
                resolve_pexels_api_key()

    def test_resolve_pixabay_api_key_is_server_environment_only(self) -> None:
        self.assertEqual(resolve_pixabay_api_key("placeholder_direct_key"), "placeholder_direct_key")
        with patch.dict(os.environ, {"PIXABAY_API_KEY": "placeholder_env_key"}, clear=True):
            self.assertEqual(resolve_pixabay_api_key(), "placeholder_env_key")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(BackgroundSourceError, "Missing Pixabay API key"):
                resolve_pixabay_api_key()

    def test_validate_nature_query_allowlist(self) -> None:
        for q in ALLOWED_NATURE_QUERIES:
            self.assertEqual(validate_nature_query(q), q)

        # Rejects arbitrary unlisted query
        with self.assertRaisesRegex(BackgroundSourceError, "not in the controlled nature query allow-list"):
            validate_nature_query("supercar driving fast")

        # Rejects prohibited terms
        with self.assertRaisesRegex(BackgroundSourceError, "prohibited subject term"):
            validate_nature_query("people in forest landscape")

        with self.assertRaisesRegex(BackgroundSourceError, "prohibited subject term"):
            validate_nature_query("mosque in mountain landscape")

        with self.assertRaisesRegex(BackgroundSourceError, "non-empty string"):
            validate_nature_query("   ")

    def test_controlled_query_vocabulary_covers_core_and_specific_nature_terms(self) -> None:
        for query in ("nature", "landscape", "forest", "sky", "sunset", "ocean", "space"):
            self.assertIn(query, CORE_NATURE_QUERIES)
            self.assertEqual(validate_nature_query(query), query)
        for query in (
            "moving clouds",
            "rain window nature",
            "mountain landscape",
            "natural texture",
            "flowers macro",
            "fog mist nature",
        ):
            self.assertIn(query, SPECIFIC_NATURE_QUERIES)
            self.assertEqual(validate_nature_query(query), query)

    def test_query_groups_are_normalized_deduplicated_and_interleaved(self) -> None:
        ordered = interleave_nature_query_groups(
            (" Nature ", "SKY", "nature"),
            ("moving   clouds", "sky", "ocean waves"),
        )
        self.assertEqual(
            ordered,
            ("nature", "moving clouds", "sky", "ocean waves"),
        )
        from engine.backgrounds import BACKGROUND_SEARCH_CATEGORIES
        self.assertEqual(ALLOWED_NATURE_QUERIES, tuple(
            query for _, queries in BACKGROUND_SEARCH_CATEGORIES for query in queries))
        self.assertEqual(len(ALLOWED_NATURE_QUERIES), len(set(ALLOWED_NATURE_QUERIES)))
        self.assertTrue(set(CORE_NATURE_QUERIES + SPECIFIC_NATURE_QUERIES) <= set(ALLOWED_NATURE_QUERIES))

    def test_native_vertical_crop_geometry(self) -> None:
        expected = {
            (3840, 2160): True,
            (1920, 1080): False,
            (1080, 1920): True,
            (2160, 3840): True,
            (2160, 2160): True,
            (3414, 1920): True,
            (1920, 1919): False,
            (1079, 4000): False,
        }
        for dimensions, qualifies in expected.items():
            with self.subTest(dimensions=dimensions):
                self.assertEqual(
                    can_produce_native_vertical_crop(*dimensions),
                    qualifies,
                )

    def test_parse_exact_1080x1920_candidate(self) -> None:
        item = make_mock_video_item(video_id=101, width=1080, height=1920)
        cand = parse_pexels_video_item(item, query="mountain landscape")

        self.assertIsNotNone(cand)
        assert cand is not None
        self.assertEqual(cand.pexels_video_id, 101)
        self.assertEqual(cand.query, "mountain landscape")
        self.assertEqual(cand.file_width, 1080)
        self.assertEqual(cand.file_height, 1920)
        self.assertFalse(cand.requires_center_crop)
        self.assertFalse(cand.human_safety_verified)
        self.assertEqual(cand.videographer_name, "Nature Cinematographer")

    def test_parse_candidate_requiring_center_crop(self) -> None:
        # 1200x1920 (aspect ratio 0.625 != 0.5625, height >= 1920)
        files = [
            {
                "id": 9902,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1200,
                "height": 1920,
                "fps": 30.0,
                "link": "https://player.vimeo.com/external/1200x1920.mp4",
            }
        ]
        item = make_mock_video_item(video_id=102, files=files)
        cand = parse_pexels_video_item(item, query="ocean waves")

        self.assertIsNotNone(cand)
        assert cand is not None
        self.assertTrue(cand.requires_center_crop)
        self.assertFalse(cand.human_safety_verified)

    def test_rejects_landscape_only_video(self) -> None:
        files = [
            {
                "id": 9903,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1920,
                "height": 1080,
                "link": "https://player.vimeo.com/external/landscape.mp4",
            }
        ]
        item = make_mock_video_item(video_id=103, files=files)
        cand = parse_pexels_video_item(item, query="forest landscape")
        self.assertIsNone(cand)

    def test_accepts_high_resolution_landscape_video(self) -> None:
        item = make_mock_video_item(video_id=118, files=[{
            "id": 9918,
            "quality": "uhd",
            "file_type": "video/mp4",
            "width": 3840,
            "height": 2160,
            "file_size": 60_000_000,
            "link": "https://player.vimeo.com/external/landscape-4k.mp4",
        }])
        candidate = parse_pexels_video_item(item, query="landscape")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual((candidate.file_width, candidate.file_height), (3840, 2160))
        self.assertTrue(candidate.requires_center_crop)

    def test_pexels_variant_ranking_prefers_portrait_then_square_then_landscape(self) -> None:
        files = [
            {"id": 1, "file_type": "video/mp4", "width": 3840, "height": 2160,
             "link": "https://cdn.example/landscape.mp4"},
            {"id": 2, "file_type": "video/mp4", "width": 2160, "height": 2160,
             "link": "https://cdn.example/square.mp4"},
            {"id": 3, "file_type": "video/mp4", "width": 1200, "height": 1920,
             "link": "https://cdn.example/portrait.mp4"},
        ]
        candidate = parse_pexels_video_item(
            make_mock_video_item(video_id=119, files=files),
            query="nature",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.video_file_id, 3)

        without_portrait = parse_pexels_video_item(
            make_mock_video_item(video_id=120, files=files[:2]),
            query="nature",
        )
        self.assertIsNotNone(without_portrait)
        assert without_portrait is not None
        self.assertEqual(without_portrait.video_file_id, 2)

    def test_pexels_chooses_smallest_sufficient_landscape_variant(self) -> None:
        files = [
            {"id": 1, "file_type": "video/mp4", "width": 7680, "height": 4320,
             "file_size": 63_000_000, "link": "https://cdn.example/8k.mp4"},
            {"id": 2, "file_type": "video/mp4", "width": 3840, "height": 2160,
             "file_size": 50_000_000, "link": "https://cdn.example/4k.mp4"},
        ]
        candidate = parse_pexels_video_item(
            make_mock_video_item(video_id=121, files=files),
            query="landscape",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.video_file_id, 2)

    def test_rejects_square_video(self) -> None:
        files = [
            {
                "id": 9904,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1080,
                "height": 1080,
                "link": "https://player.vimeo.com/external/square.mp4",
            }
        ]
        item = make_mock_video_item(video_id=104, files=files)
        cand = parse_pexels_video_item(item, query="waterfall")
        self.assertIsNone(cand)

    def test_rejects_undersized_video(self) -> None:
        # 720x1280 is rejected by production defaults (requires >= 1080x1920)
        files_720p = [
            {
                "id": 9905,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 720,
                "height": 1280,
                "link": "https://player.vimeo.com/external/720p.mp4",
            }
        ]
        item_720p = make_mock_video_item(video_id=105, files=files_720p)
        self.assertIsNone(parse_pexels_video_item(item_720p, query="waterfall"))

        # 480x854 SD is also rejected
        files_sd = [
            {
                "id": 9906,
                "quality": "sd",
                "file_type": "video/mp4",
                "width": 480,
                "height": 854,
                "link": "https://player.vimeo.com/external/sd.mp4",
            }
        ]
        item_sd = make_mock_video_item(video_id=106, files=files_sd)
        self.assertIsNone(parse_pexels_video_item(item_sd, query="waterfall"))

    def test_rejects_short_duration_video(self) -> None:
        item = make_mock_video_item(video_id=107, duration=2.5)  # Less than default 4.0s
        cand = parse_pexels_video_item(item, query="waterfall", minimum_source_duration=4.0)
        self.assertIsNone(cand)

    def test_rejects_non_https_or_non_mp4(self) -> None:
        files = [
            {
                "id": 9907,
                "quality": "hd",
                "file_type": "video/webm",
                "width": 1080,
                "height": 1920,
                "link": "http://player.vimeo.com/external/insecure.webm",
            }
        ]
        item = make_mock_video_item(video_id=108, files=files)
        cand = parse_pexels_video_item(item, query="waterfall")
        self.assertIsNone(cand)

    def test_prefers_full_hd_over_4k(self) -> None:
        files = [
            {
                "id": 4000,
                "quality": "uhd",
                "file_type": "video/mp4",
                "width": 2160,
                "height": 3840,
                "link": "https://player.vimeo.com/external/4k.mp4",
            },
            {
                "id": 1080,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1080,
                "height": 1920,
                "link": "https://player.vimeo.com/external/1080.mp4",
            },
        ]
        item = make_mock_video_item(video_id=109, files=files)
        cand = parse_pexels_video_item(item, query="mountain clouds")

        self.assertIsNotNone(cand)
        assert cand is not None
        self.assertEqual(cand.video_file_id, 1080)
        self.assertEqual(cand.file_width, 1080)
        self.assertEqual(cand.file_height, 1920)

    def test_prefers_smaller_sufficient_variant_over_oversized_encode(self) -> None:
        files = [
            {
                "id": 4001,
                "quality": "uhd",
                "file_type": "video/mp4",
                "width": 2160,
                "height": 3840,
                "file_size": 150_000_000,
                "link": "https://player.vimeo.com/external/oversized.mp4",
            },
            {
                "id": 1081,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1080,
                "height": 1920,
                "file_size": 24_000_000,
                "link": "https://player.vimeo.com/external/sufficient.mp4",
            },
        ]
        candidate = parse_pexels_video_item(
            make_mock_video_item(video_id=110, files=files),
            query="mountain clouds",
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.video_file_id, 1081)
        self.assertEqual(candidate.declared_file_size_bytes, 24_000_000)

    def test_runtime_rank_prefers_shorter_equivalent_scan_workload(self) -> None:
        short = parse_pexels_video_item(
            make_mock_video_item(video_id=111, duration=20.0),
            query="mountain landscape",
        )
        long = parse_pexels_video_item(
            make_mock_video_item(video_id=112, duration=90.0),
            query="mountain landscape",
        )
        assert short is not None and long is not None

        ordered = sorted(
            (long, short),
            key=lambda candidate: background_candidate_runtime_rank(candidate, 15.0),
        )
        self.assertEqual([candidate.pexels_video_id for candidate in ordered], [111, 112])

    def test_runtime_rank_prefers_portrait_over_landscape(self) -> None:
        portrait = parse_pexels_video_item(
            make_mock_video_item(video_id=123, duration=20.0),
            query="nature",
        )
        landscape = parse_pexels_video_item(
            make_mock_video_item(
                video_id=124,
                duration=20.0,
                files=[{
                    "id": 9924,
                    "quality": "uhd",
                    "file_type": "video/mp4",
                    "width": 3840,
                    "height": 2160,
                    "file_size": 50_000_000,
                    "link": "https://player.vimeo.com/external/landscape-9924.mp4",
                }],
            ),
            query="nature",
        )
        assert portrait is not None and landscape is not None

        ordered = sorted(
            (landscape, portrait),
            key=lambda candidate: background_candidate_runtime_rank(candidate, 15.0),
        )
        self.assertEqual([candidate.pexels_video_id for candidate in ordered], [123, 124])

    def test_cost_guard_rejects_long_or_oversized_before_download(self) -> None:
        short = parse_pexels_video_item(
            make_mock_video_item(video_id=113, duration=20.0),
            query="mountain landscape",
        )
        long = parse_pexels_video_item(
            make_mock_video_item(video_id=114, duration=60.0),
            query="mountain landscape",
        )
        oversized_files = [
            {
                "id": 1082,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1080,
                "height": 1920,
                "file_size": DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES + 1,
                "link": "https://player.vimeo.com/external/too-large.mp4",
            }
        ]
        oversized = parse_pexels_video_item(
            make_mock_video_item(video_id=115, duration=20.0, files=oversized_files),
            query="mountain landscape",
        )
        assert short is not None and long is not None and oversized is not None

        self.assertIsNone(candidate_cost_rejection_reason(short, 15.0))
        self.assertEqual(
            candidate_cost_rejection_reason(long, 15.0),
            "source_duration_too_long",
        )
        self.assertEqual(
            candidate_cost_rejection_reason(oversized, 15.0),
            "source_file_too_large",
        )

    def test_cost_guard_revalidates_format_and_native_crop(self) -> None:
        valid = parse_pexels_video_item(
            make_mock_video_item(video_id=122),
            query="nature",
        )
        assert valid is not None
        unsupported = BackgroundCandidate(**{
            **valid.__dict__,
            "file_type": "video/webm",
            "file_url": "https://cdn.example/video.webm",
        })
        undersized = BackgroundCandidate(**{
            **valid.__dict__,
            "file_width": 1920,
            "file_height": 1080,
        })
        self.assertEqual(
            candidate_cost_rejection_reason(unsupported, 15.0),
            "unsupported_video_format",
        )
        self.assertEqual(
            candidate_cost_rejection_reason(undersized, 15.0),
            "insufficient_native_crop_resolution",
        )

    def test_pool_duration_override_keeps_useful_30_second_clip(self) -> None:
        thirty = parse_pexels_video_item(
            make_mock_video_item(video_id=116, duration=30.0),
            query="ocean waves",
        )
        too_long = parse_pexels_video_item(
            make_mock_video_item(video_id=117, duration=46.0),
            query="ocean waves",
        )
        assert thirty is not None and too_long is not None
        self.assertIsNone(candidate_cost_rejection_reason(
            thirty, 15.0, maximum_source_duration_seconds=45.0,
        ))
        self.assertEqual(candidate_cost_rejection_reason(
            too_long, 15.0, maximum_source_duration_seconds=45.0,
        ), "source_duration_too_long")

    @patch("urllib.request.urlopen")
    def test_search_background_candidates_success(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        response_body = {
            "page": 1,
            "per_page": 15,
            "total_results": 1,
            "videos": [make_mock_video_item(video_id=201)],
        }
        mock_resp.read.return_value = json.dumps(response_body).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        candidates = search_background_candidates(
            query="forest landscape",
            api_key="placeholder_test_key",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].pexels_video_id, 201)
        self.assertEqual(candidates[0].query, "forest landscape")

        # Verify request parameters
        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.full_url.startswith(PEXELS_API_SEARCH_URL))
        self.assertIn("orientation=portrait", req.full_url)
        self.assertIn("size=medium", req.full_url)
        self.assertIn("page=1", req.full_url)
        self.assertEqual(req.headers.get("Authorization"), "placeholder_test_key")

    @patch("urllib.request.urlopen")
    def test_search_background_candidates_with_page_parameter(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        response_body = {
            "page": 2,
            "per_page": 15,
            "total_results": 1,
            "videos": [make_mock_video_item(video_id=202)],
        }
        mock_resp.read.return_value = json.dumps(response_body).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        candidates = search_background_candidates(
            query="mountain landscape",
            page=2,
            api_key="placeholder_test_key",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].pexels_video_id, 202)
        self.assertEqual(candidates[0].search_page, 2)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("page=2", req.full_url)

    @patch("urllib.request.urlopen")
    def test_pexels_landscape_lane_requests_large_search_class(self, mock_urlopen: MagicMock) -> None:
        response = MagicMock(status=200)
        response.read.return_value = json.dumps({"videos": []}).encode()
        response.__enter__.return_value = response
        mock_urlopen.return_value = response

        search_background_candidates(
            query="landscape",
            page=3,
            api_key="placeholder_test_key",
            config=BackgroundSearchConfig(orientation="landscape"),
        )

        request = mock_urlopen.call_args[0][0]
        self.assertIn("orientation=landscape", request.full_url)
        self.assertIn("size=large", request.full_url)
        self.assertIn("page=3", request.full_url)

    @patch("urllib.request.urlopen")
    def test_search_background_candidates_rate_limit(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url=PEXELS_API_SEARCH_URL,
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b"Rate limit exceeded"),
        )

        with self.assertRaisesRegex(BackgroundSourceError, "rate limit exceeded"):
            search_background_candidates(
                query="forest landscape",
                api_key="placeholder_test_key",
            )

    @patch("urllib.request.urlopen")
    def test_search_background_candidates_auth_failure(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url=PEXELS_API_SEARCH_URL,
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b"Unauthorized"),
        )

        with self.assertRaisesRegex(BackgroundSourceError, "authentication failed"):
            search_background_candidates(
                query="forest landscape",
                api_key="placeholder_test_key",
            )

    @patch("urllib.request.urlopen")
    def test_exact_asset_lookup_and_variant_resolution(self, mock_urlopen: MagicMock) -> None:
        asset = make_mock_video_item(video_id=250, files=[
            {"id": 1, "file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://cdn.example/one.mp4"},
            {"id": 2, "file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://cdn.example/two.mp4"},
        ])
        response = MagicMock(status=200)
        response.read.return_value = json.dumps(asset).encode()
        response.__enter__.return_value = response
        mock_urlopen.return_value = response
        current = fetch_pexels_video_asset(250, api_key="placeholder")
        exact = resolve_pexels_video_variant(current, 2)
        self.assertEqual(exact.pexels_video_id, 250)
        self.assertEqual(exact.video_file_id, 2)
        with self.assertRaisesRegex(BackgroundSourceError, "VARIANT_MISSING"):
            resolve_pexels_video_variant(current, 999)

    def test_pixabay_parser_uses_stable_id_and_smallest_sufficient_variant(self) -> None:
        candidate = parse_pixabay_video_item(
            make_mock_pixabay_item(),
            query="moving clouds",
            minimum_source_duration=15.0,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.provider, "pixabay")
        self.assertEqual(candidate.provider_asset_id, "54321")
        self.assertEqual(candidate.provider_variant_id, "medium:1080x1920")
        self.assertEqual(candidate.declared_file_size_bytes, 18_000_000)
        self.assertEqual(candidate.source_page_url, "https://pixabay.com/videos/id-54321/")

    def test_pixabay_parser_rejects_insufficient_variants_before_download(self) -> None:
        item = make_mock_pixabay_item(videos={
            "medium": {
                "url": "https://cdn.pixabay.com/video/tiny.mp4",
                "width": 720,
                "height": 1280,
                "size": 4_000_000,
            },
        })
        self.assertIsNone(parse_pixabay_video_item(item, query="moving clouds"))

    def test_pixabay_accepts_high_res_landscape_and_rejects_full_hd_landscape(self) -> None:
        high_res = make_mock_pixabay_item(videos={
            "large": {
                "url": "https://cdn.pixabay.com/video/landscape-4k.mp4",
                "width": 3840,
                "height": 2160,
                "size": 60_000_000,
            },
        })
        candidate = parse_pixabay_video_item(high_res, query="ocean")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.provider_variant_id, "large:3840x2160")

        full_hd = make_mock_pixabay_item(videos={
            "medium": {
                "url": "https://cdn.pixabay.com/video/landscape-fhd.mp4",
                "width": 1920,
                "height": 1080,
                "size": 20_000_000,
            },
        })
        self.assertIsNone(parse_pixabay_video_item(full_hd, query="ocean"))

    def test_pixabay_chooses_smallest_sufficient_landscape_variant(self) -> None:
        item = make_mock_pixabay_item(videos={
            "large": {
                "url": "https://cdn.pixabay.com/video/large.mp4",
                "width": 4096,
                "height": 2160,
                "size": 62_000_000,
            },
            "medium": {
                "url": "https://cdn.pixabay.com/video/medium.mp4",
                "width": 3840,
                "height": 2160,
                "size": 55_000_000,
            },
        })
        candidate = parse_pixabay_video_item(item, query="nature")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.provider_variant_id, "medium:3840x2160")

    @patch("urllib.request.urlopen")
    def test_pixabay_official_api_pagination_and_24_hour_cache(self, mock_urlopen: MagicMock) -> None:
        response = MagicMock(status=200)
        response.read.return_value = json.dumps({"total": 1, "totalHits": 1, "hits": [make_mock_pixabay_item()]}).encode()
        response.__enter__.return_value = response
        mock_urlopen.return_value = response
        with tempfile.TemporaryDirectory() as cache_dir:
            first = search_pixabay_background_candidates(
                "moving clouds", api_key="placeholder", page=2, cache_dir=cache_dir,
            )
            second = search_pixabay_background_candidates(
                "moving clouds", api_key="placeholder", page=2, cache_dir=cache_dir,
            )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(mock_urlopen.call_count, 1)
        request = mock_urlopen.call_args[0][0]
        self.assertTrue(request.full_url.startswith(PIXABAY_API_VIDEO_URL))
        self.assertIn("page=2", request.full_url)
        self.assertIn("q=moving+clouds", request.full_url)
        self.assertIn("category=nature", request.full_url)
        self.assertIn("safesearch=true", request.full_url)
        self.assertIn("key=placeholder", request.full_url)
        self.assertNotIn("placeholder", str(request.headers))

    @patch("urllib.request.urlopen")
    def test_pixabay_cache_identity_includes_category_query_and_page(self, mock_urlopen: MagicMock) -> None:
        response = MagicMock(status=200)
        response.read.return_value = b'{"total":0,"totalHits":0,"hits":[]}'
        response.__enter__.return_value = response
        mock_urlopen.return_value = response
        identities: list[str] = []

        def capture_identity(cache_dir: str, request_identity: str) -> str:
            identities.append(request_identity)
            return os.path.join(cache_dir, "captured.json")

        with tempfile.TemporaryDirectory() as cache_dir:
            with patch(
                "engine.backgrounds._pixabay_cache_path",
                side_effect=capture_identity,
            ):
                search_pixabay_background_candidates(
                    "forest",
                    api_key="placeholder",
                    page=4,
                    cache_dir=cache_dir,
                )

        self.assertEqual(len(identities), 1)
        identity = urllib.parse.parse_qs(identities[0])
        self.assertEqual(identity["category"], ["nature"])
        self.assertEqual(identity["q"], ["forest"])
        self.assertEqual(identity["page"], ["4"])

    @patch("urllib.request.urlopen")
    def test_pixabay_exact_asset_and_variant_resolution(self, mock_urlopen: MagicMock) -> None:
        item = make_mock_pixabay_item()
        response = MagicMock(status=200)
        response.read.return_value = json.dumps({"total": 1, "totalHits": 1, "hits": [item]}).encode()
        response.__enter__.return_value = response
        mock_urlopen.return_value = response
        asset = fetch_pixabay_video_asset(54321, api_key="placeholder")
        exact = resolve_pixabay_video_variant(asset, "medium:1080x1920", query="moving clouds")
        self.assertEqual(exact.provider, "pixabay")
        self.assertEqual(exact.provider_variant_id, "medium:1080x1920")
        with self.assertRaisesRegex(BackgroundSourceError, "PIXABAY_VARIANT_MISSING"):
            resolve_pixabay_video_variant(asset, "missing:1080x1920")

    def test_select_background_candidate_seeded_and_recent_exclusion(self) -> None:
        cand1 = parse_pexels_video_item(make_mock_video_item(video_id=301), "mountain landscape")
        cand2 = parse_pexels_video_item(make_mock_video_item(video_id=302), "mountain landscape")
        cand3 = parse_pexels_video_item(make_mock_video_item(video_id=303), "mountain landscape")
        assert cand1 and cand2 and cand3

        candidates = (cand1, cand2, cand3)

        # 1. Deterministic selection with seed
        rng1 = random.Random(42)
        sel1 = select_background_candidate(candidates, rng=rng1)
        rng2 = random.Random(42)
        sel2 = select_background_candidate(candidates, rng=rng2)
        self.assertEqual(sel1.pexels_video_id, sel2.pexels_video_id)

        # 2. Exclude recent video IDs
        sel_filtered = select_background_candidate(
            candidates,
            rng=random.Random(42),
            recent_video_ids=[301, 302],
        )
        self.assertEqual(sel_filtered.pexels_video_id, 303)

        # 3. All excluded raises error
        with self.assertRaisesRegex(BackgroundSourceError, "excluded by recent video ID history"):
            select_background_candidate(
                candidates,
                recent_video_ids=[301, 302, 303],
            )

        # 4. Empty candidate list raises error
        with self.assertRaisesRegex(BackgroundSourceError, "No background candidates provided"):
            select_background_candidate(())

    @patch("urllib.request.urlopen")
    def test_download_background_success(self, mock_urlopen: MagicMock) -> None:
        cand = parse_pexels_video_item(make_mock_video_item(video_id=401), "river water")
        assert cand is not None

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "video/mp4"}
        mock_resp.read.side_effect = [b"mock_mp4_bytes_chunk_1", b"mock_mp4_bytes_chunk_2", b""]
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            downloaded = download_background(cand, target_dir=tmpdir)

            self.assertIsInstance(downloaded, DownloadedBackground)
            self.assertEqual(downloaded.candidate.pexels_video_id, 401)
            self.assertTrue(os.path.exists(downloaded.local_path))
            self.assertGreater(downloaded.file_size_bytes, 0)
            self.assertEqual(
                downloaded.content_sha256,
                hashlib.sha256(b"mock_mp4_bytes_chunk_1mock_mp4_bytes_chunk_2").hexdigest(),
            )
            self.assertFalse(downloaded.candidate.human_safety_verified)

    @patch("urllib.request.urlopen")
    def test_download_abort_check_cleans_partial_source(self, mock_urlopen: MagicMock) -> None:
        cand = parse_pexels_video_item(make_mock_video_item(video_id=404), "river water")
        assert cand is not None
        response = MagicMock(status=200)
        response.headers = {"Content-Type": "video/mp4"}
        response.__enter__.return_value = response
        mock_urlopen.return_value = response
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(BackgroundSourceError, "ABORTED"):
                download_background(cand, target_dir=tmpdir, abort_check=lambda: True)
            self.assertEqual(os.listdir(tmpdir), [])

    @patch("urllib.request.urlopen")
    def test_download_background_rejects_empty_file(self, mock_urlopen: MagicMock) -> None:
        cand = parse_pexels_video_item(make_mock_video_item(video_id=402), "river water")
        assert cand is not None

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "video/mp4"}
        mock_resp.read.side_effect = [b""]  # 0 bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(BackgroundSourceError, "0 bytes"):
                download_background(cand, target_dir=tmpdir)

    @patch("urllib.request.urlopen")
    def test_download_rejects_declared_oversized_response_before_streaming(
        self,
        mock_urlopen: MagicMock,
    ) -> None:
        candidate = parse_pexels_video_item(
            make_mock_video_item(video_id=403),
            "river water",
        )
        assert candidate is not None
        response = MagicMock()
        response.status = 200
        response.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(DEFAULT_MAX_SOURCE_FILE_SIZE_BYTES + 1),
        }
        response.__enter__.return_value = response
        mock_urlopen.return_value = response

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(BackgroundSourceError, "maximum permitted"):
                download_background(candidate, target_dir=tmpdir)
        response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
