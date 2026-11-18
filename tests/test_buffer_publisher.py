"""Deterministic tests for the Buffer-only Facebook Reel adapter."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from engine.alignments import normalized_sha256
from engine.buffer_publisher import (
    BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS,
    BufferChannel,
    BufferClient,
    BufferConfig,
    BufferPublisher,
    BufferPublisherError,
    BufferPublishRequest,
    build_buffer_facebook_caption,
    build_buffer_hashtags,
    buffer_facebook_enabled,
    buffer_instagram_enabled,
    buffer_publishing_enabled,
    validate_stable_public_media_url,
)
from scripts import buffer_readiness
from engine.public_media_delivery import PublicMediaDeliveryError, ensure_public_media_delivery
from engine.storage import RunRecord


ARABIC_221 = "علامة اختبارية"
ENGLISH_221 = "Synthetic caption label."


def response(payload: dict, status_code: int = 200):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def record(*, ayah: int = 21, english: str = ENGLISH_221, first: int | None = None, last: int | None = None) -> RunRecord:
    first_ayah = ayah if first is None else first
    last_ayah = ayah if last is None else last
    entries = []
    for number in range(first_ayah, last_ayah + 1):
        arabic = ARABIC_221 if number == 21 else f"عَرَبِيّ {number}"
        translation = english if number == 21 else f"Exact Rowwad {number}."
        entries.append({
            "verse_key": f"2:{number}",
            "arabic_text": arabic,
            "arabic_normalized_sha256": normalized_sha256(arabic),
            "translations": {
                "en": {
                    "text": translation,
                    "normalized_sha256": normalized_sha256(translation),
                    "translation_key": "english_rwwad",
                    "translation_version": "1.0.19",
                },
            },
        })
    return RunRecord(
        run_id="manual_20260902_abcdef12",
        date="2026-09-02",
        slot="manual-production",
        run_kind="manual_production",
        status="SUCCESS",
        started_at="2026-09-02T10:00:00+00:00",
        completed_at="2026-09-02T10:00:10+00:00",
        surah_number=2,
        surah_name_ar="البقرة",
        surah_name_en="Al-Baqarah",
        first_ayah=first_ayah,
        last_ayah=last_ayah,
        media_key="quran-reels/media/manual-production/manual_20260902_abcdef12.mp4",
        final_safety_passed=True,
        provenance={"caption_ayahs": entries},
    )


from tests.editorial_fixtures import eligible_buffer_request


class TestBufferCaption(unittest.TestCase):
    def test_single_ayah_caption_is_exact_and_deterministic(self) -> None:
        caption = build_buffer_facebook_caption(record())
        self.assertEqual(
            caption,
            "سورة البقرة — الآية 21\n\n"
            f"«{ARABIC_221}»\n\nالقارئ: ياسر الدوسري\n\n\n"
            "Al-Baqarah\n\n"
            f"“{ENGLISH_221}”\n\nReciter: Yasser Al-Dosari\n\n"
            "Ayah 21\n\n"
            "Arabic text: Quran Foundation\n"
            "Translation: Rowwad Translation Center via QuranEnc.com (English 1.0.19)\n\n"
            "#Quran #QuranDaily #Ayah #AlBaqarah #QuranVerse #ياسر_الدوسري",
        )
        self.assertEqual(normalized_sha256(ARABIC_221), "02e893c38b29e0318bfbdc80003be716aa9e26c3f07c93733a2a9add96ed2462")
        self.assertEqual(normalized_sha256(ENGLISH_221), "6d6346bbd26a686e0b4cb8136c357c3c93de2f4a1acf4214526f85d653a3eac4")

    def test_credit_names_all_and_only_verified_render_languages(self):
        rec = record(first=21, last=22)
        for row in rec.provenance["caption_ayahs"]:
            row["translations"]["de"] = {"text": "Prüfbeispiel", "normalized_sha256": normalized_sha256("Prüfbeispiel"),
                                         "translation_key": "german_rwwad", "translation_version": "1.0.15"}
        self.assertIn("English 1.0.19, German 1.0.15", build_buffer_facebook_caption(rec))
        rec.provenance["caption_ayahs"][1]["translations"]["de"]["translation_version"] = "unknown"
        with self.assertRaises(BufferPublisherError):
            build_buffer_facebook_caption(rec)
        del rec.provenance["caption_ayahs"][1]["translations"]["de"]
        with self.assertRaises(BufferPublisherError):
            build_buffer_facebook_caption(rec)

    def test_range_keeps_full_ayahs_without_intervening_prose(self) -> None:
        caption = build_buffer_facebook_caption(record(first=21, last=22))
        self.assertIn("سورة البقرة — الآيات 21–22", caption)
        self.assertIn("Reciter: Yasser Al-Dosari\n\nAyahs 21–22\n\n", caption)
        self.assertIn("#QuranVerses", caption)
        self.assertNotIn("Recitation:", caption)
        self.assertIn(f"«{ARABIC_221}\nعَرَبِيّ 22»\n\nالقارئ: ياسر الدوسري\n\n\nAl-Baqarah\n\n", caption)
        self.assertIn(f"“{ENGLISH_221}\nExact Rowwad 22.”\n\nReciter: Yasser Al-Dosari\n\n", caption)
        self.assertEqual(caption.split().count("#ياسر_الدوسري"), 1)

    def test_quotes_preserve_source_bytes_and_provenance(self) -> None:
        import copy
        for last in (21, 22):
            with self.subTest(last=last):
                rec = record(first=21, last=last)
                before = copy.deepcopy(rec.provenance)
                caption = build_buffer_facebook_caption(rec)
                entries = before["caption_ayahs"]
                arabic = "\n".join(item["arabic_text"] for item in entries)
                english = "\n".join(item["translations"]["en"]["text"] for item in entries)
                self.assertEqual(caption.split("«", 1)[1].split("»", 1)[0].encode("utf-8"), arabic.encode("utf-8"))
                self.assertIn(f"“{english}”\n\nReciter: Yasser Al-Dosari", caption)
                self.assertEqual(caption.count("«"), 1)
                self.assertEqual(caption.count("»"), 1)
                self.assertNotIn("﴿", caption)
                self.assertNotIn("﴾", caption)
                self.assertEqual(rec.provenance, before)

    def test_hashtag_normalization_and_count(self) -> None:
        self.assertEqual(build_buffer_hashtags("Ya-Sin", 1, 1)[3], "#YaSin")
        self.assertEqual(build_buffer_hashtags("Al-Kahf", 1, 2)[3], "#AlKahf")
        self.assertEqual(len(build_buffer_hashtags("Al-Baqarah", 21, 21)), 6)

    def test_reciter_hashtag_is_permanent_and_unique(self) -> None:
        for first, last in ((21, 21), (21, 22)):
            self.assertEqual(build_buffer_hashtags("Al-Baqarah", first, last).count("#ياسر_الدوسري"), 1)
            self.assertEqual(build_buffer_facebook_caption(record(first=first, last=last)).split().count("#ياسر_الدوسري"), 1)

    def test_attribution_counts_toward_facebook_length_limit(self) -> None:
        base = record(english="x")
        overhead = len(build_buffer_facebook_caption(base).encode("utf-16-le")) // 2 - 1
        exact_text = "x" * (BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS - overhead)
        self.assertEqual(len(build_buffer_facebook_caption(record(english=exact_text)).encode("utf-16-le")) // 2,
                         BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS)
        with self.assertRaisesRegex(BufferPublisherError, "CAPTION_TOO_LONG_FOR_FACEBOOK"):
            build_buffer_facebook_caption(record(english=exact_text + "x"))

    def test_english_source_tampering_fails_closed(self) -> None:
        for field, value in (("text", "Altered translation"), ("translation_key", "other"),
                             ("translation_version", "wrong"), ("normalized_sha256", "0" * 64)):
            with self.subTest(field=field):
                rec = record()
                rec.provenance["caption_ayahs"][0]["translations"]["en"][field] = value
                with self.assertRaisesRegex(BufferPublisherError, "English Rowwad source"):
                    build_buffer_facebook_caption(rec)

    def test_caption_source_hash_tampering_fails_closed(self) -> None:
        rec = record()
        broken = dict(rec.provenance)
        broken["caption_ayahs"] = [dict(broken["caption_ayahs"][0], arabic_normalized_sha256="0" * 64)]
        with self.assertRaisesRegex(BufferPublisherError, "Arabic source hash"):
            build_buffer_facebook_caption(RunRecord(**{**rec.__dict__, "provenance": broken}))

    def test_caption_too_long_never_truncates(self) -> None:
        with self.assertRaisesRegex(BufferPublisherError, "CAPTION_TOO_LONG_FOR_FACEBOOK"):
            build_buffer_facebook_caption(record(english="x" * (BUFFER_FACEBOOK_CAPTION_MAX_UTF16_UNITS + 1)))


class TestBufferClient(unittest.TestCase):
    def setUp(self) -> None:
        from tests.editorial_fixtures import mock_live_authorization
        mock_live_authorization(self)
        self.config = BufferConfig(api_key="placeholder", organization_id="org_1", facebook_channel_id="channel_1")

    def test_org_and_facebook_page_discovery(self) -> None:
        http_post = Mock(side_effect=[
            response({"data": {"account": {"organizations": [{"id": "org_1", "name": "Quran"}]}}}),
            response({"data": {"channels": [{
                "id": "channel_1", "displayName": "Quran Page", "service": "facebook",
                "descriptor": "Facebook Page", "type": "page", "isDisconnected": False,
            }]}}),
        ])
        org, channel = BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).resolve_facebook_channel()
        self.assertEqual(org.id, "org_1")
        self.assertEqual(channel.display_name, "Quran Page")
        self.assertTrue(channel.is_facebook_page)

    def test_readiness_enumerates_facebook_and_instagram_without_mutation(self) -> None:
        http_post = Mock(side_effect=[
            response({"data": {"account": {"organizations": [{"id": "org_1", "name": "Quran"}]}}}),
            response({"data": {"channels": [
                {
                    "id": "facebook_1", "displayName": "Quran Reels", "service": "facebook",
                    "descriptor": "Facebook Page", "type": "page", "isDisconnected": False, "isLocked": False,
                },
                {
                    "id": "instagram_1", "displayName": "Quran Reels IG", "service": "instagram",
                    "descriptor": "Instagram Professional", "type": "business", "isDisconnected": False, "isLocked": False,
                },
                {"id": "other_1", "displayName": "Other", "service": "linkedin", "type": "page"},
            ]}}),
        ])
        config = BufferConfig(api_key="placeholder", organization_id="org_1", facebook_channel_id="facebook_1")
        client = BufferClient(config, http_post=http_post, sleep_fn=Mock())
        organization, channels = client.read_connected_channels()
        self.assertEqual(organization.id, "org_1")
        self.assertEqual(len(channels), 3)
        self.assertEqual(client.select_facebook_channel(channels).id, "facebook_1")
        instagram = client.select_instagram_channel(channels)
        self.assertEqual(instagram.id, "instagram_1")
        self.assertTrue(instagram.is_instagram)
        self.assertTrue(instagram.is_available)
        self.assertTrue(all("mutation" not in call.kwargs["json"]["query"].lower() for call in http_post.call_args_list))

    def test_readiness_facebook_only_reports_instagram_not_visible(self) -> None:
        facebook = BufferChannel("facebook_1", "Quran Reels", "facebook", "Facebook Page", "page")
        with self.assertRaisesRegex(BufferPublisherError, "INSTAGRAM_CHANNEL_NOT_VISIBLE"):
            BufferClient(self.config, http_post=Mock(), sleep_fn=Mock()).select_instagram_channel((facebook,))

    def test_readiness_multiple_instagram_channels_requires_selection(self) -> None:
        channels = (
            BufferChannel("instagram_1", "One", "instagram", "Instagram Professional", "business"),
            BufferChannel("instagram_2", "Two", "instagram", "Instagram Professional", "business"),
        )
        with self.assertRaisesRegex(BufferPublisherError, "MULTIPLE_INSTAGRAM_CHANNELS"):
            BufferClient(self.config, http_post=Mock(), sleep_fn=Mock()).select_instagram_channel(channels)

    def test_readiness_reports_disconnected_or_locked_instagram(self) -> None:
        disconnected = BufferChannel("instagram_1", "Quran", "instagram", is_disconnected=True)
        locked = BufferChannel("instagram_2", "Quran", "instagram", is_locked=True)
        for channel in (disconnected, locked):
            with self.subTest(channel=channel.id):
                with self.assertRaisesRegex(BufferPublisherError, "INSTAGRAM_CHANNEL_UNAVAILABLE"):
                    BufferClient(self.config, http_post=Mock(), sleep_fn=Mock()).select_instagram_channel((channel,))

    def test_readiness_script_reports_all_channels_without_secret_output(self) -> None:
        channels = (
            BufferChannel("facebook_1", "Quran Reels", "facebook", "Facebook Page", "page"),
            BufferChannel("instagram_1", "Quran Reels IG", "instagram", "Instagram Professional", "business"),
            BufferChannel("other_1", "Other\nChannel", "linkedin", "LinkedIn Page", "page"),
        )
        fake_client = Mock()
        fake_client.read_connected_channels.return_value = (SimpleNamespace(id="org_1"), channels)
        fake_client.select_facebook_channel.return_value = channels[0]
        fake_client.select_instagram_channel.return_value = channels[1]
        output = io.StringIO()
        with patch.object(buffer_readiness, "load_buffer_config_from_env", return_value=object()), \
             patch.object(buffer_readiness, "BufferClient", return_value=fake_client), \
             redirect_stdout(output):
            self.assertEqual(buffer_readiness.main(), 0)
        rendered = output.getvalue()
        self.assertIn("BUFFER_CHANNEL_COUNT=3", rendered)
        self.assertIn("service=instagram", rendered)
        self.assertIn("service=linkedin", rendered)
        self.assertNotIn("api_key", rendered.lower())
        self.assertNotIn("Other\nChannel", rendered)
        self.assertIn("display_name=Other Channel", rendered)

    def test_invalid_token_is_not_retried_or_logged(self) -> None:
        http_post = Mock(return_value=response({"errors": [{"message": "bad", "extensions": {"code": "UNAUTHORIZED"}}]}))
        with self.assertRaisesRegex(BufferPublisherError, "AUTH_ERROR") as ctx:
            BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).organizations()
        self.assertNotIn("placeholder", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "AUTH_ERROR")
        self.assertEqual(ctx.exception.message, "Buffer authentication was rejected: bad")
        self.assertEqual(http_post.call_count, 1)

    def test_transient_timeout_retries_then_succeeds(self) -> None:
        http_post = Mock(side_effect=[
            requests.Timeout("timeout"),
            response({"data": {"account": {"organizations": [{"id": "org_1", "name": "Quran"}]}}}),
        ])
        sleeper = Mock()
        organizations = BufferClient(self.config, http_post=http_post, sleep_fn=sleeper).organizations()
        self.assertEqual(organizations[0].id, "org_1")
        self.assertEqual(http_post.call_count, 2)
        sleeper.assert_called_once_with(2.0)

    def test_http_503_retries_but_invalid_input_does_not(self) -> None:
        http_post = Mock(side_effect=[
            response({}, status_code=503),
            response({"data": {"account": {"organizations": [{"id": "org_1", "name": "Quran"}]}}}),
        ])
        self.assertEqual(BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).organizations()[0].id, "org_1")
        invalid = Mock(return_value=response({"data": {"createPost": {"message": "caption invalid"}}}))
        with self.assertRaisesRegex(BufferPublisherError, "BUFFER_INVALID_POST"):
            BufferClient(self.config, http_post=invalid, sleep_fn=Mock()).create_facebook_reel(
                "channel_1",
                eligible_buffer_request("run", "caption", "https://dashboard.example/a/b/video.mp4"),
            )
        self.assertEqual(invalid.call_count, 1)

    def test_exhausted_transient_error_is_bounded(self) -> None:
        http_post = Mock(side_effect=requests.Timeout("network unavailable"))
        sleeper = Mock()
        with self.assertRaisesRegex(BufferPublisherError, "BUFFER_TRANSIENT_ERROR"):
            BufferClient(self.config, http_post=http_post, sleep_fn=sleeper).organizations()
        self.assertEqual(http_post.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_create_uses_one_facebook_reel_post(self) -> None:
        http_post = Mock(return_value=response({"data": {"createPost": {"post": {"id": "post_1", "status": "buffer"}}}}))
        client = BufferClient(self.config, http_post=http_post, sleep_fn=Mock())
        post = client.create_facebook_reel("channel_1", eligible_buffer_request(
            run_id="manual_20260902_abcdef12",
            caption="caption",
            public_media_url="https://dashboard.example/api/public-media/run/delivery/video.mp4",
        ))
        self.assertEqual(post.id, "post_1")
        payload = http_post.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(payload["metadata"], {"facebook": {"type": "reel"}})
        self.assertEqual(payload["assets"], [{"video": {"url": "https://dashboard.example/api/public-media/run/delivery/video.mp4"}}])
        self.assertNotIn("post", payload["metadata"]["facebook"])
        self.assertEqual(payload["mode"], "shareNow")
        self.assertEqual(len(http_post.call_args_list), 1)

    def test_create_uses_target_scoped_instagram_reel_metadata(self) -> None:
        http_post = Mock(return_value=response({"data": {"createPost": {"post": {"id": "post_ig", "status": "buffer"}}}}))
        post = BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).create_instagram_reel(
            "instagram_1", eligible_buffer_request("run", "caption", "https://dashboard.example/a/video.mp4")
        )
        self.assertEqual(post.id, "post_ig")
        payload = http_post.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(payload["channelId"], "instagram_1")
        self.assertEqual(payload["metadata"], {"instagram": {"type": "reel", "shouldShareToFeed": True}})

    def test_create_timeout_is_unknown_and_never_retried(self) -> None:
        http_post = Mock(side_effect=requests.Timeout("after request body"))
        with self.assertRaisesRegex(BufferPublisherError, "BUFFER_PUBLISH_OUTCOME_UNKNOWN") as raised:
            BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).create_facebook_reel(
                "channel_1", eligible_buffer_request("run", "caption", "https://dashboard.example/a/video.mp4")
            )
        self.assertTrue(raised.exception.possibly_dispatched)
        self.assertEqual(http_post.call_count, 1)

    def test_create_rate_limit_is_definitely_not_dispatched_and_not_retried(self) -> None:
        http_post = Mock(return_value=response({}, status_code=429))
        with self.assertRaisesRegex(BufferPublisherError, "BUFFER_RATE_LIMITED") as raised:
            BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).create_facebook_reel(
                "channel_1", eligible_buffer_request("run", "caption", "https://dashboard.example/a/video.mp4")
            )
        self.assertFalse(raised.exception.possibly_dispatched)
        self.assertEqual(http_post.call_count, 1)

    def test_reconciliation_uses_documented_post_identity_across_pages(self) -> None:
        media_url = "https://dashboard.example/api/public-media/run/delivery/video.mp4"
        http_post = Mock(side_effect=[
            response({"data": {"posts": {
                "edges": [{"node": {"id": "unrelated", "channelId": "channel_1", "text": "other", "assets": []}}],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            }}}),
            response({"data": {"posts": {
                "edges": [{"node": {"id": "post_1", "channelId": "channel_1", "text": "caption", "assets": [{"source": media_url}], "createdAt": "2026-09-03T00:00:00Z"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}),
        ])
        result = BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).reconcile_publication(
            "org_1", "channel_1", "caption", media_url, not_before="2026-09-02T00:00:00Z"
        )
        self.assertEqual(result.outcome, "FOUND")
        self.assertEqual(result.post_id, "post_1")
        self.assertEqual(http_post.call_count, 2)
        request = http_post.call_args_list[0].kwargs["json"]
        self.assertIn("posts(first: $first, after: $after", request["query"])
        self.assertNotIn("node { id source", request["query"])
        self.assertEqual(request["variables"]["input"]["filter"]["channelIds"], ["channel_1"])
        self.assertEqual(request["variables"]["input"]["filter"]["startDate"], "2026-09-02T00:00:00Z")

    def test_reconciliation_absence_requires_a_complete_page_set(self) -> None:
        http_post = Mock(return_value=response({"data": {"posts": {
            "edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}))
        result = BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).reconcile_publication(
            "org_1", "channel_1", "caption", "https://dashboard.example/a/video.mp4"
        )
        self.assertEqual(result.outcome, "ABSENT")

    def test_reconciliation_pagination_or_api_failure_is_indeterminate(self) -> None:
        paginated = Mock(return_value=response({"data": {"posts": {
            "edges": [], "pageInfo": {"hasNextPage": True, "endCursor": None},
        }}}))
        result = BufferClient(self.config, http_post=paginated, sleep_fn=Mock()).reconcile_publication(
            "org_1", "channel_1", "caption", "https://dashboard.example/a/video.mp4"
        )
        self.assertEqual(result.outcome, "INDETERMINATE")
        failing = Mock(return_value=response({"errors": [{"message": "temporary", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}))
        result = BufferClient(self.config, http_post=failing, sleep_fn=Mock()).reconcile_publication(
            "org_1", "channel_1", "caption", "https://dashboard.example/a/video.mp4"
        )
        self.assertEqual(result.outcome, "INDETERMINATE")

    def test_draft_reconciliation_never_matches_public_posts(self) -> None:
        http_post = Mock(return_value=response({"data": {"posts": {
            "edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}))
        BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).reconcile_publication(
            "org_1", "channel_1", "caption", "https://dashboard.example/a/video.mp4", drafts_only=True
        )
        self.assertEqual(http_post.call_args.kwargs["json"]["variables"]["input"]["filter"]["status"], ["draft"])

    def test_disconnected_channel_is_rejected_before_write(self) -> None:
        http_post = Mock(side_effect=[
            response({"data": {"account": {"organizations": [{"id": "org_1", "name": "Quran"}]}}}),
            response({"data": {"channels": [{"id": "channel_1", "service": "facebook", "descriptor": "Facebook Page", "type": "page", "isDisconnected": True}]}}),
        ])
        with self.assertRaisesRegex(BufferPublisherError, "CHANNEL_UNAVAILABLE"):
            BufferClient(self.config, http_post=http_post, sleep_fn=Mock()).resolve_facebook_channel()

    def test_presigned_media_is_rejected_before_request(self) -> None:
        with self.assertRaisesRegex(BufferPublisherError, "MEDIA_URL_UNREACHABLE"):
            validate_stable_public_media_url("https://b2.example/video.mp4?X-Amz-Signature=secret")

    def test_uncoordinated_public_facade_never_calls_buffer(self) -> None:
        existing = RunRecord(**{**record().__dict__, "facebook_status": "SUCCESS", "buffer_post_id": "post_existing"})
        http_post = Mock()
        result = BufferPublisher(BufferClient(self.config, http_post=http_post, sleep_fn=Mock())).publish_facebook_reel(
            existing,
            public_media_url="https://dashboard.example/api/public-media/run/delivery/video.mp4",
        )
        self.assertEqual(result.facebook.status, "FAILED")
        self.assertEqual(result.facebook.failure_reason, "BUFFER_DURABLE_LIFECYCLE_REQUIRED")
        self.assertEqual(result.instagram.status, "NOT_ATTEMPTED")
        http_post.assert_not_called()

    def test_final_safety_failure_never_calls_buffer(self) -> None:
        unsafe = RunRecord(**{**record().__dict__, "final_safety_passed": False})
        http_post = Mock()
        result = BufferPublisher(BufferClient(self.config, http_post=http_post, sleep_fn=Mock())).publish_facebook_reel(
            unsafe,
            public_media_url="https://dashboard.example/api/public-media/run/delivery/video.mp4",
        )
        self.assertEqual(result.facebook.status, "FAILED")
        self.assertEqual(result.instagram.status, "NOT_ATTEMPTED")
        http_post.assert_not_called()


class TestPublicMediaDelivery(unittest.TestCase):
    def test_delivery_is_stable_and_only_final_safe(self) -> None:
        updated, url = ensure_public_media_delivery(record(), {"PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://dashboard.example"})
        self.assertTrue(updated.public_media_delivery_id)
        self.assertTrue(url.endswith("/video.mp4"))
        again, repeated = ensure_public_media_delivery(updated, {"PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://dashboard.example"})
        self.assertEqual(again.public_media_delivery_id, updated.public_media_delivery_id)
        self.assertEqual(repeated, url)

    def test_missing_origin_or_failed_safety_cannot_be_exposed(self) -> None:
        with self.assertRaises(PublicMediaDeliveryError):
            ensure_public_media_delivery(record(), {})
        unsafe = RunRecord(**{**record().__dict__, "final_safety_passed": False})
        with self.assertRaises(PublicMediaDeliveryError):
            ensure_public_media_delivery(unsafe, {"PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://dashboard.example"})
        with self.assertRaises(PublicMediaDeliveryError):
            ensure_public_media_delivery(record(), {"PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://dashboard.example/not-an-origin"})

    def test_explicit_buffer_flags_are_required(self) -> None:
        self.assertFalse(buffer_publishing_enabled({"PUBLISHER_BACKEND": "buffer", "BUFFER_API_KEY": "present"}))
        self.assertTrue(buffer_publishing_enabled({
            "PUBLISHER_BACKEND": "buffer", "BUFFER_PUBLISHING_ENABLED": "true", "BUFFER_FACEBOOK_ENABLED": "true",
        }))

    def test_buffer_platform_flags_independent(self) -> None:
        env_fb = {"PUBLISHER_BACKEND": "buffer", "BUFFER_PUBLISHING_ENABLED": "true", "BUFFER_FACEBOOK_ENABLED": "true"}
        self.assertTrue(buffer_facebook_enabled(env_fb))
        self.assertFalse(buffer_instagram_enabled(env_fb))

        env_ig = {"PUBLISHER_BACKEND": "buffer", "BUFFER_PUBLISHING_ENABLED": "true", "BUFFER_INSTAGRAM_ENABLED": "true"}
        self.assertFalse(buffer_facebook_enabled(env_ig))
        self.assertTrue(buffer_instagram_enabled(env_ig))

        env_both = {"PUBLISHER_BACKEND": "buffer", "BUFFER_PUBLISHING_ENABLED": "true", "BUFFER_FACEBOOK_ENABLED": "true", "BUFFER_INSTAGRAM_ENABLED": "true"}
        self.assertTrue(buffer_facebook_enabled(env_both))
        self.assertTrue(buffer_instagram_enabled(env_both))
        self.assertTrue(buffer_publishing_enabled(env_both))

    def test_resolve_instagram_channel(self) -> None:
        http_post = Mock(side_effect=[
            response({"data": {"account": {"organizations": [{"id": "org_1", "name": "Quran"}]}}}),
            response({"data": {"channels": [
                {"id": "channel_fb", "service": "facebook", "descriptor": "FB", "type": "page", "isDisconnected": False},
                {"id": "6a9875b2065799be467697d5", "service": "instagram", "descriptor": "quran.rreels", "type": "business", "isDisconnected": False},
            ]}}),
        ])
        config = BufferConfig(api_key="tok", instagram_channel_id="6a9875b2065799be467697d5")
        client = BufferClient(config, http_post=http_post, sleep_fn=Mock())
        org, channel = client.resolve_instagram_channel()
        self.assertEqual(org.id, "org_1")
        self.assertEqual(channel.id, "6a9875b2065799be467697d5")
        self.assertTrue(channel.is_instagram)

    def test_independent_target_claims_and_no_facebook_republish(self) -> None:
        from engine.buffer_lifecycle import BufferPublicationCoordinator, publication_fingerprint
        claims = {}
        class MockStore:
            def claim_buffer_publication_dispatch(self, claim):
                key = (claim["run_id"], claim["platform"], claim["publisher_backend"], claim["channel_id"])
                if key in claims:
                    return {**claims[key], "_claim_created": False}
                claims[key] = {**claim, "state": "DISPATCHING"}
                return {**claims[key], "_claim_created": True}
            def transition_buffer_publication_claim(self, run_id, platform, backend, channel_id, expected, state, updates):
                key = (run_id, platform, backend, channel_id)
                if key in claims:
                    claims[key]["state"] = state
                    claims[key].update(updates)
                    return claims[key]
                return None
            def get_buffer_publication_claim(self, run_id, platform, backend, channel_id):
                return claims.get((run_id, platform, backend, channel_id))

        store = MockStore()
        # 1. Target Facebook and succeed
        coord_fb = BufferPublicationCoordinator(store, platform="facebook", publisher_backend="buffer", channel_id="fb_chan")
        c1 = coord_fb.claim(run_id="run_test_indep", fingerprint="fp_fb", source="src_fb")
        self.assertTrue(c1["_claim_created"])
        coord_fb.mark_success("run_test_indep", post_id="post_fb_1", external_link="https://fb.com/1", accepted_at="2026-09-03T00:00:00Z")

        # 2. Target Instagram independently on the same run
        coord_ig = BufferPublicationCoordinator(store, platform="instagram", publisher_backend="buffer", channel_id="ig_chan")
        c2 = coord_ig.claim(run_id="run_test_indep", fingerprint="fp_ig", source="src_ig")
        self.assertTrue(c2["_claim_created"])
        self.assertEqual(claims[("run_test_indep", "facebook", "buffer", "fb_chan")]["state"], "SUCCESS")
        self.assertEqual(claims[("run_test_indep", "instagram", "buffer", "ig_chan")]["state"], "DISPATCHING")

        # 3. Retrying Facebook returns existing SUCCESS without re-creating claim
        c3 = coord_fb.claim(run_id="run_test_indep", fingerprint="fp_fb", source="src_fb")
        self.assertFalse(c3["_claim_created"])
        self.assertEqual(c3["state"], "SUCCESS")
