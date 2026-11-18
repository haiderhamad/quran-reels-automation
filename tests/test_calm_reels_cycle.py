"""No-network first-ayah ordering and current-cycle PostgREST reader tests."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from engine.calm_reels_cycle import (
    FreshCycleSnapshot, FreshCycleError, owner_ordered_keys,
    choose_next_fresh_passage, preview_next_fresh_passage_from_store,
    choose_next_owner_ayah, preview_next_owner_ayah_from_store,
)
from engine.production_authorization import ProductionAuthorizationCatalog
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from tests.test_production_authorization import authorized_catalog

STATE={
    "catalog_id":"calm-reels-editorial-shortlist-v1",
    "catalog_sha256":"df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d",
    "total_ayahs":2211,
    "cycle_number":1,
    "started_at":"2026-10-28T21:16:47+00:00",
}

def receipt(key:str,**changes)->dict:
    d={
        "cycle_number":1,
        "ayah_key":key,
        "platform":"facebook",
        "run_id":"live_run",
        "media_key":"safe_final_reel.mp4",
        "external_post_id":"post_001",
        "evidence_sha256":"f"*64,
        "verified_at":"2026-10-29T12:30:00Z",
    }
    d.update(changes)
    return d

class FreshOwnerSequenceTests(TestCase):
    def test_first_verse_1_1_and_never_historic_generated_coverage(self):
        keys=owner_ordered_keys()
        self.assertEqual(len(keys),2211)
        self.assertEqual(keys[:3],("1:1","1:3","1:4"))
        zero=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        self.assertEqual(zero.confirmed_count,0)
        self.assertEqual(zero.next_ayah_key,"1:1")
        first=authorized_catalog(surah=1,first=1,last=1)
        target=choose_next_fresh_passage(zero,first)
        self.assertEqual((target.state,target.next_ayah_key),("READY_FOR_CONTEXT_PREFLIGHT","1:1"))
        self.assertEqual((target.passage.surah_number,target.passage.first_ayah,target.passage.last_ayah),(1,1,1))

    def test_post_confirmation_moves_to_next_owner_ayah_only(self):
        snap=FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("1:1")])
        self.assertEqual(snap.confirmed_count,1)
        self.assertEqual(snap.next_ayah_key,"1:3")
        next_approval=authorized_catalog(surah=1,first=3,last=3)
        plan=choose_next_fresh_passage(snap,next_approval)
        self.assertEqual(plan.next_ayah_key,"1:3")
        self.assertEqual(plan.state,"READY_FOR_CONTEXT_PREFLIGHT")

    def test_stop_at_first_unapproved_ayah_never_skip(self):
        zero=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        other=authorized_catalog(surah=39,first=53,last=53)
        plan=choose_next_fresh_passage(zero,other)
        self.assertEqual(plan.state,"BLOCKED_NOT_AUTHORIZED_FOR_EXACT_PASSAGE")
        self.assertEqual(plan.next_ayah_key,"1:1")
        self.assertIsNone(plan.passage)

    def test_never_slice_approved_passage_across_excluded_ayah(self):
        zero=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        three=authorized_catalog(surah=1,first=1,last=3,candidates=["1:1"])
        plan=choose_next_fresh_passage(zero,three)
        self.assertEqual(plan.state,"BLOCKED_NOT_AUTHORIZED_FOR_EXACT_PASSAGE")
        self.assertIsNone(plan.passage)

    def test_approved_exact_single_verse_wins_over_larger_passage(self):
        zero=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        single=authorized_catalog(surah=1,first=1,last=1)
        triple=authorized_catalog(surah=1,first=1,last=3,candidates=["1:1"])
        merged=ProductionAuthorizationCatalog(
            policy_version="calm-inviting-v1",
            passages=(single.passages[0],triple.passages[0]),
        )
        plan=choose_next_fresh_passage(zero,merged)
        self.assertEqual((plan.passage.first_ayah,plan.passage.last_ayah),(1,1))

    def test_validates_epoch_identity_and_refuses_historical_or_fake_post(self):
        with self.assertRaisesRegex(FreshCycleError,"STATE_INVALID"):
            FreshCycleSnapshot.from_supabase_rows({**STATE,"catalog_sha256":"a"*64},[])
        with self.assertRaisesRegex(FreshCycleError,"STATE_INVALID"):
            FreshCycleSnapshot.from_supabase_rows({**STATE,"cycle_number":True},[])
        with self.assertRaisesRegex(FreshCycleError,"RECEIPT_INVALID"):
            FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("4:138")])
        with self.assertRaisesRegex(FreshCycleError,"RECEIPT_INVALID"):
            FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("1:1"),receipt("1:1")])
        with self.assertRaisesRegex(FreshCycleError,"PREVIOUS_EPOCH"):
            FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("1:1",verified_at="2026-10-15T08:00:00Z")])
        with self.assertRaisesRegex(FreshCycleError,"RECEIPT_INVALID"):
            FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("1:1",platform="instagram")])
        with self.assertRaisesRegex(FreshCycleError,"RECEIPT_INVALID"):
            FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("1:1",evidence_sha256="bogus")])

    def test_complete_means_wait_for_atomic_rollover_not_spontaneous_cycle(self):
        receipts=[]
        for i,key in enumerate(owner_ordered_keys()):
            receipts.append(receipt(key,external_post_id=f"fb_post_{i}"))
        result=FreshCycleSnapshot.from_supabase_rows(STATE,receipts)
        self.assertEqual(result.confirmed_count,2211)
        self.assertIsNone(result.next_ayah_key)
        target=choose_next_fresh_passage(result,authorized_catalog(surah=1,first=1,last=1))
        self.assertEqual(target.state,"COMPLETE_PENDING_VERIFIED_ROLLOVER")
        self.assertEqual(target.passage,None)

class SupabaseFreshCycleReadOnlyTests(TestCase):
    def test_owner_selection_without_retired_topic_grants(self):
        zero=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        first=choose_next_owner_ayah(zero)
        self.assertEqual(
            (first.state,first.next_ayah_key,first.confirmed_count,first.cycle_number),
            ("READY_FOR_TECHNICAL_PREFLIGHT","1:1",0,1),
        )
        after=FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("1:1")])
        second=choose_next_owner_ayah(after)
        self.assertEqual(
            (second.state,second.next_ayah_key,second.confirmed_count),
            ("READY_FOR_TECHNICAL_PREFLIGHT","1:3",1),
        )
        self.assertNotEqual(first.state, "AUTHORIZED_TO_PUBLISH")

    def test_owner_order_never_skips_first_pending_on_out_of_order_receipts(self):
        unrelated=FreshCycleSnapshot.from_supabase_rows(STATE,[receipt("2:2")])
        result=choose_next_owner_ayah(unrelated)
        self.assertEqual((result.next_ayah_key,result.confirmed_count),("1:1",1))
        from tests.test_owner_reviewed_passage import TEST_REGISTRY
        # A partial receipt in the synthetic reviewed 1:3-1:7 passage
        # must be reconciled, never silently re-selected as a shorter Reel.
        for keys in (("1:3","1:4"),("1:1","1:4")):
            with self.subTest(keys=keys):
                partial=FreshCycleSnapshot.from_supabase_rows(
                    STATE,[receipt(key) for key in keys])
                with self.assertRaisesRegex(FreshCycleError,"PARTIALLY_CONFIRMED"):
                    choose_next_owner_ayah(partial, reviewed_passages=TEST_REGISTRY)

    def test_owner_preview_reads_current_cycle_only_without_old_grant_rpc(self):
        store=MagicMock()
        store.load_calm_reels_v1_fresh_cycle.return_value=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        with patch(
            "engine.production_authorization.load_production_authorization_catalog",
            side_effect=AssertionError("Legacy editorial grant RPC must not run")
        ):
            decision=preview_next_owner_ayah_from_store(store)
        self.assertEqual((decision.state,decision.next_ayah_key),
                         ("READY_FOR_TECHNICAL_PREFLIGHT","1:1"))
        store.load_calm_reels_v1_fresh_cycle.assert_called_once_with()
        store.start_run_record.assert_not_called()
        store.record_covered_ayahs.assert_not_called()
        store.complete_run_record.assert_not_called()

    def test_owner_preview_fail_closed_and_waits_for_verified_rollover(self):
        with self.assertRaisesRegex(FreshCycleError,"STORE_UNAVAILABLE"):
            preview_next_owner_ayah_from_store(object())
        with self.assertRaisesRegex(FreshCycleError,"STATE_INVALID"):
            choose_next_owner_ayah(object())
        with self.assertRaisesRegex(FreshCycleError,"RECEIPT_INVALID"):
            choose_next_owner_ayah(
                FreshCycleSnapshot(1, STATE["started_at"], frozenset({"4:138"}))
            )
        completed=FreshCycleSnapshot.from_supabase_rows(
            STATE,[receipt(key,external_post_id=f"post_{i}") for i,key in enumerate(owner_ordered_keys())]
        )
        decision=choose_next_owner_ayah(completed)
        self.assertEqual(decision.state, "COMPLETE_PENDING_VERIFIED_ROLLOVER")
        self.assertIsNone(decision.next_ayah_key)
        self.assertEqual(decision.confirmed_count,2211)

    def test_server_preview_uses_only_approved_first_exact_passage(self):
        store=MagicMock()
        store.load_calm_reels_v1_fresh_cycle.return_value=FreshCycleSnapshot.from_supabase_rows(STATE,[])
        with patch("engine.production_authorization.load_production_authorization_catalog",
                   return_value=authorized_catalog(surah=1,first=1,last=1)) as loader:
            decision=preview_next_fresh_passage_from_store(store)
        self.assertEqual((decision.state,decision.next_ayah_key),
                         ("READY_FOR_CONTEXT_PREFLIGHT","1:1"))
        loader.assert_called_once_with(store,policy_version="calm-inviting-v1")
        store.load_calm_reels_v1_fresh_cycle.assert_called_once_with()
        store.record_covered_ayahs.assert_not_called()
        store.complete_run_record.assert_not_called()

    def test_preview_rejects_missing_cycle_reader(self):
        with self.assertRaisesRegex(FreshCycleError,"STORE_UNAVAILABLE"):
            preview_next_fresh_passage_from_store(object())

    def test_worker_uses_new_tables_only_and_never_backfills(self):
        session=MagicMock()
        seen=[]
        def reply(method,url,**kwargs):
            seen.append((method,url,kwargs.get("params")))
            if url.endswith("calm_reels_v1_cycle_state"):
                value=[dict(STATE)]
            elif url.endswith("calm_reels_v1_confirmed_posts"):
                value=[]
            else:
                raise AssertionError("Attempted old state or external network read")
            result=SimpleNamespace(ok=True,status_code=200,content=b"[]",json=lambda:value)
            return result
        session.request.side_effect=reply
        store=SupabaseStateStore(SupabaseConfig("https://fake.supabase.co","unit-test-key"),session=session)
        snap=store.load_calm_reels_v1_fresh_cycle()
        self.assertEqual((snap.cycle_number,snap.confirmed_count,snap.next_ayah_key),(1,0,"1:1"))
        self.assertEqual(len(seen),2)
        self.assertTrue(all(action=="GET" for action,*_ in seen))
        self.assertFalse(any(name in url for _,url,_ in seen for name in ("run_records","coverage_state","buffer_publication_claims")))

    def test_missing_state_is_not_a_fake_zero(self):
        session=MagicMock()
        session.request.return_value=SimpleNamespace(ok=True,status_code=200,content=b"[]",json=lambda:[])
        store=SupabaseStateStore(SupabaseConfig("https://fake.supabase.co","unit-test-key"),session=session)
        with self.assertRaisesRegex(FreshCycleError,"UNAVAILABLE"):
            store.load_calm_reels_v1_fresh_cycle()
