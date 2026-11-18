"""Read-only Facebook verification CLI never mutates a post or ledger."""
from __future__ import annotations

from dataclasses import replace
from io import StringIO
import json
import os
from contextlib import redirect_stdout
from unittest import TestCase
from unittest.mock import MagicMock, patch

from engine.facebook_delivery_verification import (
    FacebookVerificationError,
    VerifiedFacebookDelivery,
)
from engine.public_media_delivery import ensure_public_media_delivery
from tests.test_calm_reels_dispatch import valid_record
from scripts.verify_calm_reels_facebook_delivery import (
    ReadOnlyVerificationError, main, verified_read_only_payload,
)


class ReadOnlyFacebookCliTests(TestCase):
    def setUp(self):
        self.record = replace(
            valid_record(),
            buffer_post_id="existing-buffer-post",
            facebook_status="ACCEPTED",
            public_media_delivery_id="existing-video-public-delivery",
        )
        self.store = MagicMock()
        self.store.get_run.return_value = self.record
        self.buffer = MagicMock()
        self.meta = MagicMock()
        self.proof = VerifiedFacebookDelivery(
            cycle_number=1, ayah_key="1:1", run_id=self.record.run_id,
            buffer_post_id="existing-buffer-post",
            facebook_video_id="123456789012",
            buffer_channel_id="my-facebook-channel",
            sent_at="2026-10-30T13:00:00Z",
            verified_at="2026-10-30T13:01:00Z",
            permalink_url="https://www.facebook.com/reel/123456789012",
            evidence_sha256="a" * 64,
        )
        self.origin = "https://public.example.test"

    def test_run_read_only_with_existing_post_proof(self):
        with patch(
            "scripts.verify_calm_reels_facebook_delivery.verify_owner_v1_facebook_delivery",
            return_value=self.proof,
        ) as verify:
            result = verified_read_only_payload(
                self.record.run_id, store=self.store, buffer=self.buffer,
                 facebook_channel_id="my-facebook-channel",
                public_origin=self.origin,
            )
        self.assertEqual(result, {
            "state": "VERIFIED_READ_ONLY",
            "cycle_number": 1,
            "ayah_key": "1:1",
            "proof_sha256": "a" * 64,
            "publication_enabled": False,
            "writes_performed": False,
        })
        verify.assert_called_once()
        self.assertEqual(
            verify.call_args.kwargs["expected_public_media_url"],
            ensure_public_media_delivery(
                self.record, {"PUBLIC_MEDIA_DELIVERY_ORIGIN": self.origin}
            )[1],
        )
        self.store.get_run.assert_called_once_with(self.record.run_id)
        self.store.save_run.assert_not_called()
        self.store.record_calm_reels_v1_verified_facebook_post.assert_not_called()
        self.store.claim_calm_reels_v1_next_ayah.assert_not_called()
        self.buffer.create_owner_v1_facebook_reel.assert_not_called()

    def test_no_existing_record_or_missing_media_is_fail_closed(self):
        self.store.get_run.return_value = None
        with self.assertRaisesRegex(ReadOnlyVerificationError, "RUN_NOT_FOUND"):
            verified_read_only_payload(
                self.record.run_id, store=self.store, buffer=self.buffer,
                 facebook_channel_id="channel",
                public_origin=self.origin,
            )
        self.store.get_run.return_value = replace(
            self.record, public_media_delivery_id=None
        )
        with self.assertRaisesRegex(ReadOnlyVerificationError, "MEDIA_ID_NOT_PERSISTED"):
            verified_read_only_payload(
                self.record.run_id, store=self.store, buffer=self.buffer,
                 facebook_channel_id="channel",
                public_origin=self.origin,
            )

    def test_manual_github_workflow_is_read_only_and_disabled_by_default(self):
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / (
            ".github/workflows/verify-existing-facebook-reel.yml"
        )
        source = path.read_text()
        self.assertIn("workflow_dispatch:", source)
        self.assertNotIn("  schedule:", source)
        self.assertNotIn("  push:", source)
        self.assertIn(
            "vars.QURAN_REELS_V1_READ_ONLY_VERIFICATION_ENABLED == 'true'",
            source,
        )
        self.assertIn("  contents: read", source)
        self.assertIn("actions/setup-python@", source)
        self.assertIn("python3 -m scripts.verify_calm_reels_facebook_delivery", source)
        self.assertNotIn("docker", source.lower())
        self.assertNotIn("kubectl", source.lower())
        self.assertNotIn("create_owner_v1_facebook_reel", source)
        self.assertNotIn("record_calm_reels_v1_verified_facebook_post", source)

    def test_cli_does_not_print_tokens_or_external_video_ids_on_error(self):
        with patch.dict(os.environ, {
            "BUFFER_FACEBOOK_CHANNEL_ID": "channel",
            "PUBLIC_MEDIA_DELIVERY_ORIGIN": self.origin,
        }):
            with patch(
                "scripts.verify_calm_reels_facebook_delivery.verify_owner_v1_facebook_delivery",
                side_effect=FacebookVerificationError("token=secret-example-xyz"),
            ):
                output = StringIO()
                with redirect_stdout(output):
                    exit_code = main(
                        ["--run-id", self.record.run_id],
                        store=self.store, buffer=self.buffer,
                    )
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "UNAVAILABLE")
        self.assertNotIn("secret-example-xyz", output.getvalue())
        self.store.save_run.assert_not_called()
