"""Unit tests for Backblaze B2 storage layer (engine/storage.py)."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from engine.storage import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_B2_PREFIX,
    DEFAULT_PRESIGNED_EXPIRY_SECONDS,
    B2Config,
    PublishingScheduleEntry,
    RunRecord,
    StorageError,
    StoredConfig,
    build_config_key,
    build_media_key,
    build_run_key,
    build_scheduler_claim_key,
    create_presigned_get_url,
    delete_object,
    get_recent_content_history,
    get_run_record,
    get_run_record_by_key,
    list_recent_runs,
    load_config,
    load_b2_config_from_env,
    object_exists,
    put_run_record,
    run_record_from_dict,
    run_record_to_dict,
    save_config,
    stored_config_from_dict,
    stored_config_to_dict,
    upload_reel,
)


def make_mock_client_error(code: str = "NoSuchKey", message: str = "The specified key does not exist.") -> ClientError:
    """Helper to create realistic botocore ClientError."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": message}},
        operation_name="GetObject",
    )


class TestB2ConfigAndEnv(unittest.TestCase):
    """Test B2Config validation and environment variable loading."""

    def test_b2_config_valid(self) -> None:
        cfg = B2Config(
            endpoint_url="https://s3.eu-central-003.backblazeb2.com",
            region_name="eu-central-003",
            application_key_id="key123",
            application_key="sec123",
            bucket_name="my-bucket",
            prefix="custom-prefix/",
        )
        self.assertEqual(cfg.endpoint_url, "https://s3.eu-central-003.backblazeb2.com")
        self.assertEqual(cfg.region_name, "eu-central-003")
        self.assertEqual(cfg.application_key_id, "key123")
        self.assertEqual(cfg.application_key, "sec123")
        self.assertEqual(cfg.bucket_name, "my-bucket")
        self.assertEqual(cfg.prefix, "custom-prefix/")

    def test_b2_config_prefix_cleaning(self) -> None:
        cfg = B2Config(
            endpoint_url="https://s3.eu-central-003.backblazeb2.com",
            region_name="eu-central-003",
            application_key_id="key123",
            application_key="sec123",
            bucket_name="my-bucket",
            prefix="/some/prefix",
        )
        self.assertEqual(cfg.prefix, "some/prefix/")

    def test_b2_config_rejection_of_auto_region(self) -> None:
        with self.assertRaises(StorageError) as ctx:
            B2Config(
                endpoint_url="https://s3.eu-central-003.backblazeb2.com",
                region_name="auto",
                application_key_id="key123",
                application_key="sec123",
                bucket_name="my-bucket",
            )
        self.assertIn("auto", str(ctx.exception).lower())

    def test_b2_config_missing_or_invalid_fields(self) -> None:
        with self.assertRaises(StorageError):
            B2Config("", "eu-central-003", "key", "sec", "bucket")
        with self.assertRaises(StorageError):
            B2Config("https://s3.eu-central-003.backblazeb2.com", "", "key", "sec", "bucket")
        with self.assertRaises(StorageError):
            B2Config("https://s3.eu-central-003.backblazeb2.com", "eu-central-003", "", "sec", "bucket")
        with self.assertRaises(StorageError):
            B2Config("https://s3.eu-central-003.backblazeb2.com", "eu-central-003", "key", "", "bucket")
        with self.assertRaises(StorageError):
            B2Config("https://s3.eu-central-003.backblazeb2.com", "eu-central-003", "key", "sec", "")

    def test_load_b2_config_from_env_valid(self) -> None:
        env = {
            "B2_ENDPOINT": "https://s3.eu-central-003.backblazeb2.com",
            "B2_REGION": "eu-central-003",
            "B2_APPLICATION_KEY_ID": "env_key",
            "B2_APPLICATION_KEY": "env_sec",
            "B2_BUCKET_NAME": "env_bucket",
            "B2_PREFIX": "quran-reels/",
        }
        cfg = load_b2_config_from_env(env)
        self.assertEqual(cfg.endpoint_url, "https://s3.eu-central-003.backblazeb2.com")
        self.assertEqual(cfg.region_name, "eu-central-003")
        self.assertEqual(cfg.application_key_id, "env_key")
        self.assertEqual(cfg.application_key, "env_sec")
        self.assertEqual(cfg.bucket_name, "env_bucket")
        self.assertEqual(cfg.prefix, "quran-reels/")

    def test_load_b2_config_from_env_missing_vars(self) -> None:
        env = {
            "B2_ENDPOINT": "https://s3.eu-central-003.backblazeb2.com",
            # Missing other keys
        }
        with self.assertRaises(StorageError) as ctx:
            load_b2_config_from_env(env)
        self.assertIn("Missing required Backblaze B2 environment variables", str(ctx.exception))
        self.assertIn("B2_REGION", str(ctx.exception))
        self.assertIn("B2_APPLICATION_KEY_ID", str(ctx.exception))
        self.assertIn("B2_APPLICATION_KEY", str(ctx.exception))
        self.assertIn("B2_BUCKET_NAME", str(ctx.exception))


class TestStoredConfigModel(unittest.TestCase):
    """Test StoredConfig default values, validation rules, and JSON serialization."""

    def test_stored_config_safe_defaults(self) -> None:
        cfg = StoredConfig()
        self.assertFalse(cfg.automation_enabled)  # Safe default: False
        self.assertEqual(cfg.timezone, "Europe/Berlin")
        self.assertTrue(cfg.morning_enabled)
        self.assertIsNone(cfg.morning_time)
        self.assertTrue(cfg.evening_enabled)
        self.assertIsNone(cfg.evening_time)
        self.assertEqual(cfg.translation_languages, ("en",))
        self.assertEqual(cfg.min_duration_seconds, 15.0)
        self.assertEqual(cfg.max_duration_seconds, 45.0)
        self.assertEqual(cfg.background_clip_strategy, "auto_prefer_single")
        self.assertEqual(cfg.max_background_clips, 3)
        self.assertEqual(cfg.target_background_clips, 1)
        self.assertEqual(cfg.arabic_font_size, 180)
        self.assertEqual(cfg.translation_font_size, 50)
        self.assertEqual(cfg.arabic_translation_gap_px, 80)
        self.assertEqual(cfg.translation_stack_gap_px, 80)
        self.assertTrue(cfg.instagram_enabled)
        self.assertTrue(cfg.facebook_enabled)
        self.assertFalse(cfg.reciter_reuse_confirmed)
        self.assertEqual(cfg.schema_version, 1)

    def test_stored_config_validation_rules(self) -> None:
        # Unsupported schema version
        with self.assertRaises(StorageError):
            StoredConfig(schema_version=99)

        # Unsupported or incorrectly ordered language selections
        with self.assertRaises(StorageError):
            StoredConfig(translation_languages=("de", "en"))
        with self.assertRaises(StorageError):
            StoredConfig(translation_languages=("en", "en"))

        # Invalid duration bounds
        with self.assertRaises(StorageError):
            StoredConfig(min_duration_seconds=5.0)  # below 10.0
        with self.assertRaises(StorageError):
            StoredConfig(max_duration_seconds=65.0)  # above 60.0
        with self.assertRaises(StorageError):
            StoredConfig(min_duration_seconds=25.0, max_duration_seconds=20.0)

        # Invalid background clip strategy
        with self.assertRaises(StorageError):
            StoredConfig(background_clip_strategy="unknown_strat")

        # Invalid target background clips
        with self.assertRaises(StorageError):
            StoredConfig(target_background_clips=0)
        with self.assertRaises(StorageError):
            StoredConfig(target_background_clips=4)

        # Invalid time format
        with self.assertRaises(StorageError):
            StoredConfig(morning_time="25:00")
        with self.assertRaises(StorageError):
            StoredConfig(evening_time="7:30")  # needs 07:30
        with self.assertRaises(StorageError):
            StoredConfig(morning_time="invalid")

    def test_stored_config_roundtrip_serialization(self) -> None:
        cfg = StoredConfig(
            automation_enabled=True,
            timezone="Europe/Berlin",
            morning_enabled=True,
            morning_time="07:30",
            evening_enabled=True,
            evening_time="19:00",
            translation_languages=("en",),
            min_duration_seconds=12.0,
            max_duration_seconds=28.0,
            target_background_clips=2,
            instagram_enabled=True,
            facebook_enabled=False,
            reciter_reuse_confirmed=True,
        )
        data = stored_config_to_dict(cfg)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["morning_time"], "07:30")
        self.assertEqual(data["translation_languages"], ["en"])

        parsed = stored_config_from_dict(data)
        self.assertEqual(parsed.morning_time, cfg.morning_time)
        self.assertEqual(parsed.evening_time, cfg.evening_time)
        self.assertEqual(parsed.translation_languages, cfg.translation_languages)
        self.assertEqual(
            parsed.publishing_schedule,
            (
                PublishingScheduleEntry(id="morning", time="07:30", enabled=True),
                PublishingScheduleEntry(id="evening", time="19:00", enabled=True),
            ),
        )

    def test_dynamic_schedule_validation_and_legacy_migration(self) -> None:
        entries = (
            PublishingScheduleEntry(id="slot-a", time="08:00", enabled=True),
            PublishingScheduleEntry(id="slot-b", time="20:00", enabled=True),
        )
        cfg = StoredConfig(publishing_schedule=entries)
        self.assertEqual(stored_config_from_dict(stored_config_to_dict(cfg)).publishing_schedule, entries)

        migrated = stored_config_from_dict({
            "morning_enabled": True,
            "morning_time": "07:30",
            "evening_enabled": False,
            "evening_time": "19:45",
        })
        self.assertEqual(
            migrated.publishing_schedule,
            (
                PublishingScheduleEntry(id="morning", time="07:30", enabled=True),
                PublishingScheduleEntry(id="evening", time="19:45", enabled=False),
            ),
        )
        self.assertEqual(migrated.morning_time, "07:30")
        self.assertEqual(migrated.evening_time, "19:45")

        for count in (1, 2, 4):
            schedule = tuple(
                PublishingScheduleEntry(id=f"count-{count}-{i}", time=f"{i + 6:02d}:00")
                for i in range(count)
            )
            self.assertEqual(len(StoredConfig(publishing_schedule=schedule).publishing_schedule), count)

        with self.assertRaises(StorageError):
            StoredConfig(publishing_schedule=(
                PublishingScheduleEntry(id="a", time="08:00"),
                PublishingScheduleEntry(id="b", time="08:00"),
            ))
        with self.assertRaises(StorageError):
            StoredConfig(publishing_schedule=tuple(
                PublishingScheduleEntry(id=f"slot-{i}", time=f"{i:02d}:00") for i in range(9)
            ))

    def test_language_and_spacing_presets_are_exact(self) -> None:
        for languages in (("en",), ("de",), ("en", "de")):
            self.assertEqual(StoredConfig(translation_languages=languages).translation_languages, languages)
        for gap in (80, 100, 120):
            self.assertEqual(StoredConfig(arabic_translation_gap_px=gap).arabic_translation_gap_px, gap)
        for gap in (80, 100, 120):
            self.assertEqual(StoredConfig(translation_stack_gap_px=gap).translation_stack_gap_px, gap)
        for size in (50, 40, 30):
            self.assertEqual(StoredConfig(translation_font_size=size).translation_font_size, size)
        with self.assertRaises(StorageError):
            StoredConfig(arabic_translation_gap_px=7)
        with self.assertRaises(StorageError):
            StoredConfig(translation_stack_gap_px=5)
        with self.assertRaises(StorageError):
            StoredConfig(translation_font_size=48)
        with self.assertRaises(StorageError):
            StoredConfig(translation_font_size=60)
        with self.assertRaises(StorageError):
            stored_config_from_dict({"publishing_schedule": []})

    def test_legacy_translation_stack_gaps_are_migrated_semantically(self) -> None:
        for before, expected in ((4, 80), (6, 100), (8, 120), (10, 80), (20, 100), (30, 120),
                                 (80, 80), (100, 100), (120, 120)):
            with self.subTest(before=before):
                config = stored_config_from_dict({"translation_stack_gap_px": before})
                self.assertEqual(config.translation_stack_gap_px, expected)
                saved = stored_config_to_dict(config)
                self.assertEqual(saved["translation_stack_gap_px"], expected)
                self.assertEqual(stored_config_from_dict(saved), config)
        for invalid in (80.5, "80", None, True, 121):
            with self.subTest(invalid=invalid), self.assertRaises(StorageError):
                stored_config_from_dict({"translation_stack_gap_px": invalid})

    def test_legacy_arabic_translation_gaps_are_migrated_semantically(self) -> None:
        v1 = {6: 80, 8: 100, 10: 80, 15: 100, 20: 120, 40: 100, 60: 120}
        for version in ({}, {"arabic_translation_gap_version": 1}):
            for old, new in v1.items():
                config = stored_config_from_dict({**version, "arabic_translation_gap_px": old})
                self.assertEqual(config.arabic_translation_gap_px, new)
        for old, new in ((20, 80), (40, 100), (60, 120)):
            legacy = {"arabic_translation_gap_version": 2, "arabic_translation_gap_px": old}
            config = stored_config_from_dict(legacy)
            self.assertEqual(config.arabic_translation_gap_px, new)
            serialized = stored_config_to_dict(config)
            self.assertEqual(serialized["arabic_translation_gap_version"], 3)
            self.assertEqual(stored_config_from_dict(serialized), config)
        for version in (1, 2, 3):
            self.assertEqual(stored_config_from_dict({"arabic_translation_gap_version": version}).arabic_translation_gap_px, 80)
        for version in (0, 4, "2", True):
            with self.assertRaises(StorageError):
                stored_config_from_dict({"arabic_translation_gap_version": version})
        for version, gap in ((1, 80), (2, 80), (2, 10), (3, 60), (3, 121)):
            with self.assertRaises(StorageError):
                stored_config_from_dict({"arabic_translation_gap_version": version, "arabic_translation_gap_px": gap})
        for invalid in (80.9, "80", None, True):
            with self.subTest(invalid=invalid), self.assertRaises(StorageError):
                stored_config_from_dict({"arabic_translation_gap_version": 3, "arabic_translation_gap_px": invalid})

    def test_legacy_translation_font_sizes_are_migrated_semantically(self) -> None:
        self.assertEqual(stored_config_from_dict({"translation_font_size": 60}).translation_font_size, 50)
        self.assertEqual(stored_config_from_dict({"translation_font_size": 55}).translation_font_size, 40)
        self.assertEqual(stored_config_from_dict({"translation_font_size": 50}).translation_font_size, 50)
        self.assertEqual(
            stored_config_from_dict({"translation_font_size": 50, "arabic_translation_gap_px": 6}).translation_font_size,
            30,
        )
        self.assertEqual(stored_config_from_dict({"translation_font_size": 40}).translation_font_size, 40)
        self.assertEqual(stored_config_from_dict({"translation_font_size": 30}).translation_font_size, 30)


class TestKeyBuildingAndSecurity(unittest.TestCase):
    """Test deterministic S3 key paths and path traversal guards."""

    def test_build_config_key(self) -> None:
        self.assertEqual(build_config_key("quran-reels/"), "quran-reels/config/current.json")
        self.assertEqual(build_config_key("custom"), "custom/config/current.json")

    def test_build_run_key_production(self) -> None:
        morning_key = build_run_key("2026-08-23", "morning", prefix="quran-reels/")
        evening_key = build_run_key("2026-08-23", "evening", prefix="quran-reels/")
        self.assertEqual(morning_key, "quran-reels/runs/2026-08-23/morning.json")
        self.assertEqual(evening_key, "quran-reels/runs/2026-08-23/evening.json")
        self.assertNotEqual(morning_key, evening_key)

    def test_build_run_key_test(self) -> None:
        test_key = build_run_key("2026-08-23", "test", run_id="run_abc123", prefix="quran-reels/")
        self.assertEqual(test_key, "quran-reels/runs/test/run_abc123.json")

    def test_build_dynamic_scheduled_and_manual_keys(self) -> None:
        self.assertEqual(
            build_run_key("2026-08-23", "slot-123", prefix="quran-reels/", run_kind="scheduled"),
            "quran-reels/runs/2026-08-23/scheduled/slot-123.json",
        )
        self.assertEqual(
            build_run_key(
                "2026-08-23", "manual-production", run_id="manual-abc", prefix="quran-reels/", run_kind="manual_production"
            ),
            "quran-reels/runs/manual-production/manual-abc.json",
        )
        self.assertEqual(
            build_media_key(
                "2026-08-23", "manual-production", "manual-abc", prefix="quran-reels/", run_kind="manual_production"
            ),
            "quran-reels/media/manual-production/manual-abc.mp4",
        )
        self.assertEqual(
            build_scheduler_claim_key("2026-08-23", "slot-123", "quran-reels/"),
            "quran-reels/scheduler/claims/2026-08-23/slot-123.json",
        )

    def test_build_run_key_traversal_rejection(self) -> None:
        with self.assertRaises(StorageError):
            build_run_key("2026-08-23/../evil", "morning")
        with self.assertRaises(StorageError):
            build_run_key("2026-08-23", "../slot")
        with self.assertRaises(StorageError):
            build_run_key("2026-08-23", "test", run_id="../../run_id")

    def test_build_media_key_production(self) -> None:
        media_key = build_media_key("2026-08-23", "morning", "run_123", prefix="quran-reels/")
        self.assertEqual(media_key, "quran-reels/media/2026-08-23/morning/run_123.mp4")

    def test_build_media_key_test(self) -> None:
        test_media = build_media_key("2026-08-23", "test", "run_test_456", prefix="quran-reels/")
        self.assertEqual(test_media, "quran-reels/media/test/run_test_456.mp4")

    def test_build_media_key_traversal_rejection(self) -> None:
        with self.assertRaises(StorageError):
            build_media_key("2026-08-23", "morning", "../evil_id")


class TestRunRecordModel(unittest.TestCase):
    """Test RunRecord validation, serialization, and secret stripping."""

    def test_run_record_valid_creation(self) -> None:
        rec = RunRecord(
            run_id="run_20260823_m",
            date="2026-08-23",
            slot="morning",
            status="SUCCESS",
            started_at="2026-08-23T06:00:00Z",
            completed_at="2026-08-23T06:01:30Z",
            surah_number=112,
            first_ayah=1,
            last_ayah=4,
            duration_seconds=18.5,
            pexels_video_ids=(101, 102, 103),
            media_key="quran-reels/media/2026-08-23/morning/run_20260823_m.mp4",
            final_safety_passed=True,
            instagram_status="NOT_ATTEMPTED",
            facebook_status="NOT_ATTEMPTED",
        )
        self.assertEqual(rec.surah_number, 112)
        self.assertEqual(rec.status, "SUCCESS")
        self.assertEqual(rec.pexels_video_ids, (101, 102, 103))

    def test_run_record_validation_errors(self) -> None:
        with self.assertRaises(StorageError):
            RunRecord(run_id="", date="2026-08-23", slot="morning", status="SUCCESS", started_at="now")
        with self.assertRaises(StorageError):
            RunRecord(run_id="r1", date="invalid-date", slot="morning", status="SUCCESS", started_at="now")
        with self.assertRaises(StorageError):
            RunRecord(run_id="r1", date="2026-08-23", slot="afternoon", status="SUCCESS", started_at="now")
        with self.assertRaises(StorageError):
            RunRecord(run_id="r1", date="2026-08-23", slot="morning", status="UNKNOWN", started_at="now")
        with self.assertRaises(StorageError):
            RunRecord(run_id="r1", date="2026-08-23", slot="morning", status="SUCCESS", started_at="now", instagram_status="PENDING")

    def test_run_record_roundtrip_serialization(self) -> None:
        rec = RunRecord(
            run_id="run_20260823_e",
            date="2026-08-23",
            slot="evening",
            status="FAILED",
            started_at="2026-08-23T18:00:00Z",
            completed_at="2026-08-23T18:00:45Z",
            failure_reason="Unable to collect safe background clips",
            stage="STAGE_SCANNING_BACKGROUNDS",
        )
        data = run_record_to_dict(rec)
        self.assertEqual(data["status"], "FAILED")
        self.assertEqual(data["failure_reason"], "Unable to collect safe background clips")

        parsed = run_record_from_dict(data)
        self.assertEqual(parsed, rec)

    def test_run_record_with_platform_failure_reasons_roundtrip(self) -> None:
        rec = RunRecord(
            run_id="run_20260823_m_warn",
            date="2026-08-23",
            slot="morning",
            status="SUCCESS",
            started_at="2026-08-23T06:00:00Z",
            facebook_status="SUCCESS",
            instagram_status="NOT_CONFIGURED",
            facebook_media_id="fb_12345",
            facebook_failure_reason=None,
            instagram_failure_reason="Instagram Professional Account is not configured (META_IG_USER_ID is not set).",
        )
        data = run_record_to_dict(rec)
        self.assertEqual(data["instagram_status"], "NOT_CONFIGURED")
        self.assertEqual(
            data["instagram_failure_reason"],
            "Instagram Professional Account is not configured (META_IG_USER_ID is not set).",
        )
        parsed = run_record_from_dict(data)
        self.assertEqual(parsed, rec)

    def test_run_record_with_buffer_acceptance_roundtrip(self) -> None:
        rec = RunRecord(
            run_id="run_20260903_buffer_accepted",
            date="2026-09-03",
            slot="manual-production",
            status="SUCCESS",
            started_at="2026-09-03T12:00:00Z",
            facebook_status="ACCEPTED",
            publisher_backend="buffer",
            buffer_post_id="buffer_post_123",
            buffer_post_status="sending",
            buffer_accepted_at="2026-09-03T12:00:05Z",
        )
        parsed = run_record_from_dict(run_record_to_dict(rec))
        self.assertEqual(parsed.facebook_status, "ACCEPTED")
        self.assertEqual(parsed.buffer_post_status, "sending")
        self.assertEqual(parsed.buffer_accepted_at, "2026-09-03T12:00:05Z")

    def test_no_secrets_in_serialized_run_record(self) -> None:
        rec = RunRecord(
            run_id="run_1",
            date="2026-08-23",
            slot="morning",
            status="SUCCESS",
            started_at="2026-08-23T06:00:00Z",
        )
        data = run_record_to_dict(rec)
        json_str = json.dumps(data)
        self.assertNotIn("secret", json_str.lower())
        self.assertNotIn("access_key", json_str.lower())
        self.assertNotIn("presigned", json_str.lower())


class TestStorageOperationsWithMockClient(unittest.TestCase):
    """Test all storage functions using mocked S3/B2 client."""

    def setUp(self) -> None:
        self.b2_config = B2Config(
            endpoint_url="https://s3.eu-central-003.backblazeb2.com",
            region_name="eu-central-003",
            application_key_id="key123",
            application_key="sec123",
            bucket_name="test-bucket",
            prefix="quran-reels/",
        )
        self.mock_client = MagicMock()

    def test_load_config_not_found(self) -> None:
        self.mock_client.get_object.side_effect = make_mock_client_error("NoSuchKey")
        cfg = load_config(self.mock_client, self.b2_config)
        self.assertIsNone(cfg)

    def test_load_config_success(self) -> None:
        stored = StoredConfig(morning_time="07:00", evening_time="19:30")
        json_data = json.dumps(stored_config_to_dict(stored)).encode("utf-8")
        self.mock_client.get_object.return_value = {"Body": io.BytesIO(json_data)}

        cfg = load_config(self.mock_client, self.b2_config)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.morning_time, "07:00")
        self.assertEqual(cfg.evening_time, "19:30")
        self.mock_client.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="quran-reels/config/current.json",
        )

    def test_save_config_success(self) -> None:
        cfg = StoredConfig(morning_time="08:00")
        key = save_config(self.mock_client, self.b2_config, cfg)
        self.assertEqual(key, "quran-reels/config/current.json")
        self.mock_client.put_object.assert_called_once()
        call_kwargs = self.mock_client.put_object.call_args[1]
        self.assertEqual(call_kwargs["Bucket"], "test-bucket")
        self.assertEqual(call_kwargs["Key"], "quran-reels/config/current.json")
        self.assertEqual(call_kwargs["ContentType"], "application/json")
        saved_dict = json.loads(call_kwargs["Body"].decode("utf-8"))
        self.assertEqual(saved_dict["morning_time"], "08:00")

    def test_put_and_get_run_record(self) -> None:
        rec_m = RunRecord(
            run_id="run_m",
            date="2026-08-23",
            slot="morning",
            status="SUCCESS",
            started_at="2026-08-23T06:00:00Z",
            surah_number=112,
            first_ayah=1,
            last_ayah=4,
        )
        key_m = put_run_record(self.mock_client, self.b2_config, rec_m)
        self.assertEqual(key_m, "quran-reels/runs/2026-08-23/morning.json")

        json_data = json.dumps(run_record_to_dict(rec_m)).encode("utf-8")
        self.mock_client.get_object.return_value = {"Body": io.BytesIO(json_data)}

        retrieved = get_run_record(self.mock_client, self.b2_config, "2026-08-23", "morning")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.run_id, "run_m")
        self.assertEqual(retrieved.surah_number, 112)

    def test_get_run_record_not_found(self) -> None:
        self.mock_client.get_object.side_effect = make_mock_client_error("NoSuchKey")
        retrieved = get_run_record(self.mock_client, self.b2_config, "2026-08-23", "evening")
        self.assertIsNone(retrieved)

    def test_list_recent_runs_and_sort_order(self) -> None:
        mock_paginator = MagicMock()
        self.mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "quran-reels/runs/2026-08-21/morning.json"},
                    {"Key": "quran-reels/runs/2026-08-22/evening.json"},
                    {"Key": "quran-reels/runs/2026-08-22/morning.json"},
                    {"Key": "quran-reels/runs/2026-08-23/morning.json"},
                ]
            }
        ]

        def mock_get_obj(Bucket: str, Key: str) -> dict:
            slot = "morning" if "morning" in Key else "evening"
            date = Key.split("/")[2]
            rec = RunRecord(
                run_id=f"id_{date}_{slot}",
                date=date,
                slot=slot,
                status="SUCCESS",
                started_at=f"{date}T06:00:00Z",
            )
            return {"Body": io.BytesIO(json.dumps(run_record_to_dict(rec)).encode("utf-8"))}

        self.mock_client.get_object.side_effect = mock_get_obj

        runs = list_recent_runs(self.mock_client, self.b2_config, limit=10)
        self.assertEqual(len(runs), 4)
        # Verify descending order (latest first)
        self.assertEqual(runs[0].date, "2026-08-23")
        self.assertEqual(runs[0].slot, "morning")
        self.assertEqual(runs[1].date, "2026-08-22")
        self.assertEqual(runs[1].slot, "morning")
        self.assertEqual(runs[2].date, "2026-08-22")
        self.assertEqual(runs[2].slot, "evening")
        self.assertEqual(runs[3].date, "2026-08-21")

    def test_get_recent_content_history(self) -> None:
        mock_paginator = MagicMock()
        self.mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "quran-reels/runs/2026-08-23/morning.json"},
                    {"Key": "quran-reels/runs/2026-08-22/evening.json"},
                    {"Key": "quran-reels/runs/2026-08-22/morning.json"},
                ]
            }
        ]

        records_map = {
            "quran-reels/runs/2026-08-23/morning.json": RunRecord(
                run_id="r1",
                date="2026-08-23",
                slot="morning",
                status="SUCCESS",
                started_at="2026-08-23T06:00:00Z",
                surah_number=112,
                first_ayah=1,
                last_ayah=4,
                pexels_video_ids=(101, 102),
            ),
            "quran-reels/runs/2026-08-22/evening.json": RunRecord(
                run_id="r2",
                date="2026-08-22",
                slot="evening",
                status="FAILED",  # Failed run: should be ignored!
                started_at="2026-08-22T18:00:00Z",
                surah_number=113,
                first_ayah=1,
                last_ayah=3,
                pexels_video_ids=(999,),
            ),
            "quran-reels/runs/2026-08-22/morning.json": RunRecord(
                run_id="r3",
                date="2026-08-22",
                slot="morning",
                status="SUCCESS",
                started_at="2026-08-22T06:00:00Z",
                surah_number=114,
                first_ayah=1,
                last_ayah=6,
                pexels_video_ids=(102, 103),  # 102 duplicate: should de-duplicate!
            ),
        }

        def mock_get_obj(Bucket: str, Key: str) -> dict:
            rec = records_map[Key]
            return {"Body": io.BytesIO(json.dumps(run_record_to_dict(rec)).encode("utf-8"))}

        self.mock_client.get_object.side_effect = mock_get_obj

        history = get_recent_content_history(self.mock_client, self.b2_config, last_n_runs=10)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["surah_number"], 112)
        self.assertEqual(history[1]["surah_number"], 114)


class TestMediaAndPresignedOperations(unittest.TestCase):
    """Test Reel media uploading, deletion, and presigned URLs."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_storage_media_")
        self.b2_config = B2Config(
            endpoint_url="https://s3.eu-central-003.backblazeb2.com",
            region_name="eu-central-003",
            application_key_id="key123",
            application_key="sec123",
            bucket_name="media-bucket",
            prefix="quran-reels/",
        )
        self.mock_client = MagicMock()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upload_reel_success(self) -> None:
        mp4_path = os.path.join(self.temp_dir, "test_reel.mp4")
        with open(mp4_path, "wb") as f:
            f.write(b"mock-mp4-video-data")

        media_key = upload_reel(
            self.mock_client,
            self.b2_config,
            local_path=mp4_path,
            date="2026-08-23",
            slot="morning",
            run_id="run_999",
        )
        self.assertEqual(media_key, "quran-reels/media/2026-08-23/morning/run_999.mp4")
        self.mock_client.upload_fileobj.assert_called_once()
        call_args = self.mock_client.upload_fileobj.call_args
        self.assertEqual(call_args[0][1], "media-bucket")
        self.assertEqual(call_args[0][2], media_key)
        self.assertEqual(call_args[1]["ExtraArgs"], {"ContentType": "video/mp4"})

    def test_upload_reel_empty_or_missing_rejections(self) -> None:
        # Missing file
        with self.assertRaises(StorageError):
            upload_reel(self.mock_client, self.b2_config, "nonexistent.mp4", "2026-08-23", "morning", "r1")

        # 0-byte file
        empty_path = os.path.join(self.temp_dir, "empty.mp4")
        with open(empty_path, "wb") as f:
            pass
        with self.assertRaises(StorageError):
            upload_reel(self.mock_client, self.b2_config, empty_path, "2026-08-23", "morning", "r1")

    def test_delete_object(self) -> None:
        delete_object(self.mock_client, self.b2_config, "quran-reels/media/2026-08-23/morning/r1.mp4")
        self.mock_client.delete_object.assert_called_once_with(
            Bucket="media-bucket",
            Key="quran-reels/media/2026-08-23/morning/r1.mp4",
        )

    def test_object_exists(self) -> None:
        self.mock_client.head_object.return_value = {"ContentLength": 12345}
        self.assertTrue(object_exists(self.mock_client, self.b2_config, "some/key.mp4"))

        self.mock_client.head_object.side_effect = make_mock_client_error("404")
        self.assertFalse(object_exists(self.mock_client, self.b2_config, "missing/key.mp4"))

    def test_create_presigned_get_url_default_6_hours(self) -> None:
        self.mock_client.generate_presigned_url.return_value = "https://s3.eu-central-003.backblazeb2.com/media.mp4?sig=abc"
        url = create_presigned_get_url(self.mock_client, self.b2_config, "quran-reels/media/r1.mp4")
        self.assertEqual(url, "https://s3.eu-central-003.backblazeb2.com/media.mp4?sig=abc")
        self.mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "media-bucket", "Key": "quran-reels/media/r1.mp4"},
            ExpiresIn=DEFAULT_PRESIGNED_EXPIRY_SECONDS,  # 21600 seconds = 6 hours
        )

    def test_create_presigned_get_url_bounds_validation(self) -> None:
        # <= 0 seconds rejected
        with self.assertRaises(StorageError):
            create_presigned_get_url(self.mock_client, self.b2_config, "key.mp4", expires_seconds=0)

        # > 7 days (604800s) rejected
        with self.assertRaises(StorageError):
            create_presigned_get_url(self.mock_client, self.b2_config, "key.mp4", expires_seconds=604801)

        # Valid custom expiry (1 hour = 3600s)
        self.mock_client.generate_presigned_url.return_value = "https://signed.url"
        url = create_presigned_get_url(self.mock_client, self.b2_config, "key.mp4", expires_seconds=3600)
        self.assertEqual(url, "https://signed.url")


if __name__ == "__main__":
    unittest.main()
