"""Owner-v1 Facebook receipts from Buffer. No Graph API or Meta credentials.

A write acknowledgement never advances the cycle. Re-read the precise Buffer
post, bind it to the saved run, and use the published Reel link Buffer returns
for the existing numeric external-video receipt contract. Do not follow links.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from time import sleep
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from engine.buffer_publisher import BufferClient, RECITER_ATTRIBUTION_AR, build_buffer_facebook_caption
from engine.public_media_delivery import ensure_public_media_delivery, PublicMediaDeliveryError
from engine.storage import RunRecord


class FacebookVerificationError(ValueError):
    """Safe operator-facing error; never include credentials or provider payloads."""


class FacebookDeliveryPending(FacebookVerificationError):
    """A matching Buffer record has not produced a complete receipt yet."""


def _published_video_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise FacebookVerificationError('OWNER_V1_FACEBOOK_PERMALINK_INVALID')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise FacebookVerificationError('OWNER_V1_FACEBOOK_PERMALINK_INVALID') from error
    if (parsed.scheme != 'https' or parsed.hostname not in {'facebook.com', 'www.facebook.com', 'm.facebook.com'}
        or port is not None or parsed.username or parsed.password or parsed.fragment):
        raise FacebookVerificationError('OWNER_V1_FACEBOOK_PERMALINK_INVALID')
    path = parsed.path.strip('/').split('/')
    query = parse_qs(parsed.query, keep_blank_values=True)
    if len(path) == 2 and path[0] in {'reel', 'videos'} and not parsed.query:
        video_id = path[1]
    elif len(path) == 3 and path[1] == 'videos' and not parsed.query:
        video_id = path[2]
    elif not parsed.query and (scoped := re.fullmatch(r'/[1-9][0-9]{5,24}_([1-9][0-9]{5,24})/?', parsed.path)):
        # Buffer also returns a Page-scoped post permalink for Reels. The second
        # ID is the external post ID, not necessarily a video object ID. The full
        # link, Buffer channel, source video, caption and post ID remain bound.
        video_id = scoped.group(1)
    elif path == ['watch'] and set(query) == {'v'} and len(query['v']) == 1:
        video_id = query['v'][0]
    else:
        raise FacebookVerificationError('OWNER_V1_FACEBOOK_PERMALINK_UNSUPPORTED')
    if not re.fullmatch(r'[1-9][0-9]{5,24}', video_id):
        raise FacebookVerificationError('OWNER_V1_FACEBOOK_PERMALINK_UNSUPPORTED')
    return video_id


def _utc(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise FacebookVerificationError(code)
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise FacebookVerificationError(code) from error
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise FacebookVerificationError(code)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class VerifiedFacebookDelivery:
    cycle_number: int
    ayah_key: str
    run_id: str
    buffer_post_id: str
    facebook_video_id: str
    buffer_channel_id: str
    sent_at: str
    verified_at: str
    permalink_url: str
    evidence_sha256: str


def verify_owner_v1_facebook_delivery(
    record: RunRecord, *, buffer: BufferClient, facebook_channel_id: str,
    expected_public_media_url: str, now: datetime | None = None,
) -> VerifiedFacebookDelivery:
    """Read a single post from Buffer. No mutation or destination-platform API call."""
    from engine.calm_reels_dispatch import require_owner_v1_dispatch_record
    if not isinstance(record, RunRecord) or record.status != 'SUCCESS':
        raise FacebookVerificationError('OWNER_V1_RECONCILIATION_RUN_INVALID')
    owner = record.provenance.get('owner_v1') if isinstance(record.provenance, Mapping) else None
    if (not isinstance(owner, Mapping) or owner.get('editorial_selection_only') is not True
        or type(owner.get('cycle_number')) is not int or owner['cycle_number'] < 1
        or record.final_safety_passed is not True or not record.media_key
        or owner.get('ayah_key') != f'{record.surah_number}:{record.first_ayah}'
        or type(record.first_ayah) is not int or type(record.last_ayah) is not int
        or record.last_ayah < record.first_ayah):
        raise FacebookVerificationError('OWNER_V1_RECONCILIATION_IDENTITY_INVALID')
    try:
        # Validate original content without rewriting the real delivery state.
        require_owner_v1_dispatch_record(replace(record, facebook_status='NOT_ATTEMPTED',
            facebook_media_id=None, buffer_post_id=None, instagram_status='NOT_ATTEMPTED', instagram_media_id=None))
    except Exception as error:
        raise FacebookVerificationError('OWNER_V1_RECONCILIATION_SOURCE_INVALID') from error
    if (record.facebook_status not in ('ACCEPTED', 'UNKNOWN') or not record.buffer_post_id
        or not facebook_channel_id or not isinstance(expected_public_media_url, str)):
        raise FacebookVerificationError('OWNER_V1_BUFFER_ACCEPTANCE_NOT_RECORDED')
    try:
        restored, actual_url = ensure_public_media_delivery(record, {
            'PUBLIC_MEDIA_DELIVERY_ORIGIN': expected_public_media_url.split('/api/public-media/', 1)[0]})
    except (PublicMediaDeliveryError, ValueError) as error:
        raise FacebookVerificationError('OWNER_V1_PUBLIC_MEDIA_MISMATCH') from error
    if restored != record or actual_url != expected_public_media_url or not record.public_media_delivery_id:
        raise FacebookVerificationError('OWNER_V1_PUBLIC_MEDIA_MISMATCH')
    try:
        response = buffer.graphql('query OwnerV1BufferPost($input: PostInput!) { post(input: $input) { '
            'id channelId channelService schedulingType text status sentAt externalLink assets { source } } }',
            {'input': {'id': record.buffer_post_id}})
        post = response.get('data', {}).get('post') if not response.get('errors') else None
    except Exception as error:
        raise FacebookVerificationError('OWNER_V1_BUFFER_READ_UNAVAILABLE') from error
    if not isinstance(post, Mapping):
        raise FacebookVerificationError('OWNER_V1_BUFFER_READ_INVALID')
    assets = post.get('assets')
    caption = build_buffer_facebook_caption(record)
    # Facebook compacts the single decorative triple-LF after the Arabic reciter
    # credit. No Quran, translation, attribution or other spacing may change.
    credit = '\n\n' + RECITER_ATTRIBUTION_AR
    displayed_caption = caption.replace(credit + '\n\n\n', credit + '\n\n', 1)
    if (post.get('id') != record.buffer_post_id or post.get('channelId') != facebook_channel_id
        or post.get('channelService') != 'facebook' or post.get('schedulingType') != 'automatic'
        or post.get('text') not in (caption, displayed_caption) or not isinstance(assets, list)
        or len(assets) != 1 or not isinstance(assets[0], Mapping) or assets[0].get('source') != actual_url):
        raise FacebookVerificationError('OWNER_V1_BUFFER_POST_MISMATCH')
    if post.get('status') in ('sending', 'scheduled', 'pending'):
        raise FacebookDeliveryPending('OWNER_V1_BUFFER_DELIVERY_PENDING')
    if post.get('status') != 'sent':
        raise FacebookVerificationError('OWNER_V1_BUFFER_POST_NOT_SENT')
    sent = _utc(post.get('sentAt'), 'OWNER_V1_BUFFER_SENT_TIME_INVALID')
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise FacebookVerificationError('OWNER_V1_VERIFICATION_TIME_INVALID')
    observed = observed.astimezone(timezone.utc)
    if not 0 <= (observed - sent).total_seconds() <= 90 * 86400:
        raise FacebookVerificationError('OWNER_V1_BUFFER_SENT_TIME_INVALID')
    link = post.get('externalLink')
    if not link:
        # Buffer's externalLink is nullable. Keep the current receipt schema:
        # await the link from Buffer, never ask the owner for a platform ID.
        raise FacebookDeliveryPending('OWNER_V1_BUFFER_REEL_LINK_PENDING')
    video_id = _published_video_id(link)
    verified_at = observed.isoformat().replace('+00:00', 'Z')
    payload = dict(source='buffer', status='sent', cycle=owner['cycle_number'], ayah=owner['ayah_key'],
                   ayah_keys=owner.get('ayah_keys', [owner['ayah_key']]),
                   run=record.run_id, post=record.buffer_post_id, channel=facebook_channel_id,
                   media_key=record.media_key, media_url=actual_url, caption=post['text'],
                   sent_at=sent.isoformat(), permalink=link, verified_at=verified_at)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return VerifiedFacebookDelivery(owner['cycle_number'], owner['ayah_key'], record.run_id,
        record.buffer_post_id, video_id, facebook_channel_id, sent.isoformat(), verified_at, link, digest)


def confirm_owner_v1_facebook_delivery(
    record: RunRecord, *, buffer: BufferClient, facebook_channel_id: str,
    expected_public_media_url: str, store: Any, now: datetime | None = None,
) -> tuple[RunRecord, Mapping[str, Any]]:
    """Persist Buffer-reported publication only for the saved first-pending run."""
    from engine.calm_reels_cycle import preview_next_owner_ayah_from_store
    required = ('get_run', 'save_run', 'record_calm_reels_v1_verified_facebook_post')
    if any(not callable(getattr(store, action, None)) for action in required):
        raise FacebookVerificationError('OWNER_V1_RECEIPT_STORE_UNAVAILABLE')
    try:
        saved = store.get_run(record.run_id)
    except Exception as error:
        raise FacebookVerificationError('OWNER_V1_SAVED_RUN_UNAVAILABLE') from error
    if not isinstance(saved, RunRecord) or saved != record:
        raise FacebookVerificationError('OWNER_V1_SAVED_RUN_CHANGED')
    try:
        decision = preview_next_owner_ayah_from_store(store)
    except Exception as error:
        raise FacebookVerificationError('OWNER_V1_CYCLE_UNAVAILABLE') from error
    owner = record.provenance.get('owner_v1')
    if (not isinstance(owner, Mapping) or decision.state != 'READY_FOR_TECHNICAL_PREFLIGHT'
        or decision.cycle_number != owner.get('cycle_number') or decision.next_ayah_key != owner.get('ayah_key')
        or (decision.reviewed_ayah_keys and tuple(
            f'{record.surah_number}:{n}' for n in range(record.first_ayah, record.last_ayah + 1)
        ) != decision.reviewed_ayah_keys)
        or (not decision.reviewed_ayah_keys and record.first_ayah != record.last_ayah
            and decision.pending_successor != f'{record.surah_number}:{record.last_ayah}')):
        raise FacebookVerificationError('OWNER_V1_FIRST_PENDING_CHANGED')
    verified = verify_owner_v1_facebook_delivery(record, buffer=buffer, facebook_channel_id=facebook_channel_id,
                                                 expected_public_media_url=expected_public_media_url, now=now)
    proof = dict(schema='owner-v1-facebook-verified-delivery-v1', evidence_source='buffer',
        buffer_channel_id=verified.buffer_channel_id, sent_at=verified.sent_at,
        buffer_post_id=verified.buffer_post_id, facebook_video_id=verified.facebook_video_id,
        permalink_url=verified.permalink_url, buffer_post_status='sent',
        verified_at=verified.verified_at, evidence_sha256=verified.evidence_sha256)
    updated = replace(record, facebook_status='SUCCESS', facebook_media_id=verified.facebook_video_id,
        facebook_reel_url=verified.permalink_url, facebook_published_at=verified.verified_at,
        buffer_post_status='sent', facebook_failure_reason=None,
        provenance={**record.provenance, 'owner_v1': {**owner, 'facebook_verification': proof}})
    try:
        store.save_run(updated)
    except Exception as error:
        raise FacebookVerificationError('OWNER_V1_VERIFIED_RUN_SAVE_FAILED') from error
    try:
        result = store.record_calm_reels_v1_verified_facebook_post(cycle_number=verified.cycle_number,
            run_id=record.run_id, facebook_video_id=verified.facebook_video_id,
            evidence_sha256=verified.evidence_sha256, verified_at=verified.verified_at)
    except Exception as error:
        raise FacebookVerificationError('OWNER_V1_RECEIPT_OUTCOME_UNKNOWN_RECONCILE_MANUALLY') from error
    if not isinstance(result, Mapping) or result.get('state') not in ('RECORDED', 'ROLLED_OVER', 'ALREADY_RECORDED'):
        raise FacebookVerificationError('OWNER_V1_RECEIPT_RESPONSE_INVALID')
    # The DB must attest that one externally verified Reel committed every
    # member in one transaction. A malformed acknowledgement is UNKNOWN,
    # never permission to send again.
    expected_ayahs = record.last_ayah - record.first_ayah + 1
    if expected_ayahs > 2 and (type(result.get('recorded_ayah_count')) is not int
                              or result['recorded_ayah_count'] != expected_ayahs):
        raise FacebookVerificationError('OWNER_V1_PASSAGE_RECEIPT_COUNT_UNKNOWN_RECONCILE_MANUALLY')
    return updated, dict(result)


def confirm_after_buffer_acceptance(
    record: RunRecord, *, buffer: BufferClient, store: Any, facebook_channel_id: str, expected_public_media_url: str,
) -> tuple[RunRecord, str | None]:
    """Retry only matched pending reads; never repeat a send or an uncertain commit."""
    for delay in (0, 10, 20, 30):
        if delay:
            sleep(delay)
        try:
            updated, _ = confirm_owner_v1_facebook_delivery(record, buffer=buffer, store=store,
                facebook_channel_id=facebook_channel_id, expected_public_media_url=expected_public_media_url)
            return updated, None
        except FacebookDeliveryPending:
            continue
        except FacebookVerificationError as error:
            return record, f'Facebook receipt requires reconciliation: {error}. Do not resend.'
        except Exception:
            return record, 'Facebook receipt outcome unknown. Inspect this run; do not resend.'
    return record, 'Delivery pending: the count remains unchanged; reconcile this run without resending.'
