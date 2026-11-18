"""Fail-closed v1 Facebook claim boundary tests, no live media or DB calls."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from engine.alignments import normalized_sha256
from engine.calm_reels_dispatch import (
    OwnerV1DispatchError,
    claim_owner_v1_before_facebook_dispatch,
    require_owner_v1_dispatch_record,
)
from engine.storage import RunRecord
from engine.translation import EXPECTED_TRANSLATION_VERSIONS, SUPPORTED_TRANSLATION_SOURCES


ARABIC = "علامة تجريبية"
TRANSLATION = "Synthetic display label."
KEY = SUPPORTED_TRANSLATION_SOURCES["en"]


def valid_record() -> RunRecord:
    arabic_hash = normalized_sha256(ARABIC)
    en_hash = normalized_sha256(TRANSLATION)
    return RunRecord(
        run_id="owner_run_1", date="2026-10-30", slot="morning",
        run_kind="scheduled", status="SUCCESS",
        started_at="2026-10-30T09:00:00Z",
        completed_at="2026-10-30T09:05:00Z",
        surah_number=1, first_ayah=1, last_ayah=1,
        media_key="media/owner_run_1.mp4", final_safety_passed=True,
        translation_languages=("en",),
        provenance={
            "owner_v1": {
                "catalog_id": "calm-reels-editorial-shortlist-v1",
                "ayah_key": "1:1",
                "cycle_number": 1,
                "source_sha256": hashlib.sha256(ARABIC.encode()).hexdigest(),
                "editorial_selection_only": True,
            },
            "caption_ayahs": [{
                "verse_key": "1:1", "arabic_text": ARABIC,
                "arabic_normalized_sha256": arabic_hash,
                "translations": {"en": {
                    "text": TRANSLATION,
                    "normalized_sha256": en_hash,
                    "translation_key": KEY,
                    "translation_version": EXPECTED_TRANSLATION_VERSIONS[KEY],
                }},
            }],
            "subtitle_ayahs": [{
                "verse_key": "1:1",
                "arabic_source_hash": arabic_hash,
                "english_source_hash": en_hash,
            }],
        },
    )


class ClaimGateTests(TestCase):
    def setUp(self):
        self.record = valid_record()
        self.state = MagicMock()
        self.state.claim_calm_reels_v1_next_ayah.return_value = {
            "state": "CLAIMED", "cycle_number": 1, "ayah_key": "1:1",
        }
        self.stored = SimpleNamespace(reciter_reuse_confirmed=True)
        self.env = {"RECITER_REUSE_CONFIRMED": "true"}
        self.source_patch = patch(
            "engine.calm_reels_dispatch.require_owner_ayah_source",
            return_value=hashlib.sha256(ARABIC.encode()).hexdigest(),
        )
        self.trust_patch = patch(
            "engine.reel_publication_policy._trusted_source_hashes",
            return_value=(normalized_sha256(ARABIC), normalized_sha256(TRANSLATION)),
        )
        self.source_patch.start()
        self.trust_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.addCleanup(self.trust_patch.stop)

    def reserve(self, record=None, **changes):
        return claim_owner_v1_before_facebook_dispatch(
            record if record is not None else self.record,
            store=changes.get("store", self.state),
            stored_config=changes.get("stored_config", self.stored),
            env=changes.get("env", self.env),
        )

    def test_source_verified_first_pending_claim_exact_identity(self):
        result = self.reserve()
        self.assertEqual(result, {"state": "CLAIMED", "ayah_key": "1:1", "cycle_number": 1})
        self.state.claim_calm_reels_v1_next_ayah.assert_called_once_with(
            cycle_number=1, ayah_key="1:1", run_id="owner_run_1",
        )
        self.state.save_run.assert_not_called()
        self.state.record_covered_ayahs.assert_not_called()

    def test_pair_requires_both_source_rows_and_an_exact_database_claim(self):
        p = self.record.provenance
        # Fatiha 1:3–1:7 is now one approved clip; test the legacy pair on
        # another contiguous frozen-catalog range instead.
        keys = ['2:2', '2:3']
        owner = p['owner_v1']
        rows = {name: [dict(p[name][0], verse_key=k) for k in keys]
                for name in ('caption_ayahs', 'subtitle_ayahs')}
        pair = replace(self.record, surah_number=2, first_ayah=2, last_ayah=3, provenance={**p, **rows,
            'owner_v1': dict(owner, ayah_key=keys[0], ayah_keys=keys,
                             source_sha256_by_key=dict.fromkeys(keys, owner['source_sha256']))})
        response = dict(state='CLAIMED', cycle_number=1, ayah_key='2:2', last_ayah_key='2:3')
        self.state.claim_calm_reels_v1_next_ayah.return_value = response
        with patch('engine.reel_publication_policy._trusted_source_hashes',
                   return_value=(normalized_sha256(ARABIC), normalized_sha256(TRANSLATION))) as trust:
            self.assertEqual(self.reserve(pair)['state'], 'CLAIMED')
            self.assertEqual([c.args for c in trust.call_args_list], [(2, 2, 'en'), (2, 3, 'en')])
        self.state.claim_calm_reels_v1_next_ayah.return_value = dict(response, last_ayah_key='2:2')
        with self.assertRaisesRegex(OwnerV1DispatchError, 'CLAIM_RESULT_INVALID'):
            self.reserve(pair)
        self.state.claim_calm_reels_v1_next_ayah.reset_mock()
        rows['subtitle_ayahs'][1]['english_source_hash'] = 'bad'
        with self.assertRaisesRegex(OwnerV1DispatchError, 'TRANSLATION_HASH_MISMATCH'):
            self.reserve(pair)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_same_run_reclaim_never_permits_another_send(self):
        self.state.claim_calm_reels_v1_next_ayah.return_value["state"] = "ALREADY_CLAIMED"
        with self.assertRaisesRegex(OwnerV1DispatchError, "NO_REDISPATCH"):
            self.reserve()

    def test_timeout_unknown_or_bad_claim_result_is_never_dispatched(self):
        self.state.claim_calm_reels_v1_next_ayah.side_effect = TimeoutError("provider uncertain")
        with self.assertRaisesRegex(OwnerV1DispatchError, "RECONCILE_MANUALLY"):
            self.reserve()
        self.state.claim_calm_reels_v1_next_ayah.side_effect = None
        for response in ({}, {"state": "CLAIMED", "cycle_number": True, "ayah_key": "1:1"},
                         {"state": "CLAIMED", "cycle_number": 1, "ayah_key": "1:3"}):
            with self.subTest(response=response):
                self.state.claim_calm_reels_v1_next_ayah.return_value = response
                with self.assertRaisesRegex(OwnerV1DispatchError, "CLAIM_RESULT_INVALID"):
                    self.reserve()

    def test_weak_or_missing_rights_and_missing_claim_api(self):
        for env, stored in (
            ({}, self.stored),
            (self.env, SimpleNamespace(reciter_reuse_confirmed=False)),
        ):
            with self.subTest(env=env):
                with self.assertRaisesRegex(OwnerV1DispatchError, "RIGHTS_NOT_CONFIRMED"):
                    self.reserve(env=env, stored_config=stored)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()
        with self.assertRaisesRegex(OwnerV1DispatchError, "ATOMIC_CLAIM_STORE_REQUIRED"):
            self.reserve(store=object())

    def test_safety_unsent_and_run_kind_are_mandatory(self):
        invalid = (
            replace(self.record, final_safety_passed=False),
            replace(self.record, status="FAILED"),
            replace(self.record, run_kind="test", slot="test", schedule_slot_id=None),
            replace(self.record, media_key=""),
            replace(self.record, facebook_status="ACCEPTED"),
            replace(self.record, facebook_status="UNKNOWN"),
            replace(self.record, facebook_status="SUCCESS"),
            replace(self.record, facebook_media_id="actual-video-id"),
            replace(self.record, instagram_status="ACCEPTED"),
            replace(self.record, instagram_status="UNKNOWN"),
            replace(self.record, instagram_status="SUCCESS"),
            replace(self.record, instagram_media_id="instagram-video-id"),
            replace(self.record, buffer_post_id="buffer-id"),
        )
        for record in invalid:
            with self.subTest(record=(record.status, record.facebook_status)):
                with self.assertRaisesRegex(OwnerV1DispatchError, "FINAL_SAFE_UNSENT"):
                    self.reserve(record)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_cycle_or_key_mismatch_fails_before_db_claim(self):
        owner = self.record.provenance["owner_v1"]
        for changes in (
            {"cycle_number": True}, {"cycle_number": 0},
            {"cycle_number": 2, "ayah_key": "1:3"},
            {"editorial_selection_only": False},
            {"catalog_id": "different"},
        ):
            with self.subTest(changes=changes):
                bad = replace(self.record, provenance={
                    **self.record.provenance,
                    "owner_v1": {**owner, **changes},
                })
                with self.assertRaises(OwnerV1DispatchError):
                    self.reserve(bad)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_fabricated_legacy_approval_is_refused(self):
        for field in ("production_authorization", "theme_policy_version",
                      "editorial_reconciliation_status"):
            bad = replace(self.record, provenance={
                **self.record.provenance, field: "APPROVED_AT_GENERATION",
            })
            with self.subTest(field=field):
                with self.assertRaisesRegex(OwnerV1DispatchError, "LEGACY_APPROVAL_NOT_ALLOWED"):
                    self.reserve(bad)

    def test_tampered_arabic_and_translations_fail_before_claim(self):
        caption = self.record.provenance["caption_ayahs"][0]
        for field, altered in (
            ("arabic_text", ARABIC + "x"),
            ("arabic_normalized_sha256", "0" * 64),
        ):
            bad = replace(self.record, provenance={
                **self.record.provenance, "caption_ayahs": [{**caption, field: altered}],
            })
            with self.subTest(field=field):
                with self.assertRaisesRegex(OwnerV1DispatchError, "FINAL_ARABIC_HASH_MISMATCH"):
                    self.reserve(bad)
        for changed in ({"text": "fabricated"}, {"normalized_sha256": "bad"},
                        {"translation_version": "unsupported"}):
            bad = replace(self.record, provenance={
                **self.record.provenance,
                "caption_ayahs": [{**caption, "translations": {
                    "en": {**caption["translations"]["en"], **changed},
                }}],
            })
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(OwnerV1DispatchError, "TRANSLATION_HASH_MISMATCH"):
                    self.reserve(bad)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_trusted_translation_source_mismatch_is_rejected(self):
        with patch("engine.reel_publication_policy._trusted_source_hashes",
                   return_value=("different", "different")):
            with self.assertRaisesRegex(OwnerV1DispatchError, "TRUSTED_SOURCE_MISMATCH"):
                self.reserve()
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_buffer_transport_runs_once_only_after_database_claim(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferPublishRequest,
            build_buffer_facebook_caption,
        )
        from engine.public_media_delivery import ensure_public_media_delivery

        record = replace(self.record, public_media_delivery_id="fixture-media-identity-00001")
        env = {**self.env, "PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://example.test"}
        _, url = ensure_public_media_delivery(record, env)
        request = BufferPublishRequest(
            run_id=record.run_id,
            caption=build_buffer_facebook_caption(record),
            public_media_url=url,
            editorial_record=record,
        )
        client = BufferClient(BufferConfig(api_key="example"))
        page = BufferChannel(
            id="page-fixture", display_name="Page", service="facebook",
            descriptor="Facebook Page",
        )
        order = []

        def claim(**kwargs):
            order.append("CLAIM")
            return {"state": "CLAIMED", "ayah_key": "1:1", "cycle_number": 1}

        def send(*args, **kwargs):
            order.append("BUFFER")
            self.assertTrue(kwargs["write"])
            return {"data": {"createPost": {"post": {
                "id": "buffer-ack", "status": "sent",
            }}}}

        self.state.claim_calm_reels_v1_next_ayah.side_effect = claim
        with (
            patch.object(client, "resolve_facebook_channel", return_value=(object(), page)),
            patch.object(client, "_post_once", side_effect=send) as sender,
        ):
            result = client.create_owner_v1_facebook_reel(
                page.id, request, store=self.state,
                stored_config=self.stored, env=env,
            )
        self.assertEqual(result.id, "buffer-ack")
        self.assertEqual(order, ["CLAIM", "BUFFER"])
        self.assertEqual(sender.call_count, 1)
        self.state.save_run.assert_not_called()
        self.state.complete_run_record.assert_not_called()

    def test_buffer_refuses_replay_and_foreign_media_before_mutation(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferPublishRequest,
            BufferPublisherError, build_buffer_facebook_caption,
        )
        from engine.public_media_delivery import ensure_public_media_delivery

        record = replace(self.record, public_media_delivery_id="fixture-media-identity-00001")
        env = {**self.env, "PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://example.test"}
        _, url = ensure_public_media_delivery(record, env)
        request = BufferPublishRequest(
            run_id=record.run_id,
            caption=build_buffer_facebook_caption(record),
            public_media_url=url, editorial_record=record,
        )
        client = BufferClient(BufferConfig(api_key="example"))
        page = BufferChannel(
            id="page-fixture", display_name="Page", service="facebook",
            descriptor="Facebook Page",
        )
        with (
            patch.object(client, "resolve_facebook_channel", return_value=(object(), page)),
            patch.object(client, "_post_once") as send,
        ):
            for changed, code in (
                ({"caption": request.caption + "changed"}, "INVALID_CAPTION"),
                ({"public_media_url": "https://example.test/other.mp4"}, "INVALID_MEDIA"),
                ({"run_id": "other-run"}, "INVALID_TARGET"),
                ({"save_to_draft": True}, "INVALID_TARGET"),
            ):
                from dataclasses import replace as replace_request
                with self.subTest(code=code):
                    with self.assertRaisesRegex(BufferPublisherError, code):
                        client.create_owner_v1_facebook_reel(
                            page.id, replace_request(request, **changed),
                            store=self.state, stored_config=self.stored, env=env,
                        )
            with self.assertRaisesRegex(BufferPublisherError, "INVALID_TARGET"):
                client.create_owner_v1_facebook_reel(
                    "different-page", request, store=self.state,
                    stored_config=self.stored, env=env,
                )
            self.state.claim_calm_reels_v1_next_ayah.assert_not_called()
            self.state.claim_calm_reels_v1_next_ayah.return_value["state"] = "ALREADY_CLAIMED"
            with self.assertRaisesRegex(OwnerV1DispatchError, "NO_REDISPATCH"):
                client.create_owner_v1_facebook_reel(
                    page.id, request, store=self.state,
                    stored_config=self.stored, env=env,
                )
            send.assert_not_called()

    def test_different_owner_hash_or_missing_caption_fails(self):
        owner = self.record.provenance["owner_v1"]
        changed = replace(self.record, provenance={
            **self.record.provenance,
            "owner_v1": {**owner, "source_sha256": "f" * 64},
        })
        with self.assertRaisesRegex(OwnerV1DispatchError, "FINAL_ARABIC_HASH_MISMATCH"):
            self.reserve(changed)
        changed = replace(self.record, provenance={
            **self.record.provenance, "caption_ayahs": [],
        })
        with self.assertRaisesRegex(OwnerV1DispatchError, "FINAL_SOURCE_EVIDENCE_MISSING"):
            self.reserve(changed)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_owner_v1_buffer_plan_targets_only_configured_facebook_and_instagram(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferOrganization,
            build_buffer_facebook_caption,
        )
        from engine.public_media_delivery import ensure_public_media_delivery

        record = replace(self.record, public_media_delivery_id="owner-plan-fixture-00001")
        env = {
            **self.env,
            "PUBLISHER_BACKEND": "buffer",
            "BUFFER_PUBLISHING_ENABLED": "true",
            "BUFFER_FACEBOOK_ENABLED": "true",
            "BUFFER_INSTAGRAM_ENABLED": "true",
            "PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://example.test",
        }
        config = SimpleNamespace(
            reciter_reuse_confirmed=True,
            facebook_enabled=True,
            instagram_enabled=True,
        )
        client = BufferClient(BufferConfig(
            api_key="fixture", facebook_channel_id="facebook-page",
            instagram_channel_id="instagram-account",
        ))
        org = BufferOrganization(id="org", name="Fixture")
        channels = (
            BufferChannel(id="facebook-page", display_name="Page",
                          service="facebook", descriptor="Facebook Page"),
            BufferChannel(id="instagram-account", display_name="Instagram",
                          service="instagram"),
            BufferChannel(id="unrelated-ig", display_name="Other",
                          service="instagram"),
        )
        with (
            patch.object(client, "read_connected_channels", return_value=(org, channels)) as read_channels,
            patch.object(client, "_post_once") as send,
        ):
            facebook, instagram, request = client.prepare_owner_v1_targets(
                record, stored_config=config, env=env,
            )
            read_channels.assert_called_once()
            send.assert_not_called()
        self.assertEqual((facebook.id, instagram.id), ("facebook-page", "instagram-account"))
        self.assertEqual(request.run_id, record.run_id)
        self.assertEqual(request.caption, build_buffer_facebook_caption(record))
        self.assertEqual(request.public_media_url, ensure_public_media_delivery(record, env)[1])
        self.assertIs(request.editorial_record, record)
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_owner_v1_buffer_plan_fails_closed_before_any_mutation(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferOrganization,
            BufferPublisherError,
        )

        record = replace(self.record, public_media_delivery_id="owner-plan-fixture-00001")
        env = {
            **self.env,
            "PUBLISHER_BACKEND": "buffer",
            "BUFFER_PUBLISHING_ENABLED": "true",
            "BUFFER_FACEBOOK_ENABLED": "true",
            "BUFFER_INSTAGRAM_ENABLED": "true",
            "PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://example.test",
        }
        config = SimpleNamespace(
            reciter_reuse_confirmed=True, facebook_enabled=True,
            instagram_enabled=True,
        )
        client = BufferClient(BufferConfig(
            api_key="fixture", facebook_channel_id="facebook-page",
            instagram_channel_id="instagram-account",
        ))
        channels = (
            BufferChannel(id="facebook-page", display_name="Page",
                          service="facebook", descriptor="Facebook Page"),
            BufferChannel(id="instagram-account", display_name="Instagram",
                          service="instagram"),
        )
        with (
            patch.object(client, "read_connected_channels",
                         return_value=(BufferOrganization(id="org", name="Fixture"), channels)) as read,
            patch.object(client, "_post_once") as send,
        ):
            for changed_env, changed_record, changed_config in (
                ({**env, "BUFFER_INSTAGRAM_ENABLED": "false"}, record, config),
                ({**env, "PUBLISHER_BACKEND": "meta"}, record, config),
                (env, replace(record, final_safety_passed=False), config),
                (env, replace(record, facebook_status="ACCEPTED"), config),
                (env, replace(record, instagram_media_id="prior-post"), config),
                (env, replace(record, public_media_delivery_id=None), config),
                (env, record, SimpleNamespace(reciter_reuse_confirmed=False, facebook_enabled=True, instagram_enabled=True)),
            ):
                with self.subTest(changed_env=changed_env, changed_record=changed_record):
                    with self.assertRaises((BufferPublisherError, OwnerV1DispatchError)):
                        client.prepare_owner_v1_targets(
                            changed_record, stored_config=changed_config, env=changed_env,
                        )
            send.assert_not_called()
            read.assert_not_called()  # All rejected before reading connected channels.
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()

    def test_owner_v1_buffer_pair_claims_facebook_then_saves_before_instagram(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferOrganization,
        )

        record = replace(self.record, public_media_delivery_id="owner-plan-fixture-00001")
        env = {
            **self.env,
            "GITHUB_ACTIONS": "true", "STATE_AUTHORITY": "supabase",
            "PUBLISHER_BACKEND": "buffer", "BUFFER_PUBLISHING_ENABLED": "true",
            "BUFFER_FACEBOOK_ENABLED": "true", "BUFFER_INSTAGRAM_ENABLED": "true",
            "PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://example.test",
        }
        config = SimpleNamespace(
            reciter_reuse_confirmed=True, facebook_enabled=True, instagram_enabled=True,
        )
        client = BufferClient(BufferConfig(
            api_key="fixture", facebook_channel_id="facebook-page",
            instagram_channel_id="instagram-account",
        ))
        channels = (
            BufferChannel(id="facebook-page", display_name="Page",
                          service="facebook", descriptor="Facebook Page"),
            BufferChannel(id="instagram-account", display_name="Instagram",
                          service="instagram"),
        )
        self.state.get_run.return_value = record
        events = []

        def owner_claim(**kwargs):
            events.append("OWNER_CLAIM")
            return {"state": "CLAIMED", "cycle_number": 1, "ayah_key": "1:1"}

        def buffer_send(_query, variables, *, write=False):
            self.assertTrue(write)
            channel = variables["input"]["channelId"]
            events.append("SEND_" + channel)
            return {"data": {"createPost": {"post": {
                "id": "post-fb" if channel == "facebook-page" else "post-ig",
                "status": "sent",
            }}}}

        def checkpoint(rec):
            events.append("SAVE_" + rec.facebook_status + "_" + rec.instagram_status)

        def ig_claim(_claim):
            events.append("IG_CLAIM")
            return {"_claim_created": True, "state": "DISPATCHING"}

        self.state.claim_calm_reels_v1_next_ayah.side_effect = owner_claim
        self.state.save_run.side_effect = checkpoint
        self.state.claim_buffer_publication_dispatch.side_effect = ig_claim
        self.state.transition_buffer_publication_claim.side_effect = (
            lambda *args, **kwargs: events.append("IG_ACK") or {"state": "SUCCESS"}
        )
        with (
            patch.object(client, "read_connected_channels",
                         return_value=(BufferOrganization(id="org", name="Fixture"), channels)),
            patch.object(client, "_post_once", side_effect=buffer_send) as transport,
        ):
            result = client.submit_owner_v1_pair(
                record, store=self.state, stored_config=config, env=env,
            )

        self.assertEqual(events, [
            "OWNER_CLAIM", "SEND_facebook-page", "SAVE_ACCEPTED_NOT_ATTEMPTED",
            "IG_CLAIM", "SEND_instagram-account", "IG_ACK", "SAVE_ACCEPTED_ACCEPTED",
        ])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result.facebook_status, "ACCEPTED")
        self.assertEqual(result.instagram_status, "ACCEPTED")
        self.assertEqual((result.buffer_post_id, result.instagram_media_id), ("post-fb", "post-ig"))
        self.state.record_calm_reels_v1_verified_facebook_post.assert_not_called()
        self.state.record_covered_ayahs.assert_not_called()

    def test_owner_v1_buffer_pair_stops_after_uncertain_facebook_save(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferOrganization, BufferPublisherError,
        )

        record = replace(self.record, public_media_delivery_id="owner-plan-fixture-00001")
        env = {
            **self.env, "PUBLISHER_BACKEND": "buffer",
            "GITHUB_ACTIONS": "true", "STATE_AUTHORITY": "supabase",
            "BUFFER_PUBLISHING_ENABLED": "true", "BUFFER_FACEBOOK_ENABLED": "true",
            "BUFFER_INSTAGRAM_ENABLED": "true",
            "PUBLIC_MEDIA_DELIVERY_ORIGIN": "https://example.test",
        }
        config = SimpleNamespace(
            reciter_reuse_confirmed=True, facebook_enabled=True, instagram_enabled=True,
        )
        client = BufferClient(BufferConfig(
            api_key="fixture", facebook_channel_id="facebook-page",
            instagram_channel_id="instagram-account",
        ))
        channels = (
            BufferChannel(id="facebook-page", display_name="Page",
                          service="facebook", descriptor="Facebook Page"),
            BufferChannel(id="instagram-account", display_name="Instagram",
                          service="instagram"),
        )
        self.state.get_run.return_value = record
        self.state.save_run.side_effect = TimeoutError("checkpoint uncertain")
        with (
            patch.object(client, "read_connected_channels",
                         return_value=(BufferOrganization(id="org", name="Fixture"), channels)),
            patch.object(client, "_post_once", return_value={
                "data": {"createPost": {"post": {"id": "fb-post", "status": "sent"}}},
            }) as transport,
        ):
            with self.assertRaisesRegex(
                BufferPublisherError, "OWNER_V1_FACEBOOK_ACK_CHECKPOINT_UNKNOWN",
            ):
                client.submit_owner_v1_pair(
                    record, store=self.state, stored_config=config, env=env,
                )
        transport.assert_called_once()
        self.state.claim_buffer_publication_dispatch.assert_not_called()
        self.state.record_calm_reels_v1_verified_facebook_post.assert_not_called()

    def test_owner_v1_pair_cannot_send_from_local_mac(self):
        from engine.buffer_publisher import BufferClient, BufferConfig, BufferPublisherError
        client = BufferClient(BufferConfig(api_key="fixture"))
        with self.assertRaisesRegex(BufferPublisherError, "OWNER_V1_GITHUB_SUPABASE_REQUIRED"):
            client.submit_owner_v1_pair(
                self.record, store=self.state,
                stored_config=SimpleNamespace(reciter_reuse_confirmed=True),
                env={"STATE_AUTHORITY": "supabase", "PUBLISHER_BACKEND": "buffer"},
            )
        self.state.get_run.assert_not_called()
        self.state.claim_calm_reels_v1_next_ayah.assert_not_called()
        self.state.save_run.assert_not_called()

    def test_owner_v1_unknown_instagram_result_is_not_retried_or_counted(self):
        from engine.buffer_publisher import (
            BufferClient, BufferChannel, BufferConfig, BufferPost,
            BufferPublishRequest, BufferPublisherError,
        )
        record = replace(self.record, public_media_delivery_id="owner-plan-fixture-00001")
        request = BufferPublishRequest(
            run_id=record.run_id, caption="fixture",
            public_media_url="https://example.test/video.mp4", editorial_record=record,
        )
        facebook = BufferChannel(
            id="facebook-page", display_name="Page",
            service="facebook", descriptor="Facebook Page",
        )
        instagram = BufferChannel(
            id="instagram-account", display_name="Instagram", service="instagram",
        )
        client = BufferClient(BufferConfig(api_key="fixture"))
        self.state.get_run.return_value = record
        self.state.claim_buffer_publication_dispatch.return_value = {
            "_claim_created": True, "state": "DISPATCHING",
        }
        self.state.transition_buffer_publication_claim.return_value = {"state": "UNKNOWN_OUTCOME"}
        with (
            patch.object(client, "prepare_owner_v1_targets",
                         return_value=(facebook, instagram, request)),
            patch.object(client, "create_owner_v1_facebook_reel",
                         return_value=BufferPost(id="fb-post", status="sent")) as send_fb,
            patch.object(client, "_create_reel_after_policy",
                         side_effect=TimeoutError("provider uncertain")) as send_ig,
        ):
            with self.assertRaisesRegex(BufferPublisherError, "OWNER_V1_INSTAGRAM_SEND_UNKNOWN"):
                client.submit_owner_v1_pair(
                    record, store=self.state,
                    stored_config=SimpleNamespace(reciter_reuse_confirmed=True),
                    env={"GITHUB_ACTIONS": "true", "STATE_AUTHORITY": "supabase"},
                )
        send_fb.assert_called_once()
        send_ig.assert_called_once()
        self.assertEqual(self.state.save_run.call_count, 2)
        self.assertEqual(self.state.save_run.call_args.args[0].facebook_status, "ACCEPTED")
        self.assertEqual(self.state.save_run.call_args.args[0].instagram_status, "UNKNOWN")
        self.state.transition_buffer_publication_claim.assert_called_once()
        self.state.record_calm_reels_v1_verified_facebook_post.assert_not_called()
