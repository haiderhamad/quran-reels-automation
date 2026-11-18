"""No-side-effect CLI preview of the frozen owner publication cycle."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock

from engine.calm_reels_cycle import FreshCycleSnapshot
from scripts.preview_calm_reels_v1 import (
    PreviewConfigError, _local_supabase_environment, _safe_error_code,
    main, preview_payload,
)


class OwnerV1PreviewCliTests(TestCase):
    def make_store(self, confirmed=()):
        store = MagicMock()
        store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(
            1, "2026-10-28T21:16:47Z", frozenset(confirmed),
        )
        return store

    def test_reads_first_then_second_pending_with_zero_writes(self):
        for confirmed, key in (((), "1:1"), (("1:1",), "1:3")):
            with self.subTest(confirmed=confirmed):
                store = self.make_store(confirmed)
                buf = StringIO()
                with redirect_stdout(buf):
                    exit_code = main([], store=store)
                self.assertEqual(exit_code, 0)
                payload = json.loads(buf.getvalue())
                self.assertEqual(payload["next_ayah_key"], key)
                self.assertEqual(payload["confirmed"], len(confirmed))
                self.assertFalse(payload["publication_enabled"])
                self.assertFalse(payload["writes_performed"])
                store.load_calm_reels_v1_fresh_cycle.assert_called_once_with()
                store.start_run_record.assert_not_called()
                store.claim_calm_reels_v1_next_ayah.assert_not_called()
                store.save_run.assert_not_called()

    def test_completed_cycle_does_not_restart_or_post(self):
        from engine.calm_reels_cycle import owner_ordered_keys

        store = self.make_store(owner_ordered_keys())
        preview = preview_payload(store)
        self.assertIsNone(preview["next_ayah_key"])
        self.assertEqual(preview["state"], "COMPLETE_PENDING_VERIFIED_ROLLOVER")
        self.assertEqual(preview["confirmed"], 2211)
        store.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_local_env_files_supply_missing_credentials_without_leaking_or_overwriting(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            local = directory / ".env.local"
            local.write_text(
                'SUPABASE_URL="https://local.supabase.co"\n'
                'SUPABASE_SERVICE_ROLE_KEY="dummy-private-value"\n'
                'OTHER_PROVIDER_KEY=not-loaded\n'
            )
            values = _local_supabase_environment(env={}, paths=[local])
            self.assertEqual(values["SUPABASE_URL"], "https://local.supabase.co")
            self.assertEqual(values["SUPABASE_SERVICE_ROLE_KEY"], "dummy-private-value")
            self.assertNotIn("OTHER_PROVIDER_KEY", values)
            process_wins = _local_supabase_environment(
                env={"SUPABASE_URL": "https://existing.supabase.co"},
                paths=[local],
            )
            self.assertEqual(process_wins["SUPABASE_URL"], "https://existing.supabase.co")
            self.assertEqual(process_wins["SUPABASE_SERVICE_ROLE_KEY"], "dummy-private-value")

    def test_missing_or_invalid_local_env_returns_sanitized_codes(self):
        from engine.supabase_state import SupabaseStateError

        with TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaises(PreviewConfigError) as err:
                _local_supabase_environment(env={}, paths=[directory / "absent"])
            self.assertEqual(_safe_error_code(err.exception), "LOCAL_SUPABASE_CONFIG_MISSING")
            with self.assertRaises(PreviewConfigError) as err:
                _local_supabase_environment(
                    env={"SUPABASE_URL": "http://invalid", "SUPABASE_SERVICE_ROLE_KEY": "fake"},
                    paths=[],
                )
            self.assertEqual(_safe_error_code(err.exception), "LOCAL_SUPABASE_URL_INVALID")
        self.assertEqual(
            _safe_error_code(SupabaseStateError("Supabase GET failed with HTTP 401: private details")),
            "SUPABASE_AUTH_FAILED",
        )
        self.assertEqual(
            _safe_error_code(SupabaseStateError("Supabase GET failed with HTTP 404: private details")),
            "SUPABASE_RESOURCE_NOT_FOUND",
        )

    def test_unavailable_errors_do_not_leak_error_details(self):
        marker = "DO_NOT_LOG_THIS_EXAMPLE_MESSAGE"
        store = MagicMock()
        store.load_calm_reels_v1_fresh_cycle.side_effect = RuntimeError(marker)
        buf = StringIO()
        with redirect_stdout(buf):
            exit_code = main([], store=store)
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(buf.getvalue())["state"], "UNAVAILABLE")
        self.assertNotIn(marker, buf.getvalue())
        store.start_run_record.assert_not_called()
