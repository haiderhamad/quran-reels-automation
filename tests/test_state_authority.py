"""Unit and integration tests for state authority selection, Supabase state store, and separation of media storage."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from engine.state_authority import (
    STATE_AUTHORITY_B2,
    STATE_AUTHORITY_SUPABASE,
    B2StateStore,
    MediaStore,
    get_active_state_store,
    get_media_store,
    get_state_authority,
)
from engine.storage import (
    TOTAL_CANONICAL_AYAHS,
    B2Config,
    CoverageState,
    RunRecord,
    StorageError,
    StoredConfig,
)
from engine.supabase_state import (
    SupabaseConfig,
    SupabaseStateError,
    SupabaseStateStore,
)


class TestStateAuthoritySelection(unittest.TestCase):
    def test_default_state_authority_is_b2(self) -> None:
        self.assertEqual(get_state_authority({}), STATE_AUTHORITY_B2)
        self.assertEqual(get_state_authority({"STATE_AUTHORITY": ""}), STATE_AUTHORITY_B2)
        self.assertEqual(get_state_authority({"STATE_AUTHORITY": "   "}), STATE_AUTHORITY_B2)

    def test_supabase_state_authority_selection(self) -> None:
        self.assertEqual(get_state_authority({"STATE_AUTHORITY": "supabase"}), STATE_AUTHORITY_SUPABASE)
        self.assertEqual(get_state_authority({"STATE_AUTHORITY": " SUPABASE "}), STATE_AUTHORITY_SUPABASE)

    def test_b2_state_authority_selection(self) -> None:
        self.assertEqual(get_state_authority({"STATE_AUTHORITY": "b2"}), STATE_AUTHORITY_B2)
        self.assertEqual(get_state_authority({"STATE_AUTHORITY": " B2 "}), STATE_AUTHORITY_B2)

    def test_invalid_state_authority_fails_closed(self) -> None:
        with self.assertRaises(StorageError):
            get_state_authority({"STATE_AUTHORITY": "redis"})

        with self.assertRaises(StorageError):
            get_state_authority({"STATE_AUTHORITY": "postgres"})

        with self.assertRaises(StorageError):
            get_state_authority({"STATE_AUTHORITY": "true"})


class MockResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json_data = json_data
        self.content = b"{}" if json_data is not None else b""

    def json(self) -> Any:
        return self._json_data


class TestSupabaseStateStoreOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SupabaseConfig(
            url="https://test-project.supabase.co",
            service_role_key="test-service-key",
        )
        self.mock_session = MagicMock()
        self.store = SupabaseStateStore(self.config, session=self.mock_session)

    def test_load_coverage_empty_table_returns_default(self) -> None:
        self.mock_session.request.return_value = MockResponse(200, [])
        cov = self.store.load_coverage()
        self.assertEqual(cov.cycle_number, 1)
        self.assertEqual(cov.total_ayahs, TOTAL_CANONICAL_AYAHS)
        self.assertEqual(cov.covered_count, 0)
        self.assertEqual(len(cov.covered_ayahs), 0)

    def test_load_coverage_existing_row(self) -> None:
        self.mock_session.request.return_value = MockResponse(
            200,
            [
                {
                    "value": {
                        "cycle_number": 2,
                        "total_ayahs": 6236,
                        "covered_count": 5,
                        "covered_ayahs": ["1:1", "1:2", "1:3", "1:4", "1:5"],
                        "processed_run_ids": ["run_1"],
                        "started_at": "2026-08-28T00:00:00Z",
                    }
                }
            ],
        )
        cov = self.store.load_coverage()
        self.assertEqual(cov.cycle_number, 2)
        self.assertEqual(cov.covered_count, 5)
        self.assertEqual(cov.covered_ayahs, ("1:1", "1:2", "1:3", "1:4", "1:5"))

    def test_record_covered_ayahs_idempotent(self) -> None:
        self.mock_session.request.return_value = MockResponse(
            200,
            [
                {
                    "value": {
                        "cycle_number": 1,
                        "total_ayahs": 6236,
                        "covered_count": 3,
                        "covered_ayahs": ["1:1", "1:2", "1:3"],
                        "processed_run_ids": ["run_test_1"],
                    }
                }
            ],
        )
        # Re-running the same run_id should be a no-op
        from tests.editorial_fixtures import legacy_authorization_catalog
        res = self.store.record_covered_ayahs(
            "run_test_1", ["1:1", "1:2", "1:3"], production_authorization=legacy_authorization_catalog()
        )
        self.assertEqual(res.covered_count, 3)

    def test_scheduler_claim_success_and_conflict(self) -> None:
        # Successful claim
        self.mock_session.request.return_value = MockResponse(201, [])
        success = self.store.claim_scheduler_slot("2026-08-29", "morning")
        self.assertTrue(success)

        # Duplicate/conflicting claim returns 409
        self.mock_session.request.return_value = MockResponse(409)
        conflict = self.store.claim_scheduler_slot("2026-08-29", "morning")
        self.assertFalse(conflict)

    def test_run_record_lifecycle(self) -> None:
        rec = RunRecord(
            run_id="run_test_lifecycle",
            date="2026-08-29",
            slot="test",
            status="STARTED",
            run_kind="test",
            started_at="2026-08-29T00:00:00Z",
        )
        self.mock_session.request.return_value = MockResponse(200, [])
        self.store.start_run_record(rec)
        self.mock_session.request.assert_called()

    def test_meta_credentials_empty_handling(self) -> None:
        self.mock_session.request.return_value = MockResponse(200, [])
        env = self.store.load_encrypted_meta_credentials()
        self.assertIsNone(env)

    def test_background_pool_stats_calculation(self) -> None:
        # Mock background_assets response
        self.mock_session.request.side_effect = [
            MockResponse(200, [{"provider": "pexels"}, {"provider": "pexels"}, {"provider": "pixabay"}]),
            MockResponse(200, [{"event_type": "safety_rejected"}, {"event_type": "metadata_ineligible"}]),
        ]
        stats = self.store.get_background_pool_stats()
        self.assertEqual(stats["verified_count"], 3)
        self.assertEqual(stats["pexels_verified"], 2)
        self.assertEqual(stats["pixabay_verified"], 1)
        self.assertEqual(stats["safety_rejected_count"], 1)
        self.assertEqual(stats["metadata_ineligible_count"], 1)


class TestNoB2StateCallsInSupabaseMode(unittest.TestCase):
    def test_supabase_mode_does_not_call_b2_state(self) -> None:
        env = {
            "STATE_AUTHORITY": "supabase",
            "SUPABASE_URL": "https://test-project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test-key",
            "B2_ENDPOINT": "https://s3.us-east-005.backblazeb2.com",
            "B2_REGION": "us-east-005",
            "B2_APPLICATION_KEY_ID": "test-key-id",
            "B2_APPLICATION_KEY": "test-key",
            "B2_BUCKET_NAME": "test-bucket",
        }
        mock_b2_client = MagicMock()
        mock_session = MagicMock()
        mock_session.request.return_value = MockResponse(200, [{"value": {"schema_version": 1, "timezone": "Europe/Berlin"}}])

        store = get_active_state_store(env, supabase_session=mock_session, b2_client=mock_b2_client)
        self.assertIsInstance(store, SupabaseStateStore)

        # Load config
        cfg = store.load_config()
        self.assertIsNotNone(cfg)

        # Verify B2 client was NEVER called for state
        mock_b2_client.get_object.assert_not_called()
        mock_b2_client.list_objects_v2.assert_not_called()
        mock_b2_client.put_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
