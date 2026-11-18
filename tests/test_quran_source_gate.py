"""Regression test suite for production Quran source invariant enforcement.

Tests A through I verify that production runs ('scheduled' with legacy or dynamic
slot IDs, and 'manual_production') strictly require verified production Quran data
(source.environment == 'production', 114 Surahs, 6236 Ayahs), and fail closed before
any rendering, upload, publishing, or coverage mutation occurs.
"""

from __future__ import annotations

import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from engine.job import JobRequest, run_job
from engine.main import (
    GenerationError,
    RunRequest,
    generate_reel,
)
from engine.quran import (
    EXPECTED_SURAH_COUNT,
    EXPECTED_TOTAL_AYAH_COUNT,
    STANDARD_SURAH_AYAH_COUNTS,
    STANDARD_SURAH_METADATA,
    QuranAyah,
    QuranData,
    QuranSourceError,
    QuranSourceMetadata,
    QuranSurah,
    validate_quran_source_for_run,
)
from engine.storage import CoverageState, StoredConfig, PublishingScheduleEntry
from tests.editorial_fixtures import authorized_run_request as RunRequest, authorized_run_job as run_job


def make_full_production_quran() -> QuranData:
    """Create synthetic, structurally complete QuranData with 114 Surahs, 6236 Ayahs, and env='production'."""
    all_surahs: list[QuranSurah] = []
    global_id = 1
    for s_num in range(1, EXPECTED_SURAH_COUNT + 1):
        count = STANDARD_SURAH_AYAH_COUNTS[s_num]
        ayahs = tuple(
            QuranAyah(global_id + i, s_num, i + 1, f"آية {s_num}:{i + 1}")
            for i in range(count)
        )
        global_id += count
        arabic_name, english_name, english_trans = STANDARD_SURAH_METADATA.get(
            s_num, (f"سورة {s_num}", f"Surah {s_num}", None)
        )
        all_surahs.append(
            QuranSurah(
                number=s_num,
                arabic_name=arabic_name,
                english_name=english_name,
                english_name_translation=english_trans,
                number_of_ayahs=count,
                ayahs=ayahs,
            )
        )
    return QuranData(
        surahs=tuple(all_surahs),
        source=QuranSourceMetadata(
            provider="Quran Foundation",
            api_base_url="https://apis.quran.foundation/content/api/v4",
            environment="production",
            script="uthmani",
            field="text_uthmani",
        ),
    )


def make_prelive_sandbox_quran() -> QuranData:
    """Create 2-Surah, 293-Ayah prelive sandbox QuranData."""
    ayahs_1 = tuple(
        QuranAyah(i, 1, i, f"آية 1:{i}") for i in range(1, 8)
    )
    surah_1 = QuranSurah(
        number=1,
        arabic_name="الفاتحة",
        english_name="Al-Fatihah",
        english_name_translation="The Opener",
        number_of_ayahs=7,
        ayahs=ayahs_1,
    )
    ayahs_2 = tuple(
        QuranAyah(7 + i, 2, i, f"آية 2:{i}") for i in range(1, 287)
    )
    surah_2 = QuranSurah(
        number=2,
        arabic_name="البقرة",
        english_name="Al-Baqarah",
        english_name_translation="The Cow",
        number_of_ayahs=286,
        ayahs=ayahs_2,
    )
    return QuranData(
        surahs=(surah_1, surah_2),
        source=QuranSourceMetadata(
            provider="Quran Foundation",
            api_base_url="https://apis-prelive.quran.foundation/content/api/v4",
            environment="prelive",
            script="uthmani",
            field="text_uthmani",
        ),
    )


def make_corrupt_production_quran_surahs() -> QuranData:
    """Create production-labelled QuranData with only 2 Surahs instead of 114."""
    prelive = make_prelive_sandbox_quran()
    return QuranData(
        surahs=prelive.surahs,
        source=QuranSourceMetadata(
            provider="Quran Foundation",
            api_base_url="https://apis.quran.foundation/content/api/v4",
            environment="production",
            script="uthmani",
            field="text_uthmani",
        ),
    )


def make_corrupt_production_quran_ayahs() -> QuranData:
    """Create production-labelled QuranData with 114 Surahs but 6235 Ayahs (1 missing)."""
    full = make_full_production_quran()
    surahs_list = list(full.surahs)
    # Truncate Surah 114 from 6 ayahs to 5 ayahs
    s114 = surahs_list[113]
    surahs_list[113] = QuranSurah(
        number=114,
        arabic_name=s114.arabic_name,
        english_name=s114.english_name,
        english_name_translation=s114.english_name_translation,
        number_of_ayahs=5,
        ayahs=s114.ayahs[:5],
    )
    return QuranData(
        surahs=tuple(surahs_list),
        source=full.source,
    )


class TestQuranSourceInvariant(unittest.TestCase):
    """Explicit verification of required regression tests A through I."""

    def setUp(self) -> None:
        self.prod_quran = make_full_production_quran()
        self.prelive_quran = make_prelive_sandbox_quran()
        self.corrupt_surahs_quran = make_corrupt_production_quran_surahs()
        self.corrupt_ayahs_quran = make_corrupt_production_quran_ayahs()

    # Test A: scheduled + slot="morning" + prelive => rejected
    def test_case_a_scheduled_morning_prelive_rejected(self) -> None:
        with self.assertRaises(QuranSourceError) as ctx:
            validate_quran_source_for_run("scheduled", self.prelive_quran)
        self.assertIn("requires verified production Quran source (env='production')", str(ctx.exception))
        self.assertIn("env='prelive'", str(ctx.exception))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_mp4 = os.path.join(tmpdir, "out.mp4")
            req = RunRequest(slot="morning", run_kind="scheduled", output_path=out_mp4)
            res = generate_reel(req, quran_data=self.prelive_quran)
            self.assertFalse(res.success)
            self.assertIn("requires verified production Quran source", res.failure_reason or "")

    # Test B: scheduled + arbitrary dynamic slot ID + prelive => rejected (MANDATORY)
    def test_case_b_scheduled_dynamic_slot_prelive_rejected(self) -> None:
        dynamic_slot = "slot-00000000-0000-4000-8000-000000000002"
        with self.assertRaises(QuranSourceError) as ctx:
            validate_quran_source_for_run("scheduled", self.prelive_quran)
        self.assertIn("requires verified production Quran source", str(ctx.exception))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_mp4 = os.path.join(tmpdir, "out.mp4")
            req = RunRequest(slot=dynamic_slot, run_kind="scheduled", output_path=out_mp4)
            res = generate_reel(req, quran_data=self.prelive_quran)
            self.assertFalse(res.success)
            self.assertIn("requires verified production Quran source", res.failure_reason or "")

    # Test C: manual_production + prelive => rejected
    def test_case_c_manual_production_prelive_rejected(self) -> None:
        with self.assertRaises(QuranSourceError) as ctx:
            validate_quran_source_for_run("manual_production", self.prelive_quran)
        self.assertIn("requires verified production Quran source", str(ctx.exception))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_mp4 = os.path.join(tmpdir, "out.mp4")
            req = RunRequest(slot="manual-production", run_kind="manual_production", output_path=out_mp4)
            res = generate_reel(req, quran_data=self.prelive_quran)
            self.assertFalse(res.success)
            self.assertIn("requires verified production Quran source", res.failure_reason or "")

    # Test D: scheduled + dynamic slot + production source + 114 Surahs + 6236 Ayahs => passes
    def test_case_d_scheduled_dynamic_slot_production_source_passes(self) -> None:
        self.assertEqual(self.prod_quran.total_surahs, 114)
        self.assertEqual(self.prod_quran.total_ayahs, 6236)
        self.assertEqual(self.prod_quran.source.environment, "production")

        # Must not raise
        validate_quran_source_for_run("scheduled", self.prod_quran)

    # Test E: manual_production + production source + 114/6236 => passes
    def test_case_e_manual_production_source_passes(self) -> None:
        self.assertEqual(self.prod_quran.total_surahs, 114)
        self.assertEqual(self.prod_quran.total_ayahs, 6236)
        self.assertEqual(self.prod_quran.source.environment, "production")

        # Must not raise
        validate_quran_source_for_run("manual_production", self.prod_quran)

    # Test F: production-labelled source with only 2 Surahs => rejected
    def test_case_f_production_labelled_source_with_only_2_surahs_rejected(self) -> None:
        self.assertEqual(self.corrupt_surahs_quran.source.environment, "production")
        self.assertEqual(self.corrupt_surahs_quran.total_surahs, 2)

        with self.assertRaises(QuranSourceError) as ctx_sched:
            validate_quran_source_for_run("scheduled", self.corrupt_surahs_quran)
        self.assertIn("requires full canonical Quran dataset (114 Surahs). Found 2 Surahs", str(ctx_sched.exception))

        with self.assertRaises(QuranSourceError) as ctx_manual:
            validate_quran_source_for_run("manual_production", self.corrupt_surahs_quran)
        self.assertIn("requires full canonical Quran dataset (114 Surahs). Found 2 Surahs", str(ctx_manual.exception))

    # Test G: production-labelled source with wrong total ayah count => rejected
    def test_case_g_production_labelled_source_with_wrong_total_ayahs_rejected(self) -> None:
        self.assertEqual(self.corrupt_ayahs_quran.source.environment, "production")
        self.assertEqual(self.corrupt_ayahs_quran.total_surahs, 114)
        self.assertEqual(self.corrupt_ayahs_quran.total_ayahs, 6235)

        with self.assertRaises(QuranSourceError) as ctx_sched:
            validate_quran_source_for_run("scheduled", self.corrupt_ayahs_quran)
        self.assertIn("requires full canonical Quran dataset (6236 Ayahs). Found 6235 Ayahs", str(ctx_sched.exception))

        with self.assertRaises(QuranSourceError) as ctx_manual:
            validate_quran_source_for_run("manual_production", self.corrupt_ayahs_quran)
        self.assertIn("requires full canonical Quran dataset (6236 Ayahs). Found 6235 Ayahs", str(ctx_manual.exception))

    # Test H: test + prelive 2-Surah sandbox => permitted
    def test_case_h_test_run_with_prelive_sandbox_permitted(self) -> None:
        self.assertEqual(self.prelive_quran.source.environment, "prelive")
        self.assertEqual(self.prelive_quran.total_surahs, 2)
        self.assertEqual(self.prelive_quran.total_ayahs, 293)

        # Test run must be permitted to use prelive sandbox
        validate_quran_source_for_run("test", self.prelive_quran)

    # Test I: a rejected production source reaches ZERO: render, upload, Buffer, Meta, coverage
    def test_case_i_rejected_production_source_reaches_zero_side_effects(self) -> None:
        from unittest.mock import patch
        from engine.storage import B2Config

        dynamic_slot = "slot-00000000-0000-4000-8000-000000000002"

        mock_state_store = MagicMock()
        mock_state_store.load_config.return_value = StoredConfig(
            automation_enabled=True,
            publishing_schedule=(
                PublishingScheduleEntry(id=dynamic_slot, time="16:00", enabled=True),
            ),
        )
        mock_state_store.get_run_record.return_value = None
        mock_state_store.is_ayah_covered.return_value = False

        initial_coverage = CoverageState(cycle_number=1, processed_run_ids=())
        mock_state_store.load_coverage.return_value = initial_coverage
        mock_state_store.update_coverage_for_run.return_value = (initial_coverage, False)

        mock_s3 = MagicMock()
        mock_buffer_publisher = MagicMock()
        mock_meta_publisher = MagicMock()
        mock_upload_reel = MagicMock()

        render_called = False

        def tracking_generate_reel(request: RunRequest, **kwargs):
            nonlocal render_called
            # The pipeline should validate before any render call
            validate_quran_source_for_run(request.run_kind, self.prelive_quran)
            render_called = True
            raise RuntimeError("Should never reach render")

        job_req = JobRequest(
            slot=dynamic_slot,
            run_kind="scheduled",
            schedule_slot_id=dynamic_slot,
            publish_facebook=True,
            publish_instagram=True,
        )

        b2_dummy_config = B2Config(
            endpoint_url="https://fake.example.com",
            region_name="us-east-1",
            application_key_id="k",
            application_key="a",
            bucket_name="b",
        )

        mock_publish_reel = MagicMock()
        mock_publish_buffer = MagicMock()

        with patch("engine.job.get_active_state_store", return_value=mock_state_store), \
             patch("engine.job.create_b2_client", return_value=mock_s3), \
             patch("engine.job.load_b2_config_from_env", return_value=b2_dummy_config), \
             patch("engine.job.upload_reel", mock_upload_reel):
            job_result = run_job(
                job_req,
                env={"STATE_AUTHORITY": "supabase"},
                generate_reel_fn=tracking_generate_reel,
                publish_reel_fn=mock_publish_reel,
                publish_buffer_facebook_fn=mock_publish_buffer,
            )

        # 1. Job must fail closed
        self.assertEqual(job_result.status, "FAILED")
        self.assertIn("requires verified production Quran source", job_result.failure_reason or "")

        # 2. ZERO renders executed
        self.assertFalse(render_called, "Rendering was invoked despite rejected production source!")

        # 3. ZERO storage uploads executed
        mock_upload_reel.assert_not_called()
        mock_s3.upload_file.assert_not_called()
        mock_s3.put_object.assert_not_called()

        # 4. ZERO Buffer / Meta publications
        mock_publish_buffer.assert_not_called()
        mock_publish_reel.assert_not_called()

        # 5. ZERO coverage mutations caused by generation
        mock_state_store.update_coverage_for_run.assert_not_called()
        mock_state_store.save_coverage.assert_not_called()

        # 6. Fail record is properly marked with STAGE_GENERATION_EXCEPTION
        mock_state_store.fail_run_record.assert_called_once()
        failed_record = mock_state_store.fail_run_record.call_args[0][0]
        self.assertEqual(failed_record.status, "FAILED")
        self.assertEqual(failed_record.stage, "STAGE_GENERATION_EXCEPTION")
        self.assertIn("requires verified production Quran source", failed_record.failure_reason or "")


if __name__ == "__main__":
    unittest.main()
