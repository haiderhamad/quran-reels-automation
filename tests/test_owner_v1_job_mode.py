"""Owner v1 production-mode boundary: preview reads only, publishing fails closed."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from engine.calm_reels_cycle import FreshCycleSnapshot, OwnerNextAyahDecision, owner_ordered_keys
from engine.facebook_delivery_verification import FacebookDeliveryPending
from engine.job import JobError, JobRequest, _build_owner_v1_generation_request, run_job
from engine.storage import StoredConfig
from engine.owner_reviewed_passages import ReviewedPassageApproval

FATIHA_APPROVAL = ReviewedPassageApproval("1:3", "1:7", "private-owner-video-review-2026-11-02", "2026-11-02")


class OwnerV1JobModeTests(TestCase):
    @staticmethod
    def fixed_date():
        return datetime(2026, 10, 29, 15, 30, tzinfo=timezone.utc)

    def preview(self, *, cycle=None, authority="supabase", run_kind="scheduled"):
        store = MagicMock()
        store.load_config.return_value = StoredConfig()
        store.load_calm_reels_v1_reviewed_passage_approvals.return_value = (FATIHA_APPROVAL,)
        store.load_calm_reels_v1_fresh_cycle.return_value = (
            cycle if cycle is not None else
            FreshCycleSnapshot(1, "2026-10-28T21:16:47Z", frozenset())
        )
        request = (
            JobRequest(slot="morning")
            if run_kind == "scheduled" else
            JobRequest(slot="manual-production", run_kind="manual_production")
        )
        with (
            patch("engine.job.get_state_authority", return_value=authority),
            patch("engine.job.get_active_state_store", return_value=store),
            patch("engine.job.load_production_authorization_catalog") as grants,
            patch("engine.job.generate_reel") as generate,
            patch("engine.job.publish_reel") as publish,
        ):
            result = run_job(
                request,
                env={"QURAN_REELS_V1_MODE": "preview"},
                b2_config=MagicMock(),
                client=MagicMock(),
                now=self.fixed_date(),
            )
            grants.assert_not_called()
            generate.assert_not_called()
            publish.assert_not_called()
        store.start_run_record.assert_not_called()
        store.claim_calm_reels_v1_next_ayah.assert_not_called()
        store.load_coverage.assert_not_called()
        store.save_run.assert_not_called()
        store.record_covered_ayahs.assert_not_called()
        return result, store

    def test_next_zero_and_then_second_key(self):
        initial, store = self.preview()
        self.assertEqual(initial.status, "SKIPPED")
        self.assertTrue(initial.publishing_blocked)
        self.assertIn("ayah=1:1;", initial.skipped_reason)
        self.assertIn("confirmed=0/2211;", initial.skipped_reason)
        store.load_calm_reels_v1_fresh_cycle.assert_called_once_with()

        after, _ = self.preview(
            cycle=FreshCycleSnapshot(1, "2026-10-28T21:16:47Z", frozenset({"1:1"})),
            run_kind="manual_production",
        )
        self.assertEqual(after.status, "SKIPPED")
        self.assertIn("ayah=1:3;", after.skipped_reason)
        self.assertIn("confirmed=1/2211;", after.skipped_reason)

    def test_prepares_exact_first_ayah_for_existing_generator_without_dispatch(self):
        config = StoredConfig()
        first = OwnerNextAyahDecision("READY_FOR_TECHNICAL_PREFLIGHT", 1, "1:1", 0)
        bound = _build_owner_v1_generation_request(
            JobRequest(slot="manual-production", run_kind="manual_production"),
            config,
            first,
        )
        self.assertEqual(bound.owner_ayah_key, "1:1")
        self.assertEqual(bound.owner_cycle_number, 1)
        self.assertEqual(bound.run_kind, "manual_production")
        self.assertIsNone(bound.production_authorization)
        self.assertIsNone(bound.requested_ayah_range)
        self.assertEqual(bound.translation_languages, tuple(config.translation_languages))

        second = OwnerNextAyahDecision("READY_FOR_TECHNICAL_PREFLIGHT", 1, "1:3", 1)
        changed = _build_owner_v1_generation_request(
            JobRequest(slot="morning", run_kind="scheduled"), config, second,
        )
        self.assertEqual(changed.owner_ayah_key, "1:3")
        self.assertEqual(changed.run_kind, "scheduled")

        for invalid in (
            OwnerNextAyahDecision("COMPLETE_PENDING_VERIFIED_ROLLOVER", 1, None, 2211),
            OwnerNextAyahDecision("READY_FOR_TECHNICAL_PREFLIGHT", 1, None, 0),
        ):
            with self.assertRaisesRegex(JobError, "OWNER_V1_EXACT_TARGET_UNAVAILABLE"):
                _build_owner_v1_generation_request(
                    JobRequest(slot="morning"), config, invalid,
                )

    def test_worker_preview_reaches_exact_request_but_never_generates(self):
        with patch(
            "engine.job._build_owner_v1_generation_request",
            wraps=_build_owner_v1_generation_request,
        ) as build:
            result, store = self.preview()
        self.assertEqual(result.status, "SKIPPED")
        self.assertTrue(result.publishing_blocked)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(build.call_args.args[2].next_ayah_key, "1:1")
        store.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_complete_cycle_waits_for_rollover_without_request_or_dispatch(self):
        complete = FreshCycleSnapshot(
            1, "2026-10-28T21:16:47Z", frozenset(owner_ordered_keys()),
        )
        with patch("engine.job._build_owner_v1_generation_request") as build:
            result, store = self.preview(cycle=complete)
        self.assertEqual(result.status, "SKIPPED")
        self.assertTrue(result.publishing_blocked)
        self.assertIn("COMPLETE_PENDING_VERIFIED_ROLLOVER", result.skipped_reason)
        build.assert_not_called()
        store.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_requires_supabase_authority(self):
        result, store = self.preview(authority="b2")
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(result.publishing_blocked)
        self.assertEqual(result.failure_reason, "CALM_V1_SUPABASE_REQUIRED")
        store.load_calm_reels_v1_fresh_cycle.assert_not_called()

    def test_bad_snapshot_fails_closed_without_any_mutations(self):
        store = MagicMock()
        store.load_config.return_value = StoredConfig()
        store.load_calm_reels_v1_fresh_cycle.side_effect = ValueError("invalid")
        with (
            patch("engine.job.get_state_authority", return_value="supabase"),
            patch("engine.job.get_active_state_store", return_value=store),
            patch("engine.job.load_production_authorization_catalog") as grants,
        ):
            result = run_job(
                JobRequest(slot="morning"),
                env={"QURAN_REELS_V1_MODE": "preview"},
                b2_config=MagicMock(),
                client=MagicMock(),
                now=self.fixed_date(),
            )
            grants.assert_not_called()
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.failure_reason, "CALM_V1_PREVIEW_UNAVAILABLE")
        self.assertTrue(result.publishing_blocked)
        store.start_run_record.assert_not_called()

    def test_unknown_or_publish_modes_fail_before_opening_providers(self):
        for mode in ("publish", "on", "enabled", "false", "nope"):
            with self.subTest(mode=mode):
                with (
                    patch("engine.job.get_active_state_store") as state,
                    patch("engine.job.create_b2_client") as b2,
                    patch("engine.job.load_production_authorization_catalog") as grants,
                ):
                    result = run_job(
                        JobRequest(slot="morning"),
                        env={"QURAN_REELS_V1_MODE": mode},
                        now=self.fixed_date(),
                    )
                    self.assertEqual(result.status, "FAILED")
                    self.assertEqual(
                        result.failure_reason,
                        "OWNER_V1_PUBLISH_NOT_READY"
                        if mode == "publish" else "CALM_V1_PUBLISHING_NOT_IMPLEMENTED",
                    )
                    self.assertTrue(result.publishing_blocked)
                    state.assert_not_called()
                    b2.assert_not_called()
                    grants.assert_not_called()

    def test_publish_without_meta_credentials_still_rejects_wrong_generated_ayah(self):
        store = MagicMock()
        store.load_config.return_value = StoredConfig(
            reciter_reuse_confirmed=True, facebook_enabled=True, instagram_enabled=True,
        )
        store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(
            1, '2026-10-28T21:16:47Z', frozenset(),
        )
        store.pending_owner_dispatch_run.return_value = None
        store.get_run_record.return_value = None
        env = dict.fromkeys(('GITHUB_ACTIONS', 'QURAN_REELS_V1_PUBLISH_ENABLED', 'RECITER_REUSE_CONFIRMED',
                             'BUFFER_PUBLISHING_ENABLED', 'BUFFER_FACEBOOK_ENABLED', 'BUFFER_INSTAGRAM_ENABLED'), 'true')
        env.update(STATE_AUTHORITY='supabase', QURAN_REELS_V1_MODE='publish', PUBLISHER_BACKEND='buffer')
        with (patch('engine.job.get_active_state_store', return_value=store),
              patch('engine.job.generate_reel') as render, patch('engine.job.upload_reel') as upload,
              patch('engine.job.BufferClient') as buffer):
            result = run_job(JobRequest(slot='manual-production', run_kind='manual_production'),
                             env=env, b2_config=MagicMock(), client=MagicMock(), now=self.fixed_date())
        self.assertEqual(result.failure_reason, 'OWNER_V1_GENERATED_AYAH_MISMATCH')
        render.assert_called_once()
        upload.assert_not_called()
        buffer.assert_not_called()
        store.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_reviewed_five_ayah_generation_reaches_upload_gate_without_social_send(self):
        """The real owner-v1 post-render gate must accept the whole reviewed range."""
        from engine.main import RunResult

        keys = ("1:3", "1:4", "1:5", "1:6", "1:7")
        store = MagicMock()
        store.load_config.return_value = StoredConfig(
            reciter_reuse_confirmed=True, facebook_enabled=True, instagram_enabled=True,
        )
        store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(
            1, "2026-11-01T00:00:00Z", frozenset(("1:1",)),
        )
        store.load_calm_reels_v1_reviewed_passages.return_value = (keys,)
        store.load_calm_reels_v1_reviewed_passage_approvals.return_value = (FATIHA_APPROVAL,)
        store.pending_owner_dispatch_run.return_value = None
        store.get_run_record.return_value = None
        pool = MagicMock()
        pool.stale_record_ids.return_value = frozenset()
        pool.list_verified.return_value = ()
        pool.list_usage.return_value = ()
        env = dict.fromkeys((
            "GITHUB_ACTIONS", "QURAN_REELS_V1_PUBLISH_ENABLED",
            "RECITER_REUSE_CONFIRMED", "BUFFER_PUBLISHING_ENABLED",
            "BUFFER_FACEBOOK_ENABLED", "BUFFER_INSTAGRAM_ENABLED",
        ), "true")
        env.update(STATE_AUTHORITY="supabase", QURAN_REELS_V1_MODE="publish",
                   PUBLISHER_BACKEND="buffer")

        def fake_generate(req, **_kwargs):
            self.assertEqual(req.owner_reviewed_ayah_keys, keys)
            self.assertEqual(req.owner_reviewed_registry, (keys,))
            self.assertEqual(req.owner_reviewed_approval, FATIHA_APPROVAL)
            return RunResult(
                success=True, slot=req.slot, surah_number=1,
                first_ayah=3, last_ayah=7, duration_seconds=37.06,
                output_path="/tmp/owner-five-mock.mp4",
                provenance={
                    "owner_v1": {
                        "catalog_id": "calm-reels-editorial-shortlist-v1",
                        "ayah_key": "1:3", "ayah_keys": list(keys),
                        "cycle_number": 1, "editorial_selection_only": True,
                        "review_approval": req.owner_reviewed_approval.snapshot(),
                        "review_approval_sha256": req.owner_reviewed_approval.sha256,
                    },
                    "translation_sources": [],
                },
            )

        with (
            patch("engine.job.get_state_authority", return_value="supabase"),
            patch("engine.job.get_active_state_store", return_value=store),
            patch("engine.job.get_background_pool_repository", return_value=pool),
            patch("engine.job.generate_reel", side_effect=fake_generate),
            patch("engine.job.upload_reel", return_value="media/fatiha.mp4") as upload,
            patch("engine.job.ensure_public_media_delivery",
                  side_effect=RuntimeError("STOP_BEFORE_BUFFER")),
            patch("engine.job.BufferClient") as buffer,
        ):
            result = run_job(
                JobRequest(slot="manual-production", run_kind="manual_production",
                           requested_run_id="owner-v1-reviewed-five"),
                env=env, b2_config=MagicMock(), client=MagicMock(),
                now=self.fixed_date(),
            )
        upload.assert_called_once()
        buffer.assert_not_called()
        store.record_calm_reels_v1_verified_facebook_post.assert_not_called()
        self.assertEqual(result.failure_reason, "OWNER_V1_BUFFER_OUTCOME_UNKNOWN_RECONCILE")

    def test_publishing_mode_uses_exact_first_ayah_not_legacy_coverage(self):
        """Mock all external boundaries; no actual video or Buffer write."""
        from dataclasses import replace
        from engine.main import RunResult

        store = MagicMock()
        store.load_config.return_value = StoredConfig(
            reciter_reuse_confirmed=True,
            facebook_enabled=True,
            instagram_enabled=True,
        )
        store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(
            1, "2026-10-28T21:16:47Z", frozenset(),
        )
        store.get_run_record.return_value = None
        store.pending_owner_dispatch_run.return_value = None
        pool = MagicMock()
        pool.stale_record_ids.return_value = frozenset()
        pool.list_verified.return_value = ()
        pool.list_usage.return_value = ()
        requests = []

        def fake_generate(req, **kwargs):
            requests.append(req)
            return RunResult(
                success=True, slot=req.slot,
                surah_number=1, first_ayah=1, last_ayah=1,
                duration_seconds=16.0, output_path="/tmp/owner-v1-mocked.mp4",
                provenance={
                    "owner_v1": {
                        "catalog_id": "calm-reels-editorial-shortlist-v1",
                        "ayah_key": req.owner_ayah_key,
                        "cycle_number": req.owner_cycle_number,
                        "editorial_selection_only": True,
                    },
                    "translation_sources": [],
                },
            )

        def fake_public_url(record, _env):
            return replace(
                record, public_media_delivery_id="owner-mock-delivery-001",
            ), "https://example.test/owner/mock/video.mp4"

        def fake_send(record, **kwargs):
            return replace(
                record, facebook_status="ACCEPTED",
                instagram_status="ACCEPTED",
                facebook_media_id="buffer-fb", instagram_media_id="buffer-ig",
                buffer_post_id="buffer-fb",
            )

        env = {
            "GITHUB_ACTIONS": "true",
            "STATE_AUTHORITY": "supabase",
            "QURAN_REELS_V1_MODE": "publish",
            "QURAN_REELS_V1_PUBLISH_ENABLED": "true",
            "PUBLISHER_BACKEND": "buffer",
            "BUFFER_PUBLISHING_ENABLED": "true",
            "BUFFER_FACEBOOK_ENABLED": "true",
            "BUFFER_INSTAGRAM_ENABLED": "true",
            "RECITER_REUSE_CONFIRMED": "true",
        }
        with (
            patch("engine.job.get_state_authority", return_value="supabase"),
            patch("engine.job.get_active_state_store", return_value=store),
            patch("engine.job.get_background_pool_repository", return_value=pool),
            patch("engine.job.generate_reel", side_effect=fake_generate),
            patch("engine.job.upload_reel", return_value="media/owner-v1.mp4"),
            patch("engine.job.ensure_public_media_delivery", side_effect=fake_public_url),
            patch("engine.job._record_production_pool_usage"),
            patch("engine.job.load_buffer_config_from_env"),
            patch("engine.job.BufferClient") as buffer_cls,
            patch("engine.job.load_meta_config_from_env", side_effect=AssertionError("No Meta credentials in Buffer-only mode")) as meta_config,
            patch("engine.facebook_delivery_verification.confirm_owner_v1_facebook_delivery",
                  side_effect=FacebookDeliveryPending("pending")) as proof,
            patch("engine.facebook_delivery_verification.sleep") as poll_delay,
            patch("engine.job.load_production_authorization_catalog") as legacy_grants,
        ):
            buffer_cls.return_value.submit_owner_v1_pair.side_effect = fake_send
            result = run_job(
                JobRequest(
                    slot="manual-production", run_kind="manual_production",
                    requested_run_id="owner-mock-first",
                ),
                env=env, b2_config=MagicMock(), client=MagicMock(),
                now=self.fixed_date(),
            )
            legacy_grants.assert_not_called()
            buffer_cls.return_value.submit_owner_v1_pair.assert_called_once()
            self.assertEqual(proof.call_count, 4)
            self.assertEqual(poll_delay.call_count, 3)
            meta_config.assert_not_called()

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual((result.facebook_status, result.instagram_status),
                         ("ACCEPTED", "ACCEPTED"))
        self.assertIn("remains unchanged", result.publishing_warning)
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0].owner_ayah_key, requests[0].owner_cycle_number),
                         ("1:1", 1))
        self.assertEqual(requests[0].covered_ayahs, ())
        self.assertIsNone(requests[0].production_authorization)
        store.load_coverage.assert_not_called()
        store.record_covered_ayahs.assert_not_called()
        store.record_calm_reels_v1_verified_facebook_post.assert_not_called()

    def test_later_manual_run_reconciles_old_claim_without_generating_or_sending(self):
        from dataclasses import replace
        from tests.test_calm_reels_dispatch import valid_record

        store = MagicMock()
        store.load_config.return_value = StoredConfig(reciter_reuse_confirmed=True,
            facebook_enabled=True, instagram_enabled=True)
        store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(
            1, '2026-10-28T21:16:47Z', frozenset())
        prior = replace(valid_record(), facebook_status='ACCEPTED', instagram_status='ACCEPTED',
            buffer_post_id='b-fb', instagram_media_id='b-ig', public_media_delivery_id='fixture-delivery-prior')
        store.get_run.return_value = prior
        store.pending_owner_dispatch_run.return_value = prior.run_id
        env = dict.fromkeys(('GITHUB_ACTIONS', 'QURAN_REELS_V1_PUBLISH_ENABLED', 'RECITER_REUSE_CONFIRMED',
            'BUFFER_PUBLISHING_ENABLED', 'BUFFER_FACEBOOK_ENABLED', 'BUFFER_INSTAGRAM_ENABLED'), 'true')
        env.update(QURAN_REELS_V1_MODE='publish', STATE_AUTHORITY='supabase', PUBLISHER_BACKEND='buffer')
        with (patch('engine.job.get_active_state_store', return_value=store),
              patch('engine.job.generate_reel') as generate, patch('engine.job.load_meta_config_from_env'),
              patch('engine.job.ensure_public_media_delivery', return_value=(prior, 'https://example.test/video.mp4')),
              patch('engine.job.load_buffer_config_from_env'), patch('engine.job.BufferClient') as client,
              patch('engine.facebook_delivery_verification.confirm_after_buffer_acceptance',
                    return_value=(replace(prior, facebook_status='SUCCESS'), None)) as verify):
            def start():
                return run_job(JobRequest(slot='manual-production', run_kind='manual_production', requested_run_id='later'),
                    env=env, b2_config=MagicMock(), client=MagicMock(), now=self.fixed_date())
            result = start()
            self.assertEqual((result.status, result.run_id, result.facebook_status), ('SKIPPED', prior.run_id, 'SUCCESS'))
            self.assertEqual(result.skipped_reason, 'OWNER_V1_RECONCILED_EXISTING_RUN')
            self.assertIs(verify.call_args.args[0], prior)
            # A partial send and a missing claim table must stop before any render or send.
            store.get_run.return_value = replace(prior, instagram_status='UNKNOWN')
            self.assertEqual(start().status, 'FAILED')
            store.pending_owner_dispatch_run.side_effect = RuntimeError('missing table')
            self.assertEqual(start().status, 'FAILED')
            verify.assert_called_once()
            generate.assert_not_called()
            client.return_value.submit_owner_v1_pair.assert_not_called()
            store.start_run_record.assert_not_called()

    def test_test_run_does_not_expose_preview_as_publication(self):
        # The preview is only available for production-mode entry points.
        # Test jobs retain their existing independent non-public test semantics.
        with patch("engine.job.get_active_state_store") as state:
            state.side_effect = RuntimeError("dummy fake: no storage")
            result = run_job(
                JobRequest(slot="test"),
                env={"QURAN_REELS_V1_MODE": "preview"},
                b2_config=MagicMock(),
                client=MagicMock(),
                now=self.fixed_date(),
            )
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("CALM_V1_", result.failure_reason or "")
