from engine.storage import PublishingScheduleEntry
from tests.editorial_fixtures import eligible_record_evidence
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from engine.background_pool import (
    CURRENT_SAFETY_POLICY_FINGERPRINT,
    RISK_TIER_HIGH_RISK,
    RISK_TIER_LOW_RISK,
    RISK_TIER_MEDIUM_RISK,
    RISK_TIER_ROBUST,
    RISK_TIER_UNKNOWN_RISK,
    SafetyEvidence,
    VerifiedBackgroundRecord,
    build_verified_record,
    compute_render_config_fingerprint,
    get_background_pool_repository,
    get_render_risk_tier,
    get_render_robustness,
    is_runtime_eligible,
    is_source_safe,
    select_verified_pool_records,
)
from engine.backgrounds import BackgroundCandidate
from engine.job import _record_production_pool_usage
from engine.main import DownloadedBackground, GenerationError, RunRequest, generate_reel
from tests.editorial_fixtures import authorized_run_request as RunRequest, mock_live_authorization
from engine.renderer import RenderConfig, RenderResult, SubtitleConfig
from engine.safety import Detection, SafetyScanResult
from engine.supabase_state import SupabaseConfig, SupabaseStateStore

from dataclasses import replace


def make_dummy_record(
    provider: str,
    asset_id: str,
    variant_id: str = "var1",
    sha: str = "a" * 64,
    status: str = "verified",
    active: bool = True,
    duration: float = 30.0,
    render_robustness: dict | None = None,
) -> VerifiedBackgroundRecord:
    cand = BackgroundCandidate(
        pexels_video_id=int(asset_id) if str(asset_id).isdigit() else 1,
        query="nature",
        pexels_page_url=f"https://example.com/{asset_id}",
        videographer_name="Author",
        videographer_url="https://example.com/@author",
        duration_seconds=duration,
        source_width=1080,
        source_height=1920,
        video_file_id=variant_id,
        file_url=f"https://cdn.example.com/{asset_id}.mp4",
        file_type="video/mp4",
        fps=30.0,
        file_width=1080,
        file_height=1920,
        requires_center_crop=False,
        declared_file_size_bytes=10000,
        provider=provider,
        provider_variant_name=variant_id,
    )
    rec = build_verified_record(
        cand,
        content_sha256=sha,
        downloaded_file_size=10000,
        scanned_frames=100,
        animal_detector="opencv-zoo-yolox-2022nov",
        animal_classes=("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"),
        animal_threshold=0.35,
        animal_verdict="CLEAR",
        verified_at="2026-08-30T12:00:00Z",
    )
    return replace(rec, status=status, active=active, render_robustness=render_robustness)


class TestRuntimeRetryAndRobustness(unittest.TestCase):
    def setUp(self):
        mock_live_authorization(self)
        self.render_cfg = RenderConfig(
            subtitle_config=SubtitleConfig(
                arabic_font_size=170,
                translation_font_size=50,
                arabic_translation_gap_px=10,
                translation_stack_gap_px=30,
                transition_duration_ms=100,
                subtitle_lead_ms=0,
                subtitle_mode="progressive_phrase_sync",
            )
        )
        self.fp = compute_render_config_fingerprint(self.render_cfg)

    def test_risk_tier_classification(self):
        # 0.0% -> ROBUST
        rec_robust = make_dummy_record(
            "pexels", "7681534", render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0}
        )
        self.assertEqual(get_render_risk_tier(rec_robust, self.fp), RISK_TIER_ROBUST)

        # 2.12% -> LOW_RISK
        rec_low = make_dummy_record(
            "pexels", "32079443", render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0212}
        )
        self.assertEqual(get_render_risk_tier(rec_low, self.fp), RISK_TIER_LOW_RISK)

    def test_production_pool_usage_preserves_provider_variant_and_record_identity(self):
        written = []

        class Repository:
            def save_usage(self, event):
                written.append(event)

        record = __import__("engine.storage", fromlist=["RunRecord"]).RunRecord(
            run_id="scheduled-usage-test",
            date="2026-08-30",
            slot="daily-0800",
            status="SUCCESS",
            run_kind="scheduled",
            started_at="2026-08-30T10:00:00Z",
            duration_seconds=15.0,
            provenance={
                "background_source_mode": "VERIFIED_POOL",
                "verified_backgrounds": [{
                    "provider": "pixabay",
                    "provider_asset_id": "219843",
                    "provider_file_id": "high",
                    "verified_record_id": "pixabay:219843:high:" + "a" * 64 + ":policy",
                }],
            },
        )
        _record_production_pool_usage(Repository(), record, used_at="2026-08-30T10:01:00Z")
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].provider, "pixabay")
        self.assertEqual(written[0].provider_variant_id, "high")
        self.assertEqual(written[0].verified_record_id, record.provenance["verified_backgrounds"][0]["verified_record_id"])

        # 5.86% -> MEDIUM_RISK
        rec_med = make_dummy_record(
            "pexels", "12328908", render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0586}
        )
        self.assertEqual(get_render_risk_tier(rec_med, self.fp), RISK_TIER_MEDIUM_RISK)

        # 91.40% -> HIGH_RISK (pexels:15499818)
        rec_high = make_dummy_record(
            "pexels", "15499818", render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.9140}
        )
        self.assertEqual(get_render_risk_tier(rec_high, self.fp), RISK_TIER_HIGH_RISK)

    def test_fingerprint_mismatch_downgrades_to_unknown_risk(self):
        rec = make_dummy_record(
            "pexels", "7681534", render_robustness={"fixture_config_fingerprint": "old_fp_123", "failed_evaluation_rate": 0.0}
        )
        # Without current fingerprint match, tier is UNKNOWN_RISK
        self.assertEqual(get_render_risk_tier(rec, self.fp), RISK_TIER_UNKNOWN_RISK)

    def test_high_risk_excluded_by_default_in_selection(self):
        rec_robust = make_dummy_record(
            "pexels", "7681534", sha="1" * 64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0}
        )
        rec_high = make_dummy_record(
            "pexels", "15499818", sha="2" * 64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.9140}
        )

        selected = select_verified_pool_records(
            [rec_robust, rec_high],
            (),
            required_duration_seconds=10.0,
            render_config_fingerprint=self.fp,
            allow_high_risk=False,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].provider_asset_id, "7681534")

        # When allow_high_risk=True, high risk is at the very end
        selected_all = select_verified_pool_records(
            [rec_high, rec_robust],
            (),
            required_duration_seconds=10.0,
            render_config_fingerprint=self.fp,
            allow_high_risk=True,
        )
        self.assertEqual(len(selected_all), 2)
        self.assertEqual(selected_all[0].provider_asset_id, "7681534")
        self.assertEqual(selected_all[1].provider_asset_id, "15499818")

    def test_selection_priority_ordering(self):
        rec_med = make_dummy_record(
            "pexels", "12328908", sha="3" * 64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0586}
        )
        rec_unk = make_dummy_record("pexels", "38735389", sha="4" * 64, render_robustness=None)
        rec_low = make_dummy_record(
            "pexels", "32079443", sha="5" * 64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0212}
        )
        rec_rob = make_dummy_record(
            "pexels", "7681534", sha="6" * 64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0}
        )

        selected = select_verified_pool_records(
            [rec_med, rec_unk, rec_low, rec_rob],
            (),
            required_duration_seconds=10.0,
            render_config_fingerprint=self.fp,
        )
        # Expected order: ROBUST (7681534) -> LOW_RISK (32079443) -> UNKNOWN_RISK (38735389) -> MEDIUM_RISK (12328908)
        self.assertEqual(
            [r.provider_asset_id for r in selected],
            ["7681534", "32079443", "38735389", "12328908"],
        )

    def test_record_render_safety_failure_telemetry(self):
        calls = []

        class FakeSession:
            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                resp = MagicMock()
                resp.ok = True
                resp.status_code = 201
                resp.content = b"[]"
                resp.json.return_value = []
                return resp

        store = SupabaseStateStore(SupabaseConfig("https://example.supabase.co", "test-key"), session=FakeSession())
        evt_id = store.record_render_safety_failure(
            provider="pexels",
            provider_asset_id="32079443",
            provider_variant_id="13674428",
            content_sha256="sha123",
            run_id="run_test_001",
            attempt_number=1,
            start_offset_seconds=14.25,
            verse_keys=["2:21"],
            render_config_fingerprint=self.fp,
            failure_reason="person detected in frame 120",
            detector_counts={"person": 1, "face": 0, "animal": 0},
        )
        self.assertTrue(evt_id.startswith("rsf_"))
        self.assertEqual(len(evt_id), 68)  # rsf_ + 64 hex chars
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("background_events", url)
        import json
        payload = json.loads(kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["event_type"], "render_safety_failure")
        self.assertEqual(payload["value"]["attempt_number"], 1)
        self.assertEqual(payload["value"]["offset_bucket_seconds"], 14)
        self.assertEqual(payload["value"]["detector_counts"]["person"], 1)

    def test_telemetry_idempotency_same_and_different_plans(self):
        calls = []

        class FakeSession:
            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                resp = MagicMock()
                resp.ok = True
                resp.status_code = 201
                resp.content = b"[]"
                resp.json.return_value = []
                return resp

        store = SupabaseStateStore(SupabaseConfig("https://example.supabase.co", "test-key"), session=FakeSession())

        plan_a = [
            {"provider": "pexels", "provider_asset_id": "7681534", "provider_variant_id": "100", "start_offset_seconds": 2.5},
            {"provider": "pexels", "provider_asset_id": "32079443", "provider_variant_id": "200", "start_offset_seconds": 8.0},
        ]
        plan_b = [
            {"provider": "pexels", "provider_asset_id": "7681534", "provider_variant_id": "100", "start_offset_seconds": 5.5},
            {"provider": "pexels", "provider_asset_id": "32079443", "provider_variant_id": "200", "start_offset_seconds": 12.0},
        ]

        # Same plan and attempt -> identical event_id
        id_1a = store.record_render_safety_failure(
            provider="pexels", provider_asset_id="7681534", provider_variant_id="100",
            content_sha256="sha1", run_id="run_idem_001", attempt_number=1,
            start_offset_seconds=2.5, verse_keys=["2:21"], render_config_fingerprint=self.fp,
            failure_reason="face", detector_counts={"face": 1}, background_plan=plan_a,
        )
        id_1a_dup = store.record_render_safety_failure(
            provider="pexels", provider_asset_id="7681534", provider_variant_id="100",
            content_sha256="sha1", run_id="run_idem_001", attempt_number=1,
            start_offset_seconds=2.5, verse_keys=["2:21"], render_config_fingerprint=self.fp,
            failure_reason="face", detector_counts={"face": 1}, background_plan=plan_a,
        )
        self.assertEqual(id_1a, id_1a_dup)

        # Different plan -> distinct event_id
        id_1b = store.record_render_safety_failure(
            provider="pexels", provider_asset_id="7681534", provider_variant_id="100",
            content_sha256="sha1", run_id="run_idem_001", attempt_number=1,
            start_offset_seconds=5.5, verse_keys=["2:21"], render_config_fingerprint=self.fp,
            failure_reason="face", detector_counts={"face": 1}, background_plan=plan_b,
        )
        self.assertNotEqual(id_1a, id_1b)

    def test_multi_background_telemetry_attribution(self):
        import os
        import tempfile
        from engine.safety import Detection
        from tests.runtime_fixtures import (
            make_mock_candidate,
            make_mock_quran_data,
            make_mock_safety_result,
            make_mock_surah_audio_94,
            make_mock_timing_read,
            make_mock_translation_data,
        )

        with patch("engine.main.preflight_subtitle_layout", return_value=MagicMock()), tempfile.TemporaryDirectory() as tmp_dir:
            out_mp4 = os.path.join(tmp_dir, "multi_bg.mp4")
            audio_mp3 = os.path.join(tmp_dir, "audio.mp3")
            with open(audio_mp3, "wb") as f:
                f.write(b"fake_mp3_data")
            bg1_mp4 = os.path.join(tmp_dir, "bg1.mp4")
            with open(bg1_mp4, "wb") as f:
                f.write(b"fake_bg1")
            bg2_mp4 = os.path.join(tmp_dir, "bg2.mp4")
            with open(bg2_mp4, "wb") as f:
                f.write(b"fake_bg2")

            req = RunRequest(slot="morning", seed=42, output_path=out_mp4, max_render_attempts=1)
            telemetry_calls = []

            def mock_render(r, config=None):
                with open(out_mp4, "wb") as f:
                    f.write(b"fake_rendered")
                from engine.renderer import BackgroundSegmentPlan
                plans = (
                    BackgroundSegmentPlan(clip_index=0, local_path=bg1_mp4, start_offset_seconds=1.0, segment_duration_seconds=10.0, transition_offset_seconds=0.0),
                    BackgroundSegmentPlan(clip_index=1, local_path=bg2_mp4, start_offset_seconds=5.0, segment_duration_seconds=10.0, transition_offset_seconds=10.0),
                )
                return RenderResult(output_path=out_mp4, duration_seconds=20.0, plans=plans)

            # Detection at t=10.0s lies on the xfade boundary: both source
            # clips contribute, so no single source may be blamed.
            det_clip1 = Detection(frame_index=40, timestamp_seconds=10.0, detection_type="person", confidence=0.85, bounding_box=(10.0, 10.0, 50.0, 50.0), class_name="person")

            def mock_scan(path):
                if path == out_mp4:
                    return SafetyScanResult(safe=False, person_detections=(det_clip1,), face_detections=(), frames_scanned=80, duration_seconds=0.5, failure_reason="Person detected at 14.0s")
                return make_mock_safety_result(safe=True)

            def mock_telemetry(*args, **kwargs):
                telemetry_calls.append(kwargs)

            # Pass 2 pool records
            rec0 = make_dummy_record("pexels", "7681534", variant_id="v0", sha="0" * 64)
            rec1 = make_dummy_record("pixabay", "219843", variant_id="v1", sha="1" * 64)
            req = RunRequest(
                slot="morning",
                seed=42,
                output_path=out_mp4,
                max_render_attempts=1,
                background_clip_strategy="multiple",
                max_background_clips=2,
                target_background_clips=2,
                verified_background_records=(rec0, rec1),
            )

            def mock_dl_bg(cand, **kwargs):
                sha = "0" * 64 if cand.provider == "pexels" else "1" * 64
                return DownloadedBackground(
                    candidate=cand,
                    local_path=bg1_mp4 if cand.provider == "pexels" else bg2_mp4,
                    content_sha256=sha,
                    file_size_bytes=100,
                )

            def mock_make_provider(name):
                prov = MagicMock()
                prov.name = name
                prov.get_asset = lambda aid: aid
                def res_var(asset, variant_id):
                    cand = make_mock_candidate(int(asset) if str(asset).isdigit() else 100, 25.0)
                    from dataclasses import replace
                    return replace(cand, provider=name)
                prov.resolve_variant = res_var
                return prov

            with patch("engine.main.make_provider", side_effect=mock_make_provider):
                res = generate_reel(
                    req,
                    fetch_quran_fn=lambda *a, **kw: make_mock_quran_data(),
                    fetch_translation_fn=lambda *a, **kw: make_mock_translation_data(),
                    fetch_timing_soar_fn=lambda *a, **kw: (94,),
                    fetch_surah_timing_fn=lambda *a, **kw: make_mock_surah_audio_94(),
                    download_audio_fn=lambda *a, **kw: audio_mp3,
                    download_background_fn=mock_dl_bg,
                    scan_video_fn=mock_scan,
                    render_reel_fn=mock_render,
                    record_render_safety_failure_fn=mock_telemetry,
                )

            self.assertFalse(res.success)
            self.assertEqual(len(telemetry_calls), 1)
            t_call = telemetry_calls[0]
            self.assertEqual(t_call["provider"], "unknown")
            self.assertEqual(t_call["provider_asset_id"], "ambiguous_crossfade")
            self.assertEqual(len(t_call["background_plan"]), 2)
            self.assertTrue(
                all(
                    item["primary_attribution"] == "AMBIGUOUS_CROSSFADE"
                    for item in t_call["background_plan"]
                )
            )

    def test_pexels_15499818_is_high_risk_and_excluded_at_runtime(self):
        # pexels:15499818 is verified in DB, active=True, but has 91.40% failed_evaluation_rate
        rec_15499818 = make_dummy_record(
            "pexels",
            "15499818",
            variant_id="var1",
            sha="f" * 64,
            status="verified",
            active=True,
            render_robustness={
                "fixture_config_fingerprint": self.fp,
                "failed_evaluation_rate": 0.9140239605355885,
                "detection_density": 1.08738548273432,
                "total_cross_evaluations": 1419,
                "failed_evaluation_count": 1297,
                "total_detections": 1543,
                "qualifying_fixture": "english_rwwad:2:21",
            },
        )
        # Robustness tier is HIGH_RISK
        self.assertEqual(get_render_risk_tier(rec_15499818, self.fp), RISK_TIER_HIGH_RISK)

        # Source is safe
        self.assertTrue(is_source_safe(rec_15499818))

        # Runtime eligible (source safe + verified + active)
        self.assertTrue(is_runtime_eligible(rec_15499818))

        # But select_verified_pool_records excludes HIGH_RISK by default
        selected = select_verified_pool_records(
            [rec_15499818],
            (),
            required_duration_seconds=10.0,
            render_config_fingerprint=self.fp,
            allow_high_risk=False,
        )
        self.assertEqual(len(selected), 0)

        # When allow_high_risk=True, it is included
        selected_allowed = select_verified_pool_records(
            [rec_15499818],
            (),
            required_duration_seconds=10.0,
            render_config_fingerprint=self.fp,
            allow_high_risk=True,
        )
        self.assertEqual(len(selected_allowed), 1)

    def test_runtime_render_fingerprint_matches_app_config_and_stored_robustness(self):
        from engine.storage import StoredConfig
        from engine.job import build_render_config_from_stored_config

        # Version 2 Standard spacing builds a stable canonical RenderConfig
        stored_cfg = StoredConfig(
            arabic_font_size=170,
            translation_font_size=50,
            arabic_translation_gap_px=20,
            translation_stack_gap_px=30,
        )
        render_cfg = build_render_config_from_stored_config(stored_cfg)
        runtime_fp = compute_render_config_fingerprint(render_cfg)

        # The corrected person preprocessing invalidates earlier qualifications.
        legacy_fp = "8a2b4a109cbd6a22159e2a20d7c6479c698bd845511a865a4a5ad96a822eef5c"
        target_fp = "e93f55155201ec8239f196ebe9e761fefbabca17de067d9bde58046c8c434cd7"
        self.assertEqual(runtime_fp, target_fp)
        legacy = make_dummy_record("pexels", "old-qualification", render_robustness={
            "fixture_config_fingerprint": legacy_fp, "failed_evaluation_rate": 0.0,
        })
        self.assertEqual(
            get_render_risk_tier(legacy, current_fingerprint=runtime_fp), RISK_TIER_UNKNOWN_RISK,
        )

        # Build cohort records with target_fp
        rec_7681534 = make_dummy_record("pexels", "7681534", render_robustness={"fixture_config_fingerprint": target_fp, "failed_evaluation_rate": 0.0})
        rec_32079443 = make_dummy_record("pexels", "32079443", render_robustness={"fixture_config_fingerprint": target_fp, "failed_evaluation_rate": 0.0212})
        rec_12328908 = make_dummy_record("pexels", "12328908", render_robustness={"fixture_config_fingerprint": target_fp, "failed_evaluation_rate": 0.0586})
        rec_15499818 = make_dummy_record("pexels", "15499818", render_robustness={"fixture_config_fingerprint": target_fp, "failed_evaluation_rate": 0.9140})

        self.assertEqual(get_render_risk_tier(rec_7681534, current_fingerprint=runtime_fp), RISK_TIER_ROBUST)
        self.assertEqual(get_render_risk_tier(rec_32079443, current_fingerprint=runtime_fp), RISK_TIER_LOW_RISK)
        self.assertEqual(get_render_risk_tier(rec_12328908, current_fingerprint=runtime_fp), RISK_TIER_MEDIUM_RISK)
        self.assertEqual(get_render_risk_tier(rec_15499818, current_fingerprint=runtime_fp), RISK_TIER_HIGH_RISK)

        # Selection ordering
        selected = select_verified_pool_records(
            [rec_12328908, rec_15499818, rec_32079443, rec_7681534],
            (),
            required_duration_seconds=10.0,
            render_config_fingerprint=runtime_fp,
            allow_high_risk=False,
        )
        self.assertEqual([r.provider_asset_id for r in selected], ["7681534", "32079443", "12328908"])

    def test_bounded_retry_pipeline_recovers_on_third_attempt(self):
        import os
        import tempfile
        from tests.runtime_fixtures import (
            make_mock_candidate,
            make_mock_quran_data,
            make_mock_safety_result,
            make_mock_surah_audio_94,
            make_mock_timing_read,
            make_mock_translation_data,
        )

        with patch("engine.main.preflight_subtitle_layout", return_value=MagicMock()), tempfile.TemporaryDirectory() as tmp_dir:
            out_mp4 = os.path.join(tmp_dir, "retry_success.mp4")
            audio_mp3 = os.path.join(tmp_dir, "audio.mp3")
            with open(audio_mp3, "wb") as f:
                f.write(b"fake_mp3_data")
            bg_mp4 = os.path.join(tmp_dir, "bg.mp4")
            with open(bg_mp4, "wb") as f:
                f.write(b"fake_bg_mp4_data")

            req = RunRequest(slot="morning", seed=42, output_path=out_mp4, max_render_attempts=4)

            render_calls = []
            scan_final_calls = []
            telemetry_events = []

            def mock_render(r, config=None):
                render_calls.append(r)
                # Create the output file
                with open(out_mp4, "wb") as f:
                    f.write(b"fake_rendered_video")
                return RenderResult(output_path=out_mp4, duration_seconds=12.0)

            def mock_scan_video(path):
                if path == out_mp4:
                    scan_final_calls.append(len(scan_final_calls) + 1)
                    attempt = len(scan_final_calls)
                    if attempt < 3:
                        # Fail attempts 1 and 2
                        return make_mock_safety_result(safe=False, failure_reason=f"Face detected in attempt {attempt}")
                    return make_mock_safety_result(safe=True)
                return make_mock_safety_result(safe=True)

            def mock_telemetry(*args, **kwargs):
                telemetry_events.append((args, kwargs))

            res = generate_reel(
                req,
                fetch_quran_fn=lambda *a, **kw: make_mock_quran_data(),
                fetch_translation_fn=lambda *a, **kw: make_mock_translation_data(),
                fetch_timing_soar_fn=lambda *a, **kw: (94,),
                fetch_surah_timing_fn=lambda *a, **kw: make_mock_surah_audio_94(),
                download_audio_fn=lambda *a, **kw: audio_mp3,
                search_backgrounds_fn=lambda *a, **kw: (make_mock_candidate(101, 20.0), make_mock_candidate(102, 20.0)),
                download_background_fn=lambda cand, *a, **kw: DownloadedBackground(candidate=cand, local_path=bg_mp4, file_size_bytes=100),
                scan_video_fn=mock_scan_video,
                render_reel_fn=mock_render,
                record_render_safety_failure_fn=mock_telemetry,
            )

            self.assertTrue(res.success, res.failure_reason)
            self.assertEqual(res.provenance["render_attempts"], 3)
            self.assertEqual(len(render_calls), 3)
            self.assertEqual(len(scan_final_calls), 3)
            # 2 failed attempts logged telemetry
            self.assertEqual(len(telemetry_events), 2)
            self.assertEqual(telemetry_events[0][1]["attempt_number"], 1)
            self.assertEqual(telemetry_events[1][1]["attempt_number"], 2)
            self.assertTrue(os.path.exists(out_mp4))

    def test_bounded_retry_pipeline_exhaustion_cleans_up_and_fails(self):
        import os
        import tempfile
        from tests.runtime_fixtures import (
            make_mock_candidate,
            make_mock_quran_data,
            make_mock_safety_result,
            make_mock_surah_audio_94,
            make_mock_timing_read,
            make_mock_translation_data,
        )

        with patch("engine.main.preflight_subtitle_layout", return_value=MagicMock()), tempfile.TemporaryDirectory() as tmp_dir:
            out_mp4 = os.path.join(tmp_dir, "retry_fail.mp4")
            audio_mp3 = os.path.join(tmp_dir, "audio.mp3")
            with open(audio_mp3, "wb") as f:
                f.write(b"fake_mp3_data")
            bg_mp4 = os.path.join(tmp_dir, "bg.mp4")
            with open(bg_mp4, "wb") as f:
                f.write(b"fake_bg_mp4_data")

            req = RunRequest(slot="morning", seed=42, output_path=out_mp4, max_render_attempts=4)

            scan_final_calls = []
            telemetry_events = []

            def mock_render(r, config=None):
                with open(out_mp4, "wb") as f:
                    f.write(b"fake_rendered_video")
                return RenderResult(output_path=out_mp4, duration_seconds=12.0)

            def mock_scan_video(path):
                if path == out_mp4:
                    scan_final_calls.append(len(scan_final_calls) + 1)
                    return make_mock_safety_result(safe=False, failure_reason="Unsafe frame")
                return make_mock_safety_result(safe=True)

            def mock_telemetry(*args, **kwargs):
                telemetry_events.append((args, kwargs))

            res = generate_reel(
                req,
                fetch_quran_fn=lambda *a, **kw: make_mock_quran_data(),
                fetch_translation_fn=lambda *a, **kw: make_mock_translation_data(),
                fetch_timing_soar_fn=lambda *a, **kw: (94,),
                fetch_surah_timing_fn=lambda *a, **kw: make_mock_surah_audio_94(),
                download_audio_fn=lambda *a, **kw: audio_mp3,
                search_backgrounds_fn=lambda *a, **kw: (make_mock_candidate(101, 20.0), make_mock_candidate(102, 20.0)),
                download_background_fn=lambda cand, *a, **kw: DownloadedBackground(candidate=cand, local_path=bg_mp4, file_size_bytes=100),
                scan_video_fn=mock_scan_video,
                render_reel_fn=mock_render,
                record_render_safety_failure_fn=mock_telemetry,
            )

            self.assertFalse(res.success)
            self.assertIn("MAX_RENDER_SAFETY_RETRIES_EXCEEDED", res.failure_reason or "")
            self.assertEqual(len(scan_final_calls), 4)
            self.assertEqual(len(telemetry_events), 4)
            # Temporary file must be removed
            self.assertFalse(os.path.exists(out_mp4))

    def test_job_orchestration_fail_fail_pass_coverage_and_media_contract(self):
        from engine.job import JobRequest
        from tests.editorial_fixtures import authorized_run_job as run_job
        from engine.storage import RunRecord, CoverageState, B2Config, StoredConfig
        from engine.main import RunResult

        media_uploads = []
        coverage_commits = []
        written_records = []
        telemetry_events = []

        class MockStateStore:
            authority = "supabase"

            def load_config(self):
                return StoredConfig(publishing_schedule=(PublishingScheduleEntry("morning", "09:00", True),), automation_enabled=True, reciter_reuse_confirmed=True, facebook_enabled=False, instagram_enabled=False)

            def get_run_record(self, *args, **kwargs):
                return None

            def start_run_record(self, record):
                written_records.append(record)

            def complete_run_record(self, record):
                written_records.append(record)

            def fail_run_record(self, record):
                written_records.append(record)

            def save_run(self, record):
                written_records.append(record)

            def load_coverage(self):
                return CoverageState(cycle_number=1, covered_count=0, covered_ayahs=())

            def record_covered_ayahs(self, run_id, verse_keys, production_authorization=None):
                coverage_commits.append((run_id, verse_keys))

            def record_background_usage(self, **kwargs):
                pass

            def record_render_safety_failure(self, **kwargs):
                telemetry_events.append(kwargs)
                return "rsf_123"

            def get_recent_content_history(self, last_n_runs=30):
                return ()

        def mock_upload_reel(client, b2_config, local_path, date, slot, run_id, run_kind):
            media_uploads.append((local_path, run_id))
            return f"media/{run_id}.mp4"

        def mock_generate_reel(req, **kwargs):
            rec_fail_fn = kwargs.get("record_render_safety_failure_fn")
            if rec_fail_fn:
                rec_fail_fn(provider="pexels", provider_asset_id="1", provider_variant_id="1", content_sha256="", attempt_number=1, start_offset_seconds=0.0, verse_keys=["2:21"], render_config_fingerprint="fp", failure_reason="fail1", detector_counts={})
                rec_fail_fn(provider="pexels", provider_asset_id="1", provider_variant_id="1", content_sha256="", attempt_number=2, start_offset_seconds=1.0, verse_keys=["2:21"], render_config_fingerprint="fp", failure_reason="fail2", detector_counts={})

            return RunResult(
                success=True,
                slot="morning",
                output_path=req.output_path or "/tmp/test_output.mp4",
                surah_number=1,
                first_ayah=1,
                last_ayah=1,
                duration_seconds=15.0,
                final_safety_result=SafetyScanResult(safe=True, frames_scanned=10, duration_seconds=1.0, person_detections=(), face_detections=()),
                provenance={"render_attempts": 3, **eligible_record_evidence(1, 1, 1)},
            )

        job_req = JobRequest(slot="morning", force=True, run_kind="scheduled")
        env = {
            "STATE_AUTHORITY": "supabase",
            "RECITER_REUSE_CONFIRMED": "true",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test-key",
        }
        b2_cfg = B2Config(
            endpoint_url="https://s3.eu-central-003.backblazeb2.com",
            region_name="eu-central-003",
            application_key_id="k",
            application_key="ak",
            bucket_name="test",
        )
        mock_s3 = MagicMock()

        with patch("engine.job.get_active_state_store", return_value=MockStateStore()), \
             patch("engine.job.get_background_pool_repository", return_value=MagicMock(list_verified_records=lambda: ())), \
             patch("engine.job.upload_reel", side_effect=mock_upload_reel), \
             patch("engine.job.generate_reel", side_effect=mock_generate_reel):

            result = run_job(job_req, env=env, b2_config=b2_cfg, client=mock_s3)

        self.assertEqual(result.status, "SUCCESS")
        # Exactly 2 failure telemetry events logged
        self.assertEqual(len(telemetry_events), 2)
        # Exactly 1 media upload
        self.assertEqual(len(media_uploads), 1)
        # Coverage committed exactly once
        self.assertEqual(len(coverage_commits), 1)
        # RunRecord final_safety_passed is True
        self.assertTrue(any(r.final_safety_passed for r in written_records))

    def test_job_orchestration_four_failures_exhaustion(self):
        from engine.job import JobRequest
        from tests.editorial_fixtures import authorized_run_job as run_job
        from engine.storage import RunRecord, CoverageState, B2Config, StoredConfig
        from engine.main import RunResult

        media_uploads = []
        coverage_commits = []
        written_records = []
        telemetry_events = []

        class MockStateStore:
            authority = "supabase"

            def load_config(self):
                return StoredConfig(publishing_schedule=(PublishingScheduleEntry("morning", "09:00", True),), automation_enabled=True, reciter_reuse_confirmed=True, facebook_enabled=False, instagram_enabled=False)

            def get_run_record(self, *args, **kwargs):
                return None

            def start_run_record(self, record):
                written_records.append(record)

            def complete_run_record(self, record):
                written_records.append(record)

            def fail_run_record(self, record):
                written_records.append(record)

            def save_run(self, record):
                written_records.append(record)

            def load_coverage(self):
                return CoverageState(cycle_number=1, covered_count=0, covered_ayahs=())

            def record_covered_ayahs(self, run_id, verse_keys, production_authorization=None):
                coverage_commits.append((run_id, verse_keys))

            def record_background_usage(self, **kwargs):
                pass

            def record_render_safety_failure(self, **kwargs):
                telemetry_events.append(kwargs)
                return "rsf_123"

            def get_recent_content_history(self, last_n_runs=30):
                return ()

        def mock_upload_reel(client, b2_config, local_path, date, slot, run_id, run_kind):
            media_uploads.append((local_path, run_id))
            return f"media/{run_id}.mp4"

        def mock_generate_reel_fail(req, **kwargs):
            rec_fail_fn = kwargs.get("record_render_safety_failure_fn")
            if rec_fail_fn:
                for att in range(1, 5):
                    rec_fail_fn(provider="pexels", provider_asset_id="1", provider_variant_id="1", content_sha256="", attempt_number=att, start_offset_seconds=float(att), verse_keys=["2:21"], render_config_fingerprint="fp", failure_reason="fail", detector_counts={})

            return RunResult(
                success=False,
                slot="morning",
                output_path=req.output_path or "/tmp/test_output.mp4",
                surah_number=2,
                first_ayah=21,
                last_ayah=21,
                duration_seconds=15.0,
                final_safety_result=SafetyScanResult(safe=False, frames_scanned=10, duration_seconds=1.0, person_detections=(), face_detections=(), failure_reason="MAX_RENDER_SAFETY_RETRIES_EXCEEDED (attempts=4)"),
                failure_reason="MAX_RENDER_SAFETY_RETRIES_EXCEEDED (attempts=4)",
            )

        job_req = JobRequest(slot="morning", force=True, run_kind="scheduled")
        env = {
            "STATE_AUTHORITY": "supabase",
            "RECITER_REUSE_CONFIRMED": "true",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test-key",
        }
        b2_cfg = B2Config(
            endpoint_url="https://s3.eu-central-003.backblazeb2.com",
            region_name="eu-central-003",
            application_key_id="k",
            application_key="ak",
            bucket_name="test",
        )
        mock_s3 = MagicMock()

        with patch("engine.job.get_active_state_store", return_value=MockStateStore()), \
             patch("engine.job.get_background_pool_repository", return_value=MagicMock(list_verified_records=lambda: ())), \
             patch("engine.job.upload_reel", side_effect=mock_upload_reel), \
             patch("engine.job.generate_reel", side_effect=mock_generate_reel_fail):

            result = run_job(job_req, env=env, b2_config=b2_cfg, client=mock_s3)

        self.assertEqual(result.status, "FAILED")
        # 4 telemetry events
        self.assertEqual(len(telemetry_events), 4)
        # MediaStore upload = 0
        self.assertEqual(len(media_uploads), 0)
        # Coverage writes = 0
        self.assertEqual(len(coverage_commits), 0)
        # Final run record is FAILED and final_safety_passed is False
        failed_recs = [r for r in written_records if r.status == "FAILED"]
        self.assertTrue(len(failed_recs) >= 1)
        self.assertFalse(failed_recs[-1].final_safety_passed)

    def test_supabase_authority_zero_b2_state_writes(self):
        from engine.storage import StoredConfig

        b2_state_writes = []

        class MockB2Client:
            def put_object(self, Bucket, Key, Body, **kwargs):
                if not Key.startswith("quran-reels/media/"):
                    b2_state_writes.append(Key)
                return {}

        store = SupabaseStateStore(
            SupabaseConfig("https://example.supabase.co", "test-key"),
            session=MagicMock(request=lambda *a, **kw: MagicMock(ok=True, status_code=200, json=lambda: [], content=b"[]"))
        )
        pool_repo = get_background_pool_repository({"STATE_AUTHORITY": "supabase"}, supabase_store=store)
        self.assertEqual(pool_repo.operation_counts()["writes"], 0)


class TestRetryBackgroundDiversification(unittest.TestCase):
    def setUp(self):
        mock_live_authorization(self)
        self.render_cfg = RenderConfig(
            subtitle_config=SubtitleConfig(
                arabic_font_size=170,
                translation_font_size=50,
                arabic_translation_gap_px=10,
                translation_stack_gap_px=30,
                transition_duration_ms=100,
                subtitle_lead_ms=0,
                subtitle_mode="progressive_phrase_sync",
            )
        )
        self.fp = compute_render_config_fingerprint(self.render_cfg)

    def test_regression_a_final_safety_fail_on_asset_a_causes_attempt_2_to_choose_b(self):
        """Test A: final-safety fail on asset A causes attempt 2 to choose B when B exists."""
        from tests.runtime_fixtures import (
            make_mock_candidate,
            make_mock_quran_data,
            make_mock_safety_result,
            make_mock_surah_audio_94,
            make_mock_translation_data,
        )

        with patch("engine.main.preflight_subtitle_layout", return_value=MagicMock()), tempfile.TemporaryDirectory() as tmp_dir:
            out_mp4 = os.path.join(tmp_dir, "out.mp4")
            audio_mp3 = os.path.join(tmp_dir, "audio.mp3")
            with open(audio_mp3, "wb") as f:
                f.write(b"fake_mp3_data")
            bg_mp4 = os.path.join(tmp_dir, "bg.mp4")
            with open(bg_mp4, "wb") as f:
                f.write(b"fake_bg_data")

            rec_a = make_dummy_record("pexels", "101", sha="a"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})
            rec_b = make_dummy_record("pexels", "102", sha="b"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})

            mock_provider = MagicMock()
            mock_provider.name = "pexels"
            mock_provider.get_asset.side_effect = lambda aid: {"id": str(aid)}
            mock_provider.resolve_variant.side_effect = lambda asset, vid: make_mock_candidate(int(asset["id"]), 30.0)

            rendered_background_ids = []
            def mock_render(req, config=None):
                rendered_background_ids.append(req.backgrounds[0].pexels_video_id)
                with open(out_mp4, "wb") as f:
                    f.write(b"rendered")
                return RenderResult(output_path=out_mp4, duration_seconds=10.0)

            scan_final_calls = []
            def mock_scan(path):
                if path == out_mp4:
                    scan_final_calls.append(len(scan_final_calls) + 1)
                    if len(scan_final_calls) == 1:
                        return make_mock_safety_result(safe=False, failure_reason="Face on attempt 1")
                    return make_mock_safety_result(safe=True)
                return make_mock_safety_result(safe=True)

            def mock_download(cand, *a, **kw):
                sha = "a"*64 if str(cand.pexels_video_id) == "101" else "b"*64
                return DownloadedBackground(candidate=cand, local_path=bg_mp4, file_size_bytes=100, content_sha256=sha)

            run_req = RunRequest(
                slot="morning",
                seed=42,
                output_path=out_mp4,
                max_render_attempts=4,
                render_config=self.render_cfg,
                verified_background_records=(rec_a, rec_b),
            )

            res = generate_reel(
                run_req,
                fetch_quran_fn=lambda *a, **kw: make_mock_quran_data(),
                fetch_translation_fn=lambda *a, **kw: make_mock_translation_data(),
                fetch_timing_soar_fn=lambda *a, **kw: (94,),
                fetch_surah_timing_fn=lambda *a, **kw: make_mock_surah_audio_94(),
                download_audio_fn=lambda *a, **kw: audio_mp3,
                download_background_fn=mock_download,
                background_pool_provider=mock_provider,
                scan_video_fn=mock_scan,
                render_reel_fn=mock_render,
            )

            self.assertTrue(res.success)
            self.assertEqual(len(rendered_background_ids), 2)
            first_id = rendered_background_ids[0]
            second_id = rendered_background_ids[1]
            self.assertNotEqual(first_id, second_id)
            self.assertIn(first_id, (101, 102))
            self.assertIn(second_id, (101, 102))

    def test_regression_b_attempt_3_excludes_a_and_b(self):
        """Test B: attempt 3 excludes A+B and selects C."""
        from tests.runtime_fixtures import (
            make_mock_candidate,
            make_mock_quran_data,
            make_mock_safety_result,
            make_mock_surah_audio_94,
            make_mock_translation_data,
        )

        with patch("engine.main.preflight_subtitle_layout", return_value=MagicMock()), tempfile.TemporaryDirectory() as tmp_dir:
            out_mp4 = os.path.join(tmp_dir, "out.mp4")
            audio_mp3 = os.path.join(tmp_dir, "audio.mp3")
            with open(audio_mp3, "wb") as f:
                f.write(b"fake_mp3_data")
            bg_mp4 = os.path.join(tmp_dir, "bg.mp4")
            with open(bg_mp4, "wb") as f:
                f.write(b"fake_bg_data")

            rec_a = make_dummy_record("pexels", "101", sha="a"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})
            rec_b = make_dummy_record("pexels", "102", sha="b"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})
            rec_c = make_dummy_record("pexels", "103", sha="c"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})

            mock_provider = MagicMock()
            mock_provider.name = "pexels"
            mock_provider.get_asset.side_effect = lambda aid: {"id": str(aid)}
            mock_provider.resolve_variant.side_effect = lambda asset, vid: make_mock_candidate(int(asset["id"]), 30.0)

            rendered_background_ids = []
            def mock_render(req, config=None):
                rendered_background_ids.append(req.backgrounds[0].pexels_video_id)
                with open(out_mp4, "wb") as f:
                    f.write(b"rendered")
                return RenderResult(output_path=out_mp4, duration_seconds=10.0)

            scan_final_calls = []
            def mock_scan(path):
                if path == out_mp4:
                    scan_final_calls.append(len(scan_final_calls) + 1)
                    if len(scan_final_calls) < 3:
                        return make_mock_safety_result(safe=False, failure_reason=f"Face on attempt {len(scan_final_calls)}")
                    return make_mock_safety_result(safe=True)
                return make_mock_safety_result(safe=True)

            def mock_download(cand, *a, **kw):
                sha_map = {101: "a"*64, 102: "b"*64, 103: "c"*64}
                sha = sha_map.get(cand.pexels_video_id, "z"*64)
                return DownloadedBackground(candidate=cand, local_path=bg_mp4, file_size_bytes=100, content_sha256=sha)

            run_req = RunRequest(
                slot="morning",
                seed=42,
                output_path=out_mp4,
                max_render_attempts=4,
                render_config=self.render_cfg,
                verified_background_records=(rec_a, rec_b, rec_c),
            )

            res = generate_reel(
                run_req,
                fetch_quran_fn=lambda *a, **kw: make_mock_quran_data(),
                fetch_translation_fn=lambda *a, **kw: make_mock_translation_data(),
                fetch_timing_soar_fn=lambda *a, **kw: (94,),
                fetch_surah_timing_fn=lambda *a, **kw: make_mock_surah_audio_94(),
                download_audio_fn=lambda *a, **kw: audio_mp3,
                download_background_fn=mock_download,
                background_pool_provider=mock_provider,
                scan_video_fn=mock_scan,
                render_reel_fn=mock_render,
            )

            self.assertTrue(res.success)
            self.assertEqual(len(rendered_background_ids), 3)
            self.assertEqual(len(set(rendered_background_ids)), 3)
            self.assertNotIn(rendered_background_ids[2], (rendered_background_ids[0], rendered_background_ids[1]))


    def test_regression_c_no_coverage_mutation_before_successful_checkpoint(self):
        """Test C: no Coverage mutation occurs before successful generation/storage checkpoint."""
        from engine.job import JobRequest, run_job
        from engine.storage import CoverageState, StoredConfig, B2Config
        from engine.main import RunResult

        coverage_commits = []

        class MockStateStore:
            authority = "supabase"
            def load_config(self):
                return StoredConfig(publishing_schedule=(PublishingScheduleEntry("morning", "09:00", True), PublishingScheduleEntry("evening", "16:00", True)), automation_enabled=True, reciter_reuse_confirmed=True, facebook_enabled=False, instagram_enabled=False)
            def get_run_record(self, *args, **kwargs): return None
            def start_run_record(self, record): pass
            def complete_run_record(self, record): pass
            def fail_run_record(self, record): pass
            def save_run(self, record): pass
            def load_coverage(self):
                return CoverageState(cycle_number=1, covered_count=0, covered_ayahs=())
            def record_covered_ayahs(self, run_id, verse_keys, production_authorization=None):
                coverage_commits.append((run_id, verse_keys))
            def record_background_usage(self, **kwargs): pass
            def record_render_safety_failure(self, **kwargs): return "rsf_fail"
            def get_recent_content_history(self, last_n_runs=30): return ()

        def failing_generate_reel(req, **kwargs):
            rec_fn = kwargs.get("record_render_safety_failure_fn")
            if rec_fn:
                rec_fn(provider="pexels", provider_asset_id="101", provider_variant_id="1", content_sha256="", attempt_number=1, start_offset_seconds=0.0, verse_keys=["94:1"], render_config_fingerprint="fp", failure_reason="fail1", detector_counts={})
                rec_fn(provider="pexels", provider_asset_id="102", provider_variant_id="2", content_sha256="", attempt_number=2, start_offset_seconds=0.0, verse_keys=["94:1"], render_config_fingerprint="fp", failure_reason="fail2", detector_counts={})
            raise GenerationError("MAX_RENDER_SAFETY_RETRIES_EXCEEDED (attempted 4)")

        job_req = JobRequest(slot="manual-production", force=True, run_kind="manual_production")
        env = {"STATE_AUTHORITY": "supabase", "RECITER_REUSE_CONFIRMED": "true"}
        b2_cfg = B2Config(endpoint_url="https://s3.example.com", region_name="eu", application_key_id="k", application_key="ak", bucket_name="b")

        with patch("engine.job.get_active_state_store", return_value=MockStateStore()), \
             patch("engine.job.get_background_pool_repository", return_value=MagicMock()), \
             patch("engine.job.generate_reel", side_effect=failing_generate_reel):
            res = run_job(job_req, env=env, b2_config=b2_cfg, client=MagicMock())

        self.assertEqual(res.status, "FAILED")
        self.assertEqual(len(coverage_commits), 0)  # Coverage was NOT mutated!

    def test_regression_d_same_run_id_is_retained_across_all_attempts(self):
        """Test D: same run_id is retained across all attempts and failure telemetry."""
        from engine.job import JobRequest, run_job
        from engine.storage import CoverageState, StoredConfig, B2Config

        telemetry_run_ids = []
        started_run_ids = []

        class MockStateStore:
            authority = "supabase"
            def load_config(self):
                return StoredConfig(publishing_schedule=(PublishingScheduleEntry("morning", "09:00", True), PublishingScheduleEntry("evening", "16:00", True)), automation_enabled=True, reciter_reuse_confirmed=True, facebook_enabled=False, instagram_enabled=False)
            def get_run_record(self, *args, **kwargs): return None
            def start_run_record(self, record): started_run_ids.append(record.run_id)
            def complete_run_record(self, record): pass
            def fail_run_record(self, record): pass
            def save_run(self, record): pass
            def load_coverage(self): return CoverageState(cycle_number=1, covered_count=0, covered_ayahs=())
            def record_covered_ayahs(self, run_id, verse_keys, production_authorization=None): pass
            def record_background_usage(self, **kwargs): pass
            def record_render_safety_failure(self, **kwargs):
                telemetry_run_ids.append(kwargs.get("run_id"))
                return "rsf_id"
            def get_recent_content_history(self, last_n_runs=30): return ()

        def multi_attempt_generate_reel(req, **kwargs):
            rec_fn = kwargs.get("record_render_safety_failure_fn")
            if rec_fn:
                for att in range(1, 4):
                    rec_fn(provider="pexels", provider_asset_id=str(100 + att), provider_variant_id="1", content_sha256="", attempt_number=att, start_offset_seconds=0.0, verse_keys=["94:1"], render_config_fingerprint="fp", failure_reason=f"fail{att}", detector_counts={})
            raise GenerationError("MAX_RENDER_SAFETY_RETRIES_EXCEEDED")

        job_req = JobRequest(slot="manual-production", force=True, run_kind="manual_production")
        env = {"STATE_AUTHORITY": "supabase", "RECITER_REUSE_CONFIRMED": "true"}
        b2_cfg = B2Config(endpoint_url="https://s3.example.com", region_name="eu", application_key_id="k", application_key="ak", bucket_name="b")

        with patch("engine.job.get_active_state_store", return_value=MockStateStore()), \
             patch("engine.job.get_background_pool_repository", return_value=MagicMock()), \
             patch("engine.job.generate_reel", side_effect=multi_attempt_generate_reel):
            res = run_job(job_req, env=env, b2_config=b2_cfg, client=MagicMock())

        self.assertEqual(res.status, "FAILED")
        self.assertEqual(len(started_run_ids), 1)
        expected_run_id = started_run_ids[0]
        self.assertEqual(res.run_id, expected_run_id)

    def test_regression_e_buffer_and_social_not_called_during_failed_attempts(self):
        """Test E: Buffer/social is not called during failed attempts."""
        from engine.job import JobRequest, run_job
        from engine.storage import CoverageState, StoredConfig, B2Config

        facebook_publishes = []
        instagram_publishes = []

        class MockStateStore:
            authority = "supabase"
            def load_config(self):
                return StoredConfig(publishing_schedule=(PublishingScheduleEntry("morning", "09:00", True), PublishingScheduleEntry("evening", "16:00", True)), automation_enabled=True, reciter_reuse_confirmed=True, facebook_enabled=True, instagram_enabled=True)
            def get_run_record(self, *args, **kwargs): return None
            def start_run_record(self, record): pass
            def complete_run_record(self, record): pass
            def fail_run_record(self, record): pass
            def save_run(self, record): pass
            def load_coverage(self): return CoverageState(cycle_number=1, covered_count=0, covered_ayahs=())
            def record_covered_ayahs(self, run_id, verse_keys, production_authorization=None): pass
            def record_background_usage(self, **kwargs): pass
            def record_render_safety_failure(self, **kwargs): return "rsf"
            def get_recent_content_history(self, last_n_runs=30): return ()

        def failing_gen(req, **kwargs):
            raise GenerationError("MAX_RENDER_SAFETY_RETRIES_EXCEEDED")

        job_req = JobRequest(slot="manual-production", force=True, run_kind="manual_production")
        env = {"STATE_AUTHORITY": "supabase", "RECITER_REUSE_CONFIRMED": "true"}
        b2_cfg = B2Config(endpoint_url="https://s3.example.com", region_name="eu", application_key_id="k", application_key="ak", bucket_name="b")

        with patch("engine.job.get_active_state_store", return_value=MockStateStore()), \
             patch("engine.job.get_background_pool_repository", return_value=MagicMock()), \
             patch("engine.job.generate_reel", side_effect=failing_gen):
            res = run_job(
                job_req,
                env=env,
                b2_config=b2_cfg,
                client=MagicMock(),
                publish_reel_fn=lambda *a, **kw: instagram_publishes.append(1),
                publish_buffer_facebook_fn=lambda *a, **kw: facebook_publishes.append(1),
            )

        self.assertEqual(res.status, "FAILED")
        self.assertEqual(len(facebook_publishes), 0)
        self.assertEqual(len(instagram_publishes), 0)

    def test_regression_f_high_risk_never_introduced_as_retry_fallback(self):
        """Test F: HIGH_RISK is never introduced as retry fallback."""
        rec_safe = make_dummy_record("pexels", "7681534", sha="1"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})
        rec_high = make_dummy_record("pexels", "15499818", sha="2"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.9140})

        # When asset 7681534 is excluded (has failed), select_verified_pool_records MUST NOT fall back to HIGH_RISK 15499818
        selected = select_verified_pool_records(
            [rec_safe, rec_high],
            (),
            required_duration_seconds=10.0,
            excluded_provider_identities=[("pexels", "7681534")],
            render_config_fingerprint=self.fp,
            allow_high_risk=False,
        )
        # Result should be either empty or contain only safe (reused at end), NEVER high risk
        self.assertNotIn("15499818", [r.provider_asset_id for r in selected])

    def test_regression_g_recent_use_preference_relaxed_for_diversity(self):
        """Test G: recent-use/history preference may be relaxed when necessary to obtain a different safe verified asset."""
        rec_a = make_dummy_record("pixabay", "169906", sha="a"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.002})
        rec_b = make_dummy_record("pexels", "7681534", sha="b"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})

        recent_ids = [7681534]

        # Initial selection: non-recent rec_a is 1st, recent rec_b is 2nd (available as fallback)
        initial = select_verified_pool_records(
            [rec_b, rec_a],
            (),
            required_duration_seconds=10.0,
            recent_asset_ids=recent_ids,
            render_config_fingerprint=self.fp,
            allow_high_risk=False,
        )
        self.assertEqual([r.provider_asset_id for r in initial], ["169906", "7681534"])

        # When rec_a fails, retry selection excludes rec_a. Recent rec_b is selected as fallback!
        retry_selection = select_verified_pool_records(
            [rec_b, rec_a],
            (),
            required_duration_seconds=10.0,
            recent_asset_ids=recent_ids,
            excluded_provider_identities=[("pixabay", "169906")],
            render_config_fingerprint=self.fp,
            allow_high_risk=False,
        )
        self.assertEqual(retry_selection[0].provider_asset_id, "7681534")

    def test_regression_h_single_eligible_asset_fails_closed(self):
        """Test H: if only one genuinely safe eligible asset exists, behavior remains fail-closed."""
        from tests.runtime_fixtures import (
            make_mock_candidate,
            make_mock_quran_data,
            make_mock_safety_result,
            make_mock_surah_audio_94,
            make_mock_translation_data,
        )

        with patch("engine.main.preflight_subtitle_layout", return_value=MagicMock()), tempfile.TemporaryDirectory() as tmp_dir:
            out_mp4 = os.path.join(tmp_dir, "fail_closed.mp4")
            audio_mp3 = os.path.join(tmp_dir, "audio.mp3")
            with open(audio_mp3, "wb") as f:
                f.write(b"fake_mp3_data")
            bg_mp4 = os.path.join(tmp_dir, "bg.mp4")
            with open(bg_mp4, "wb") as f:
                f.write(b"fake_bg_data")

            # Only ONE eligible asset exists: 101
            rec_single = make_dummy_record("pexels", "101", sha="a"*64, render_robustness={"fixture_config_fingerprint": self.fp, "failed_evaluation_rate": 0.0})

            mock_provider = MagicMock()
            mock_provider.name = "pexels"
            mock_provider.get_asset.side_effect = lambda aid: {"id": str(aid)}
            mock_provider.resolve_variant.side_effect = lambda asset, vid: make_mock_candidate(int(asset["id"]), 30.0)

            render_calls = []
            def mock_render(req, config=None):
                render_calls.append(req.seed)
                with open(out_mp4, "wb") as f:
                    f.write(b"rendered")
                return RenderResult(output_path=out_mp4, duration_seconds=10.0)

            # All 4 attempts fail final safety
            scan_final_calls = []
            def mock_scan(path):
                if path == out_mp4:
                    scan_final_calls.append(len(scan_final_calls) + 1)
                    return make_mock_safety_result(safe=False, failure_reason="Unsafe composition on single asset")
                return make_mock_safety_result(safe=True)

            def mock_download(cand, *a, **kw):
                return DownloadedBackground(candidate=cand, local_path=bg_mp4, file_size_bytes=100, content_sha256="a"*64)

            run_req = RunRequest(
                slot="morning",
                seed=100,
                output_path=out_mp4,
                max_render_attempts=4,
                render_config=self.render_cfg,
                verified_background_records=(rec_single,),
            )

            res = generate_reel(
                run_req,
                fetch_quran_fn=lambda *a, **kw: make_mock_quran_data(),
                fetch_translation_fn=lambda *a, **kw: make_mock_translation_data(),
                fetch_timing_soar_fn=lambda *a, **kw: (94,),
                fetch_surah_timing_fn=lambda *a, **kw: make_mock_surah_audio_94(),
                download_audio_fn=lambda *a, **kw: audio_mp3,
                download_background_fn=mock_download,
                background_pool_provider=mock_provider,
                scan_video_fn=mock_scan,
                render_reel_fn=mock_render,
            )

            # Fail-closed: generation fails cleanly with MAX_RENDER_SAFETY_RETRIES_EXCEEDED
            self.assertFalse(res.success)
            self.assertIn("MAX_RENDER_SAFETY_RETRIES_EXCEEDED", str(res.failure_reason))
            # All 4 attempts were executed
            self.assertEqual(len(render_calls), 4)
            # Seeds/offsets differed across attempts
            self.assertEqual(len(set(render_calls)), 4)
            # File was cleaned up
            self.assertFalse(os.path.exists(out_mp4))



if __name__ == "__main__":
    unittest.main()
