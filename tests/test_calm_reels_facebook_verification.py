"""No-network tests of Buffer as the only social delivery evidence source."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from engine.buffer_publisher import BufferClient, build_buffer_facebook_caption
from engine.calm_reels_cycle import FreshCycleSnapshot
from engine.facebook_delivery_verification import (
    FacebookDeliveryPending, FacebookVerificationError, _published_video_id,
    confirm_after_buffer_acceptance, confirm_owner_v1_facebook_delivery, verify_owner_v1_facebook_delivery,
)
from engine.public_media_delivery import ensure_public_media_delivery
from tests.test_calm_reels_dispatch import valid_record


class FacebookDeliveryVerificationTests(TestCase):
    def setUp(self):
        self.record = replace(valid_record(), facebook_status='ACCEPTED', buffer_post_id='buffer-post-1',
            facebook_media_id='buffer-post-1', instagram_status='ACCEPTED', instagram_media_id='ig-post-1',
            public_media_delivery_id='owner-video-proof-111122223333')
        self.url = ensure_public_media_delivery(self.record, {'PUBLIC_MEDIA_DELIVERY_ORIGIN': 'https://public.example.test'})[1]
        self.video = '123456789012'
        self.link = f'https://www.facebook.com/reel/{self.video}'
        self.post = dict(id='buffer-post-1', channelId='fb-channel', channelService='facebook',
            schedulingType='automatic', status='sent', sentAt='2026-10-30T13:00:00Z',
            text=build_buffer_facebook_caption(self.record), externalLink=self.link, assets=[{'source': self.url}])
        self.now = datetime(2026, 10, 30, 13, 5, tzinfo=timezone.utc)
        self.buffer = MagicMock(spec=BufferClient)
        self.buffer.graphql.side_effect = lambda *_: {'data': {'post': self.post}}
        self.args = dict(buffer=self.buffer, facebook_channel_id='fb-channel', expected_public_media_url=self.url)
        self.store = MagicMock()
        self.store.get_run.return_value = self.record
        self.store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(1, '2026-10-28T21:16:47Z', frozenset())
        self.store.record_calm_reels_v1_verified_facebook_post.return_value = {'state': 'RECORDED'}
        gate = patch('engine.calm_reels_dispatch.require_owner_v1_dispatch_record', return_value=(1, '1:1'))
        self.source_gate = gate.start()
        self.addCleanup(gate.stop)
        # Only the mocked Buffer client may be used. In particular, no direct
        # platform request can silently creep into delivery verification.
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Direct HTTP forbidden'))
        self.http = guard.start()
        self.addCleanup(guard.stop)

    def verify(self, record=None):
        return verify_owner_v1_facebook_delivery(record or self.record, now=self.now, **self.args)

    def confirm(self):
        return confirm_owner_v1_facebook_delivery(self.record, now=self.now, store=self.store, **self.args)

    def test_automatic_sent_post_is_bound_to_exact_channel_run_ayah_and_media(self):
        proof = self.verify()
        self.assertEqual((proof.facebook_video_id, proof.buffer_post_id), (self.video, 'buffer-post-1'))
        self.assertEqual((proof.ayah_key, proof.cycle_number, proof.buffer_channel_id), ('1:1', 1, 'fb-channel'))
        self.assertEqual(len(proof.evidence_sha256), 64)
        self.assertEqual(proof.permalink_url, self.link)
        query, variables = self.buffer.graphql.call_args.args
        self.assertIn('post(input:', query)
        self.assertNotIn('mutation', query.lower())
        self.assertEqual(variables['input']['id'], self.record.buffer_post_id)
        self.http.assert_not_called()

    def test_pending_failed_draft_and_mismatched_posts_never_write(self):
        for key, value in [('id', 'other'), ('channelId', 'other'), ('channelService', 'instagram'),
                ('schedulingType', 'notification'), ('text', 'other caption'), ('assets', []),
                ('assets', [{'source': 'https://other.test/video.mp4'}]), ('status', 'error'),
                ('status', 'draft'), ('status', 'accepted'), ('status', 'scheduled'), ('status', 'sending'),
                ('sentAt', None), ('sentAt', '2027-01-01T00:00:00Z'), ('sentAt', '2020-01-01T00:00:00Z'),
                ('externalLink', 'https://evil.example.test/reel/123456789012')]:
            with self.subTest(key=key, value=value), patch.dict(self.post, {key: value}):
                with self.assertRaises(FacebookVerificationError):
                    self.confirm()
        self.store.save_run.assert_not_called()
        self.store.record_calm_reels_v1_verified_facebook_post.assert_not_called()
        self.buffer.create_reel.assert_not_called()

    def test_missing_buffer_link_or_queued_state_is_pending_not_a_send(self):
        for data in ({'externalLink': None}, {'status': 'scheduled', 'sentAt': None}):
            with patch.dict(self.post, data), self.assertRaises(FacebookDeliveryPending):
                self.verify()
        self.buffer.create_reel.assert_not_called()

    def test_failed_read_and_partial_graphql_response_are_not_receipts(self):
        self.buffer.graphql.side_effect = TimeoutError('sensitive fixture')
        with self.assertRaisesRegex(FacebookVerificationError, 'BUFFER_READ_UNAVAILABLE'):
            self.confirm()
        self.buffer.graphql.side_effect = None
        self.buffer.graphql.return_value = {'data': {'post': self.post}, 'errors': [{'message': 'partial'}]}
        with self.assertRaisesRegex(FacebookVerificationError, 'BUFFER_READ_INVALID'):
            self.confirm()
        self.store.save_run.assert_not_called()
        self.store.record_calm_reels_v1_verified_facebook_post.assert_not_called()

    def test_invalid_run_or_media_rejected_before_buffer_read(self):
        for changes in ({'facebook_status': 'SUCCESS'}, {'facebook_status': 'NOT_ATTEMPTED'},
                {'buffer_post_id': None}, {'final_safety_passed': False}, {'media_key': None},
                {'public_media_delivery_id': None}, {'first_ayah': 2, 'last_ayah': 2}):
            with self.subTest(changes=changes), self.assertRaises(FacebookVerificationError):
                self.verify(replace(self.record, **changes))
        self.buffer.graphql.assert_not_called()

    def test_wrong_cycle_or_changed_saved_run_never_queries_buffer(self):
        self.store.get_run.return_value = replace(self.record, media_key='other')
        with self.assertRaisesRegex(FacebookVerificationError, 'SAVED_RUN_CHANGED'):
            self.confirm()
        self.store.get_run.return_value = self.record
        self.store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(1, '2026-10-28T21:16:47Z', frozenset({'1:1'}))
        with self.assertRaisesRegex(FacebookVerificationError, 'FIRST_PENDING_CHANGED'):
            self.confirm()
        self.buffer.graphql.assert_not_called()
        self.store.save_run.assert_not_called()

    def test_exact_receipt_uses_buffer_and_preserves_instagram_state(self):
        updated, _ = self.confirm()
        candidate = self.source_gate.call_args.args[0]
        self.assertEqual((candidate.facebook_status, candidate.instagram_status), ('NOT_ATTEMPTED', 'NOT_ATTEMPTED'))
        self.assertIsNone(candidate.instagram_media_id)
        self.assertEqual((updated.facebook_status, updated.instagram_status, updated.instagram_media_id), ('SUCCESS','ACCEPTED','ig-post-1'))
        proof = updated.provenance['owner_v1']['facebook_verification']
        self.assertEqual((proof['evidence_source'], proof['buffer_post_status'], proof['buffer_channel_id']), ('buffer','sent','fb-channel'))
        self.assertEqual((updated.facebook_media_id, proof['facebook_video_id']), (self.video, self.video))
        self.assertEqual(proof['buffer_post_id'], self.record.buffer_post_id)
        self.assertNotIn('facebook_page_id', proof)
        self.store.save_run.assert_called_once_with(updated)
        self.store.record_calm_reels_v1_verified_facebook_post.assert_called_once_with(cycle_number=1,
            run_id=updated.run_id, facebook_video_id=self.video, evidence_sha256=proof['evidence_sha256'], verified_at=proof['verified_at'])
        self.http.assert_not_called()

    def test_reviewed_five_ayah_fatiha_is_one_verified_buffer_receipt(self):
        from tests.test_owner_reviewed_passage import ReviewedPassageDispatchTests, FATIHA_KEYS, SYNTHETIC_REVIEW
        self.store.load_calm_reels_v1_reviewed_passage_approvals.return_value = (SYNTHETIC_REVIEW,)
        self.record = replace(ReviewedPassageDispatchTests().make_record(),
            facebook_status='ACCEPTED', buffer_post_id='buffer-post-1',
            facebook_media_id='buffer-post-1', instagram_status='ACCEPTED',
            instagram_media_id='ig-post-1', public_media_delivery_id='owner-video-proof-111122223333')
        self.url = ensure_public_media_delivery(self.record, {
            'PUBLIC_MEDIA_DELIVERY_ORIGIN': 'https://public.example.test'})[1]
        self.post['text'] = build_buffer_facebook_caption(self.record)
        self.post['assets'] = [{'source': self.url}]
        self.args['expected_public_media_url'] = self.url
        self.store.get_run.return_value = self.record
        self.store.load_calm_reels_v1_fresh_cycle.return_value = FreshCycleSnapshot(
            1, '2026-10-28T21:16:47Z', frozenset({'1:1'}))
        self.source_gate.return_value = (1, '1:3')
        self.store.record_calm_reels_v1_verified_facebook_post.return_value = {
            'state': 'RECORDED', 'recorded_ayah_count': 5, 'confirmed_ayahs': 6}
        updated, receipt = self.confirm()
        self.assertEqual(receipt['recorded_ayah_count'], 5)
        self.assertEqual(updated.facebook_status, 'SUCCESS')
        self.assertEqual(updated.provenance['owner_v1']['ayah_keys'], list(FATIHA_KEYS))
        self.buffer.create_reel.assert_not_called()
        self.buffer.graphql.assert_called_once()
        self.store.record_calm_reels_v1_verified_facebook_post.assert_called_once()

        # The receipt RPC may have committed; a mismatched return requires
        # manual reconciliation, never another Buffer write or count guess.
        self.store.record_calm_reels_v1_verified_facebook_post.reset_mock()
        self.store.save_run.reset_mock()
        self.buffer.graphql.reset_mock()
        self.store.record_calm_reels_v1_verified_facebook_post.return_value = {
            'state': 'RECORDED', 'recorded_ayah_count': 1, 'confirmed_ayahs': 2}
        with self.assertRaisesRegex(FacebookVerificationError, 'PASSAGE_RECEIPT_COUNT_UNKNOWN'):
            self.confirm()
        self.buffer.create_reel.assert_not_called()

    def test_delayed_sent_retries_only_the_exact_buffer_read(self):
        self.buffer.graphql.side_effect = [{'data': {'post': {**self.post,'status':'sending'}}}, {'data': {'post':self.post}}]
        def at_fixed_time(record, **kwargs):
            return confirm_owner_v1_facebook_delivery(record, now=self.now, **kwargs)
        with patch('engine.facebook_delivery_verification.sleep') as wait, patch('engine.facebook_delivery_verification.confirm_owner_v1_facebook_delivery', side_effect=at_fixed_time):
            updated, warning = confirm_after_buffer_acceptance(self.record, store=self.store, **self.args)
        self.assertIsNone(warning)
        self.assertEqual(updated.facebook_status, 'SUCCESS')
        wait.assert_called_once_with(10)
        self.assertEqual(self.buffer.graphql.call_count, 2)
        self.store.record_calm_reels_v1_verified_facebook_post.assert_called_once()
        self.buffer.create_reel.assert_not_called()

    def test_ambiguous_receipt_commit_does_not_retry_or_resend(self):
        self.store.record_calm_reels_v1_verified_facebook_post.side_effect = TimeoutError('uncertain')
        def at_fixed_time(record, **kwargs):
            return confirm_owner_v1_facebook_delivery(record, now=self.now, **kwargs)
        with patch('engine.facebook_delivery_verification.sleep') as wait, patch('engine.facebook_delivery_verification.confirm_owner_v1_facebook_delivery', side_effect=at_fixed_time):
            unchanged, warning = confirm_after_buffer_acceptance(self.record, store=self.store, **self.args)
        self.assertIs(unchanged, self.record)
        self.assertIn('RECEIPT_OUTCOME_UNKNOWN', warning)
        wait.assert_not_called()
        self.store.save_run.assert_called_once()
        self.store.record_calm_reels_v1_verified_facebook_post.assert_called_once()
        self.buffer.create_reel.assert_not_called()

    def test_facebook_only_compacts_the_decorative_reciter_separator(self):
        from engine.buffer_publisher import RECITER_ATTRIBUTION_AR
        raw = self.post['text']
        compact = raw.replace('\n\n' + RECITER_ATTRIBUTION_AR + '\n\n\n', '\n\n' + RECITER_ATTRIBUTION_AR + '\n\n', 1)
        self.assertEqual(len(raw) - len(compact), 1)
        with patch.dict(self.post, {'text': compact}):
            self.assertEqual(self.verify().facebook_video_id, self.video)
        for invalid in (compact.replace('\n\n', '\n', 1), compact.replace('«', 'X', 1), compact + ' '):
            with patch.dict(self.post, {'text': invalid}), self.assertRaises(FacebookVerificationError):
                self.verify()

    def test_dual_read_binds_both_posts_without_writes(self):
        from scripts.verify_calm_reels_facebook_delivery import verified_read_only_payload, ReadOnlyVerificationError
        sent = datetime.now(timezone.utc).isoformat()
        fb = {**self.post, 'sentAt': sent, 'externalLink': f'https://facebook.com/987654321012_{self.video}'}
        ig = {**self.post, 'sentAt': sent, 'id': self.record.instagram_media_id, 'channelId': 'ig-channel',
              'channelService': 'instagram', 'externalLink': 'https://www.instagram.com/reel/Fixture123/'}
        self.buffer.graphql.side_effect = lambda q, v: {'data': {'post': fb if v['input']['id'] == fb['id'] else ig}}
        def inspect_pair():
            return verified_read_only_payload(self.record.run_id, store=self.store, buffer=self.buffer,
                facebook_channel_id='fb-channel', instagram_channel_id='ig-channel', public_origin='https://public.example.test')
        result = inspect_pair()
        self.assertEqual(result['facebook']['facebook_video_id'], self.video)
        self.assertEqual(result['instagram']['permalink_url'], ig['externalLink'])
        for change in ({'channelId': 'other'}, {'status': 'sending'}, {'assets': []},
                       {'externalLink': 'https://instagram.com.evil.test/reel/Fixture123/'}):
            with self.subTest(change=change), patch.dict(ig, change), self.assertRaises(ReadOnlyVerificationError):
                inspect_pair()
        self.store.save_run.assert_not_called()
        self.store.record_calm_reels_v1_verified_facebook_post.assert_not_called()
        self.buffer.create_reel.assert_not_called()

    def test_permalinks_are_read_as_data_never_followed(self):
        for link in (self.link, f'https://facebook.com/videos/{self.video}',
                     f'https://www.facebook.com/page/videos/{self.video}/', f'https://facebook.com/watch?v={self.video}'):
            self.assertEqual(_published_video_id(link), self.video)
        for link in ('http://facebook.com/reel/123456789012', self.link+'?x=1', self.link+'#x',
                     'https://facebook.com.evil.test/reel/123456789012','https://facebook.com:443/reel/123456789012',
                     'https://facebook.com:invalid/reel/123456789012','https://facebook.com/watch?v=1234567&v=7654321'):
            with self.assertRaises(FacebookVerificationError):
                _published_video_id(link)
        self.http.assert_not_called()
