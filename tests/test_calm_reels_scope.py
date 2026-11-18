"""Owner catalog gates live passage grants without ever creating a grant."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.calm_reels_scope import (
    CATALOG_SHA256, CATALOG_FILE, CalmReelsScopeError,
    owner_shortlist_keys, restrict_to_owner_shortlist,
)
from engine.production_authorization import (
    ProductionAuthorizationCatalog, ProductionAuthorizationError,
)
from tests.test_production_authorization import authorized_catalog

class OwnerShortlistScopeTests(unittest.TestCase):
    def test_original_owner_list_checked_and_known_sensitive_verses_excluded(self):
        keys=owner_shortlist_keys()
        self.assertEqual(len(keys),2211)
        self.assertIn("39:53",keys)
        self.assertIn("52:31",keys)  # requires contextual review; still no authorization
        self.assertNotIn("4:138",keys)
        self.assertNotIn("2:40",keys)

    def test_scoping_only_reduces_wholly_authorized_passages(self):
        good=authorized_catalog(surah=39,first=53,last=53)
        bad=authorized_catalog(surah=2,first=40,last=40)
        mixed=ProductionAuthorizationCatalog(
            policy_version="calm-inviting-v1",
            passages=(good.passages[0],bad.passages[0]),
        )
        narrowed=restrict_to_owner_shortlist(mixed)
        self.assertEqual(len(narrowed.passages),1)
        self.assertIs(narrowed.passages[0],good.passages[0])
        self.assertEqual(narrowed.authorized_verse_keys,frozenset({"39:53"}))
        self.assertTrue(narrowed.authorized_verse_keys.issubset(mixed.authorized_verse_keys))
        self.assertEqual(narrowed.passage_for(2,40,40),None)
        self.assertTrue(narrowed.passage_for(39,53,53))

    def test_never_split_production_approved_whole_passage(self):
        spanning=authorized_catalog(surah=2,first=39,last=40,candidates=["2:39"])
        with self.assertRaisesRegex(ProductionAuthorizationError,"NO_FULLY_AUTHORIZED_PASSAGES"):
            restrict_to_owner_shortlist(spanning)

    def test_fails_closed_when_only_unwanted_verses_authorized(self):
        bad=authorized_catalog(surah=4,first=138,last=138)
        with self.assertRaisesRegex(ProductionAuthorizationError,"NO_FULLY_AUTHORIZED_PASSAGES"):
            restrict_to_owner_shortlist(bad)
        with self.assertRaisesRegex(ProductionAuthorizationError,"RESPONSE_INVALID"):
            restrict_to_owner_shortlist(object()) # type: ignore[arg-type]

    def test_rejects_tampered_owner_snapshot_even_with_valid_rpc(self):
        good=authorized_catalog(surah=39,first=53,last=53)
        with tempfile.TemporaryDirectory() as td:
            tampered=Path(td)/"catalog.json"
            tampered.write_bytes(CATALOG_FILE.read_bytes()+b" ")
            owner_shortlist_keys.cache_clear()
            try:
                with patch("engine.calm_reels_scope.CATALOG_FILE",tampered):
                    with self.assertRaisesRegex(CalmReelsScopeError,"FROZEN_SCOPE_UNAVAILABLE"):
                        restrict_to_owner_shortlist(good)
            finally:
                owner_shortlist_keys.cache_clear()

class OwnerScopeProductionWorkerTests(unittest.TestCase):
    def test_real_worker_rejects_out_of_scope_grants_before_media_or_posting(self):
        from unittest.mock import MagicMock
        from engine.job import JobRequest, run_job
        from engine.storage import StoredConfig

        store = MagicMock()
        store.load_config.return_value = StoredConfig(reciter_reuse_confirmed=False)
        request = JobRequest(slot="manual-production", run_kind="manual_production")
        with (patch("engine.job.get_state_authority", return_value="supabase"),
              patch("engine.job.get_active_state_store", return_value=store)):
            blocked = run_job(
                request,
                production_authorization=authorized_catalog(
                    surah=4, first=138, last=138,
                ),
                env={"QURAN_REELS_V1_MODE": "legacy"},
                b2_config=MagicMock(), client=MagicMock(),
            )
            permitted_scope = run_job(
                request,
                production_authorization=authorized_catalog(
                    surah=39, first=53, last=53,
                ),
                env={"QURAN_REELS_V1_MODE": "legacy"},
                b2_config=MagicMock(), client=MagicMock(),
            )
        self.assertEqual(blocked.status, "FAILED")
        self.assertTrue(blocked.publishing_blocked)
        self.assertEqual(
            blocked.failure_reason,
            "CALM_REELS_OWNER_SCOPE_OR_EXACT_GRANTS_UNAVAILABLE",
        )
        # Scope filtering does not override independent reciter-rights checks.
        self.assertEqual(permitted_scope.status, "SKIPPED")
        self.assertTrue(permitted_scope.publishing_blocked)
        store.start_run_record.assert_not_called()
        store.claim_calm_reels_v1_next_ayah.assert_not_called()


if __name__=="__main__":
    unittest.main()
