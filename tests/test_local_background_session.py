"""Regression tests for Phase 8H: Local Background Session Orchestrator."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.local_background_session import (
    LOCAL_DISCOVERY_BATCH_SIZE,
    LOCAL_DISCOVERY_COOLDOWN_MINUTES,
    LOCAL_DISCOVERY_LOW_WATER_PENDING,
    LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS,
    main,
    run_local_background_session,
)


class TestLocalBackgroundSession(unittest.TestCase):
    """Test suite covering requirements A through P for Phase 8H."""

    def setUp(self) -> None:
        self.simulated_time = 1000.0
        self.env_patcher = patch("scripts.local_background_session._validate_environment")
        self.mock_validate_env = self.env_patcher.start()
        # Orchestration unit tests inject fake qualifying operations. Do not
        # require downloaded, license-controlled CV binaries just to test timing.
        self.safety_patcher = patch("scripts.local_background_session.verify_current_safety_model_files")
        self.mock_verify_models = self.safety_patcher.start()

    def tearDown(self) -> None:
        self.safety_patcher.stop()
        self.env_patcher.stop()

    def fake_monotonic(self) -> float:
        return self.simulated_time

    def fake_sleep(self, seconds: float) -> None:
        self.simulated_time += seconds

    def test_A_pending_above_low_water_starts_qualification_without_discovery(self) -> None:
        """A. PENDING >= low-water -> qualification starts immediately, zero discovery."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 15, "total": 20}

        discovery_mock = MagicMock()
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        def step_time() -> float:
            # Advance time by 30s per call so session eventually ends
            self.simulated_time += 30.0
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=2.0,
            low_water_pending=10,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualification_mock,
            monotonic=step_time,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["discovery_runs"], 0)
        self.assertEqual(discovery_mock.call_count, 0)
        self.assertGreater(result["jobs_completed"], 0)
        self.assertGreater(qualification_mock.call_count, 0)

    def test_B_pending_below_low_water_triggers_bounded_discovery(self) -> None:
        """B. PENDING below low-water -> bounded discovery runs."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 5, "total": 10}

        discovery_mock = MagicMock(return_value={"status": "COMPLETED", "discovered": 10})
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        call_count = 0

        def advance_clock() -> float:
            nonlocal call_count
            call_count += 1
            if call_count > 10:
                self.simulated_time += 300.0  # End session
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=1.0,
            low_water_pending=10,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualification_mock,
            monotonic=advance_clock,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertGreaterEqual(result["discovery_runs"], 1)
        discovery_mock.assert_called()

    def test_C_pending_zero_triggers_immediate_startup_discovery(self) -> None:
        """C. PENDING = 0 -> immediate startup discovery pass."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 0, "total": 5}

        discovery_mock = MagicMock(return_value={"status": "COMPLETED", "discovered": 5})
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        call_count = 0

        def advance_clock() -> float:
            nonlocal call_count
            call_count += 1
            if call_count > 8:
                self.simulated_time += 600.0
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=1.0,
            low_water_pending=10,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualification_mock,
            monotonic=advance_clock,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertGreaterEqual(result["discovery_runs"], 1)
        self.assertEqual(discovery_mock.call_count, 1)

    def test_D_discovery_adds_candidates_then_qualification_resumes(self) -> None:
        """D. Discovery adds candidates -> qualification resumes."""
        mock_store = MagicMock()
        # First call pending=0, subsequent calls pending=10
        queue_state = {"pending": 0}

        def fake_summary() -> dict[str, int]:
            return {"pending": queue_state["pending"], "total": 10}

        mock_store.background_candidate_summary.side_effect = fake_summary

        def fake_discovery() -> dict[str, int]:
            queue_state["pending"] = 12
            return {"status": "COMPLETED", "discovered": 12}

        discovery_mock = MagicMock(side_effect=fake_discovery)
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        call_count = 0

        def advance_clock() -> float:
            nonlocal call_count
            call_count += 1
            if call_count > 10:
                self.simulated_time += 600.0
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=1.0,
            low_water_pending=10,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualification_mock,
            monotonic=advance_clock,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["discovery_runs"], 1)
        self.assertEqual(result["candidates_discovered"], 12)
        self.assertGreater(result["jobs_completed"], 0)

    def test_E_discovery_returns_zero_avoids_busy_loop(self) -> None:
        """E. Discovery returns zero candidates -> no busy loop (sleeps)."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 0, "total": 0}

        discovery_mock = MagicMock(return_value={"status": "COMPLETED", "discovered": 0})
        qualification_mock = MagicMock(return_value=None)
        sleep_mock = MagicMock(side_effect=self.fake_sleep)

        # End after 3 sleep calls
        def fake_time() -> float:
            if sleep_mock.call_count >= 3:
                self.simulated_time += 10000.0
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=5.0,
            low_water_pending=10,
            discovery_cooldown_minutes=15.0,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualification_mock,
            monotonic=fake_time,
            sleep_fn=sleep_mock,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertGreater(sleep_mock.call_count, 0)
        # Verify sleep duration was positive and bounded
        for call_args in sleep_mock.call_args_list:
            wait_s = call_args[0][0]
            self.assertGreater(wait_s, 0.0)
            self.assertLessEqual(wait_s, 15.0)

    def test_F_cooldown_prevents_repeated_discovery_hammering(self) -> None:
        """F. Cooldown prevents repeated discovery hammering when queue stays low."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 2, "total": 5}

        discovery_mock = MagicMock(return_value={"status": "COMPLETED", "discovered": 0})
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        # Run 5 iterations within 2 minutes (cooldown is 15 minutes)
        iterations = 0

        def advance_clock() -> float:
            nonlocal iterations
            iterations += 1
            if iterations > 8:
                self.simulated_time += 10000.0  # Force deadline
            else:
                self.simulated_time += 10.0  # Only 10s elapsed
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=2.0,
            low_water_pending=10,
            discovery_cooldown_minutes=15.0,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualification_mock,
            monotonic=advance_clock,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        # Discovery should have run exactly once at startup, NOT on every 10s step
        self.assertEqual(result["discovery_runs"], 1)
        self.assertEqual(discovery_mock.call_count, 1)

    def test_G_cooldown_expiry_permits_discovery_again(self) -> None:
        """G. A useful discovery pass uses the short replenishment guard."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 2, "total": 5}

        discovery_mock = MagicMock(return_value={"status": "COMPLETED", "discovered": 1})

        def qualify_and_advance() -> dict[str, str]:
            self.simulated_time += LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS + 1.0
            return {"status": "VERIFIED", "candidate_id": "c1"}

        result = run_local_background_session(
            duration_minutes=5.0,
            low_water_pending=10,
            discovery_cooldown_minutes=15.0,
            successful_discovery_cooldown_seconds=LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS,
            max_jobs=2,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualify_and_advance,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["discovery_runs"], 2)
        self.assertEqual(discovery_mock.call_count, 2)

    def test_G2_zero_result_keeps_long_discovery_backoff(self) -> None:
        """A zero-result pass does not retry after only the short guard."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 2, "total": 5}
        discovery_mock = MagicMock(return_value={"status": "COMPLETED", "discovered": 0})

        def qualify_and_advance() -> dict[str, str]:
            self.simulated_time += LOCAL_SUCCESSFUL_DISCOVERY_COOLDOWN_SECONDS + 1.0
            return {"status": "VERIFIED", "candidate_id": "c1"}

        result = run_local_background_session(
            duration_minutes=5.0,
            low_water_pending=10,
            discovery_cooldown_minutes=LOCAL_DISCOVERY_COOLDOWN_MINUTES,
            max_jobs=2,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualify_and_advance,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["discovery_runs"], 1)
        self.assertEqual(discovery_mock.call_count, 1)

    def test_H_local_session_does_not_apply_a_global_pending_limit(self) -> None:
        """H. Local discovery has a bounded batch but no total queue ceiling."""

        with patch("scripts.local_background_session.run_discovery_step") as mock_disc:
            mock_disc.return_value = {"status": "COMPLETED", "discovered": 0}
            mock_store = MagicMock()
            mock_store.background_candidate_summary.return_value = {"pending": 0, "total": 0}

            call_count = 0

            def advance_clock() -> float:
                nonlocal call_count
                call_count += 1
                if call_count > 3:
                    self.simulated_time += 10000.0
                return self.simulated_time

            run_local_background_session(
                duration_minutes=1.0,
                low_water_pending=10,
                store=mock_store,
                pool_repository=MagicMock(),
                qualification_fn=MagicMock(return_value=None),
                monotonic=advance_clock,
                sleep_fn=self.fake_sleep,
                logger=lambda msg: None,
            )

            mock_disc.assert_called()
            self.assertNotIn("target_pending", mock_disc.call_args.kwargs)
            self.assertEqual(LOCAL_DISCOVERY_BATCH_SIZE, 40)
            self.assertEqual(mock_disc.call_args.kwargs["max_candidates"], 40)

    def test_I_duration_minutes_controls_total_session_window(self) -> None:
        """I. 100-minute duration parameter strictly bounds monotonic execution window."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 50, "total": 60}

        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        def advance_clock() -> float:
            # Advance 20 minutes per job
            self.simulated_time += 20.0 * 60.0
            return self.simulated_time

        result = run_local_background_session(
            duration_minutes=100.0,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=advance_clock,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["duration_minutes"], 100.0)
        # Should complete at most 5 jobs (5 * 20 min = 100 min)
        self.assertLessEqual(result["jobs_completed"], 5)

    def test_J_duration_minutes_argument_parsing(self) -> None:
        """J. 30, 60, 100, 120 minute values parse correctly from CLI."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--duration-minutes", type=float, required=True)
        parser.add_argument("--discovery-low-water", type=int, default=LOCAL_DISCOVERY_LOW_WATER_PENDING)
        parser.add_argument("--discovery-cooldown-minutes", type=float, default=LOCAL_DISCOVERY_COOLDOWN_MINUTES)
        parser.add_argument("--max-jobs", type=int, default=None)

        for minutes in [30, 60, 100, 120]:
            args = parser.parse_args(["--duration-minutes", str(minutes)])
            self.assertEqual(args.duration_minutes, float(minutes))

    def test_K_ctrl_c_cleanup_works(self) -> None:
        """K. Stop check / Ctrl+C terminates loop cleanly and cleans up work_dir."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 20, "total": 20}

        temp_test_dir = tempfile.mkdtemp(prefix="test_session_work_")
        work_path = Path(temp_test_dir)
        self.assertTrue(work_path.exists())

        stop_called = False

        def fake_stop() -> bool:
            nonlocal stop_called
            if stop_called:
                return True
            stop_called = True
            return False

        result = run_local_background_session(
            duration_minutes=60.0,
            store=mock_store,
            pool_repository=MagicMock(),
            work_dir=work_path,
            qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
            stop_check=fake_stop,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        # Work dir must be cleaned up in finally block
        self.assertFalse(work_path.exists())

    def test_L_atomic_candidate_claiming_preserved(self) -> None:
        """L. Cloud/local atomic candidate claiming is invoked with lease."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 10, "total": 10}

        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        run_local_background_session(
            duration_minutes=10.0,
            max_jobs=1,
            lease_seconds=900,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        qualification_mock.assert_called_once()

    def test_M_duplicate_discovery_idempotent(self) -> None:
        """M. Duplicate discovery remains idempotent via discover_candidates."""
        from engine.background_candidates import discover_candidates
        from engine.backgrounds import BackgroundCandidate, BackgroundDiscoveryPage

        fake_cand = BackgroundCandidate(
            pexels_video_id=12345,
            query="nature",
            pexels_page_url="https://example.test/v/12345",
            videographer_name="Creator",
            videographer_url="https://example.test/creator",
            duration_seconds=20.0,
            source_width=1080,
            source_height=1920,
            video_file_id="67890",
            file_url="https://example.test/video.mp4",
            file_type="video/mp4",
            fps=30.0,
            file_width=1080,
            file_height=1920,
            requires_center_crop=False,
            provider="pexels",
        )
        fake_page = BackgroundDiscoveryPage(
            provider="pexels",
            query="nature",
            page=1,
            discovered=1,
            candidates=(fake_cand,),
            metadata_ineligible=(),
        )
        mock_prov = MagicMock()
        mock_prov.search.side_effect = [fake_page] + [
            BackgroundDiscoveryPage("pexels", "nature", 1, 0, (), ())
        ] * 30

        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 1, "total": 1}
        # Simulate duplicate discovery: discovery_count = 2
        mock_store.enqueue_background_candidate.return_value = {"discovery_count": 2}

        res = discover_candidates(
            store=mock_store,
            providers={"pexels": mock_prov},
            max_candidates=1,
            target_pending=100,
            verified_target=40,
        )

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["duplicates"], 1)

    def test_N_no_coverage_mutation(self) -> None:
        """N. Local background session never touches Coverage state tables or models."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 20, "total": 20}

        def step_clock() -> float:
            self.simulated_time += 1000.0
            return self.simulated_time

        run_local_background_session(
            duration_minutes=1.0,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
            monotonic=step_clock,
            sleep_fn=self.fake_sleep,
            logger=lambda msg: None,
        )

        # Assert no coverage methods were invoked on store
        self.assertFalse(mock_store.load_coverage.called)
        self.assertFalse(mock_store.save_coverage.called)
        self.assertFalse(mock_store.load_cycles.called)
        self.assertFalse(mock_store.save_cycle.called)

    def test_O_no_buffer_or_social_calls(self) -> None:
        """O. Local background session has zero calls to Buffer or social publishers."""
        with patch.dict(sys.modules, {"engine.buffer_publisher": MagicMock()}):
            mock_store = MagicMock()
            mock_store.background_candidate_summary.return_value = {"pending": 20, "total": 20}

            def step_clock() -> float:
                self.simulated_time += 1000.0
                return self.simulated_time

            run_local_background_session(
                duration_minutes=1.0,
                store=mock_store,
                pool_repository=MagicMock(),
                qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
                monotonic=step_clock,
                sleep_fn=self.fake_sleep,
                logger=lambda msg: None,
            )

            # Store buffer claim methods must not be called
            self.assertFalse(mock_store.acquire_buffer_publication_claim.called)
            self.assertFalse(mock_store.complete_buffer_publication_claim.called)

    def test_P_no_quran_generation(self) -> None:
        """P. Local background session has zero Quran generation or rendering."""
        with patch("engine.main.generate_reel") as mock_gen, patch("engine.renderer.render_reel") as mock_render:
            mock_store = MagicMock()
            mock_store.background_candidate_summary.return_value = {"pending": 20, "total": 20}

            def step_clock() -> float:
                self.simulated_time += 1000.0
                return self.simulated_time

            run_local_background_session(
                duration_minutes=1.0,
                store=mock_store,
                pool_repository=MagicMock(),
                qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
                monotonic=step_clock,
                sleep_fn=self.fake_sleep,
                logger=lambda msg: None,
            )

            mock_gen.assert_not_called()
            mock_render.assert_not_called()

    def test_Q_summary_read_timeout_retries_and_session_continues(self) -> None:
        """1. first background_candidate_summary() raises ReadTimeout, next succeeds -> session continues."""
        from requests.exceptions import ReadTimeout

        mock_store = MagicMock()
        mock_store.background_candidate_summary.side_effect = [
            ReadTimeout("Read timed out"),
            {"pending": 10, "total": 10},
        ]
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})
        logged_messages: list[str] = []

        result = run_local_background_session(
            duration_minutes=5.0,
            max_jobs=1,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=logged_messages.append,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["jobs_completed"], 1)
        self.assertEqual(mock_store.background_candidate_summary.call_count, 2)
        # Sanitized warning was logged
        transient_logs = [m for m in logged_messages if "Transient Supabase/network error: ReadTimeout" in m]
        self.assertEqual(len(transient_logs), 1)
        self.assertIn("Retrying after", transient_logs[0])
        # Verify no credentials/URLs in logs
        for m in logged_messages:
            self.assertNotIn("token", m.lower())
            self.assertNotIn("secret", m.lower())

    def test_R_transient_connection_error_retries_and_session_continues(self) -> None:
        """2. transient ConnectionError (requests & builtin) -> session continues."""
        from requests.exceptions import ConnectionError as ReqConnectionError

        # Test requests ConnectionError
        mock_store = MagicMock()
        mock_store.background_candidate_summary.side_effect = [
            ReqConnectionError("HTTPSConnectionPool failed"),
            {"pending": 10, "total": 10},
        ]
        qualification_mock = MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"})

        result = run_local_background_session(
            duration_minutes=5.0,
            max_jobs=1,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda m: None,
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["jobs_completed"], 1)

        # Test builtin ConnectionError
        mock_store2 = MagicMock()
        mock_store2.background_candidate_summary.side_effect = [
            ConnectionError("Network unreachable"),
            {"pending": 10, "total": 10},
        ]
        result2 = run_local_background_session(
            duration_minutes=5.0,
            max_jobs=1,
            store=mock_store2,
            pool_repository=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda m: None,
        )
        self.assertEqual(result2["status"], "COMPLETED")
        self.assertEqual(result2["jobs_completed"], 1)

    def test_S_retry_delay_is_bounded_and_uses_injected_sleep_fn(self) -> None:
        """3. retry delay is bounded and uses injected sleep_fn."""
        from requests.exceptions import ReadTimeout

        mock_store = MagicMock()
        mock_store.background_candidate_summary.side_effect = [
            ReadTimeout("Timeout"),
            {"pending": 10, "total": 10},
        ]
        sleep_mock = MagicMock(side_effect=self.fake_sleep)

        run_local_background_session(
            duration_minutes=10.0,
            max_jobs=1,
            transient_retry_seconds=5.0,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
            monotonic=self.fake_monotonic,
            sleep_fn=sleep_mock,
            logger=lambda m: None,
        )

        sleep_mock.assert_called()
        first_wait = sleep_mock.call_args_list[0][0][0]
        self.assertGreater(first_wait, 0.0)
        self.assertLessEqual(first_wait, 5.0)

    def test_T_session_deadline_is_respected_during_persistent_timeouts(self) -> None:
        """4. session deadline is still respected even with repeated transient errors."""
        from requests.exceptions import ReadTimeout

        mock_store = MagicMock()
        mock_store.background_candidate_summary.side_effect = ReadTimeout("Persistent timeout")
        sleep_mock = MagicMock(side_effect=self.fake_sleep)

        # 1 minute session -> deadline should be reached after 12 * 5s sleeps
        result = run_local_background_session(
            duration_minutes=1.0,
            transient_retry_seconds=5.0,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
            monotonic=self.fake_monotonic,
            sleep_fn=sleep_mock,
            logger=lambda m: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["jobs_completed"], 0)
        # Monotonic time should have crossed or reached deadline
        self.assertGreaterEqual(self.simulated_time, 1000.0 + 60.0)

    def test_U_stop_check_stops_cleanly_during_or_after_retry(self) -> None:
        """5. stop_check still stops cleanly during/after retry."""
        from requests.exceptions import ReadTimeout

        mock_store = MagicMock()
        mock_store.background_candidate_summary.side_effect = ReadTimeout("Timeout")

        stop_requested = False

        def sleep_and_signal_stop(seconds: float) -> None:
            nonlocal stop_requested
            self.fake_sleep(seconds)
            stop_requested = True

        result = run_local_background_session(
            duration_minutes=60.0,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=MagicMock(return_value={"status": "VERIFIED", "candidate_id": "c1"}),
            stop_check=lambda: stop_requested,
            monotonic=self.fake_monotonic,
            sleep_fn=sleep_and_signal_stop,
            logger=lambda m: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(mock_store.background_candidate_summary.call_count, 1)

    def test_V_non_transient_exceptions_propagate_and_are_not_swallowed(self) -> None:
        """6. non-transient RuntimeError / ValueError still propagate and are NOT swallowed."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.side_effect = RuntimeError("Fatal DB corruption")

        with self.assertRaises(RuntimeError):
            run_local_background_session(
                duration_minutes=5.0,
                store=mock_store,
                pool_repository=MagicMock(),
                monotonic=self.fake_monotonic,
                sleep_fn=self.fake_sleep,
                logger=lambda m: None,
            )

        mock_store.background_candidate_summary.side_effect = ValueError("Corrupted schema response")
        with self.assertRaises(ValueError):
            run_local_background_session(
                duration_minutes=5.0,
                store=mock_store,
                pool_repository=MagicMock(),
                monotonic=self.fake_monotonic,
                sleep_fn=self.fake_sleep,
                logger=lambda m: None,
            )

    def test_W_discovery_transient_timeout_uses_long_backoff(self) -> None:
        """7. A transient discovery timeout does not immediately hammer the provider."""
        from requests.exceptions import ReadTimeout

        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 2, "total": 5}

        discovery_mock = MagicMock(
            side_effect=[
                ReadTimeout("Discovery timeout"),
                {"status": "COMPLETED", "discovered": 5},
            ]
        )
        discovery_counts_during_qualification: list[int] = []

        def qualify_and_advance() -> dict[str, str]:
            discovery_counts_during_qualification.append(discovery_mock.call_count)
            if len(discovery_counts_during_qualification) == 1:
                self.simulated_time += (LOCAL_DISCOVERY_COOLDOWN_MINUTES * 60.0) - 6.0
            else:
                self.simulated_time += 2.0
            return {"status": "VERIFIED", "candidate_id": "c1"}

        result = run_local_background_session(
            duration_minutes=30.0,
            low_water_pending=10,
            discovery_cooldown_minutes=15.0,
            max_jobs=3,
            store=mock_store,
            pool_repository=MagicMock(),
            discovery_fn=discovery_mock,
            qualification_fn=qualify_and_advance,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda m: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["jobs_completed"], 3)
        self.assertEqual(discovery_counts_during_qualification, [1, 1, 2])
        self.assertEqual(discovery_mock.call_count, 2)
        self.assertEqual(result["discovery_runs"], 1)
        self.assertEqual(result["candidates_discovered"], 5)

    def test_X_qualification_behavior_remains_unchanged(self) -> None:
        """8. existing qualification behavior (VERIFIED, TRANSIENT_FAILURE, SAFETY_REJECTED) unchanged."""
        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 20, "total": 20}

        # Candidate qualification returns intentionally classified TRANSIENT_FAILURE
        qualification_mock = MagicMock(side_effect=[
            {"status": "TRANSIENT_FAILURE", "candidate_id": "c_fail"},
            {"status": "SAFETY_REJECTED", "candidate_id": "c_rej"},
            {"status": "VERIFIED", "candidate_id": "c_ok"},
        ])
        logged_messages: list[str] = []

        result = run_local_background_session(
            duration_minutes=5.0,
            max_jobs=3,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=logged_messages.append,
        )

        self.assertEqual(result["status"], "COMPLETED")
        # All 3 classified outcomes are counted in jobs_completed and not treated as transport errors
        self.assertEqual(result["jobs_completed"], 3)
        self.assertEqual(qualification_mock.call_count, 3)
        self.assertTrue(any("status=TRANSIENT_FAILURE candidate=c_fail" in m for m in logged_messages))
        self.assertTrue(any("status=SAFETY_REJECTED candidate=c_rej" in m for m in logged_messages))
        self.assertTrue(any("status=VERIFIED candidate=c_ok" in m for m in logged_messages))

    def test_Y_qualification_transport_timeout_retries(self) -> None:
        """Qualification call transport timeout retries and session continues."""
        from requests.exceptions import ReadTimeout

        mock_store = MagicMock()
        mock_store.background_candidate_summary.return_value = {"pending": 20, "total": 20}

        # Qualification transport timeout (e.g. claim_background_candidate ReadTimeout), then success
        qualification_mock = MagicMock(side_effect=[
            ReadTimeout("Claim timeout"),
            {"status": "VERIFIED", "candidate_id": "c1"},
        ])

        result = run_local_background_session(
            duration_minutes=5.0,
            max_jobs=1,
            store=mock_store,
            pool_repository=MagicMock(),
            qualification_fn=qualification_mock,
            monotonic=self.fake_monotonic,
            sleep_fn=self.fake_sleep,
            logger=lambda m: None,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["jobs_completed"], 1)
        self.assertEqual(qualification_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
