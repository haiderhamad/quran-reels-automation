"""Buffer-only production transport: no direct Meta posting from real workers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from engine.job import JobRequest, run_job


class BufferOnlyPublishingTests(TestCase):
    def test_direct_meta_backend_fails_before_storage_or_external_io(self):
        jobs = (
            JobRequest(slot="morning", run_kind="scheduled", schedule_slot_id="morning"),
            JobRequest(slot="manual-production", run_kind="manual_production"),
        )
        for request in jobs:
            with self.subTest(run_kind=request.run_kind):
                with (
                    patch("engine.job.create_b2_client") as b2,
                    patch("engine.job.get_active_state_store") as store,
                    patch("engine.job.publish_reel") as direct_meta,
                    patch("engine.job.generate_reel") as generate,
                ):
                    outcome = run_job(
                        request,
                        env={"PUBLISHER_BACKEND": "meta"},
                        now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
                    )
                    self.assertEqual(outcome.status, "FAILED")
                    self.assertTrue(outcome.publishing_blocked)
                    self.assertIn(
                        "DIRECT_META_PUBLISHING_DISABLED_USE_BUFFER",
                        outcome.failure_reason or "",
                    )
                    self.assertEqual(outcome.facebook_status, "FAILED")
                    self.assertEqual(outcome.instagram_status, "FAILED")
                    b2.assert_not_called()
                    store.assert_not_called()
                    generate.assert_not_called()
                    direct_meta.assert_not_called()

    def test_buffer_backend_is_not_forced_to_meta(self):
        from engine.buffer_publisher import (
            buffer_facebook_enabled, buffer_instagram_enabled,
        )

        source = {
            "PUBLISHER_BACKEND": "buffer",
            "BUFFER_PUBLISHING_ENABLED": "true",
            "BUFFER_FACEBOOK_ENABLED": "true",
            "BUFFER_INSTAGRAM_ENABLED": "true",
        }
        self.assertTrue(buffer_facebook_enabled(source))
        self.assertTrue(buffer_instagram_enabled(source))
        self.assertFalse(buffer_facebook_enabled({**source, "PUBLISHER_BACKEND": "meta"}))
        self.assertFalse(buffer_instagram_enabled({**source, "PUBLISHER_BACKEND": "meta"}))
