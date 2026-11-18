#!/usr/bin/env python3
"""Read Buffer's publication evidence for an existing owner-v1 Reel; no writes."""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping, Sequence
from typing import Any

from engine.facebook_delivery_verification import FacebookVerificationError, verify_owner_v1_facebook_delivery
from engine.public_media_delivery import ensure_public_media_delivery
from scripts.preview_calm_reels_v1 import _local_supabase_environment


class ReadOnlyVerificationError(ValueError):
    """Safe diagnostic without provider details or tokens."""


def verified_read_only_payload(run_id: str, *, store: Any, buffer: Any, facebook_channel_id: str,
                               public_origin: str, instagram_channel_id: str | None = None) -> dict[str, object]:
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 120:
        raise ReadOnlyVerificationError('OWNER_V1_RUN_ID_INVALID')
    if not facebook_channel_id or not public_origin:
        raise ReadOnlyVerificationError('OWNER_V1_READ_CONFIG_MISSING')
    try:
        record = store.get_run(run_id)
    except Exception as error:
        raise ReadOnlyVerificationError('OWNER_V1_SAVED_RUN_UNAVAILABLE') from error
    if record is None:
        raise ReadOnlyVerificationError('OWNER_V1_RUN_NOT_FOUND')
    try:
        restored, url = ensure_public_media_delivery(record, {'PUBLIC_MEDIA_DELIVERY_ORIGIN': public_origin})
    except Exception as error:
        raise ReadOnlyVerificationError('OWNER_V1_MEDIA_UNAVAILABLE') from error
    if restored != record:
        raise ReadOnlyVerificationError('OWNER_V1_MEDIA_ID_NOT_PERSISTED')
    proof = verify_owner_v1_facebook_delivery(record, buffer=buffer, facebook_channel_id=facebook_channel_id,
                                               expected_public_media_url=url)
    result = {'state': 'VERIFIED_READ_ONLY', 'cycle_number': proof.cycle_number, 'ayah_key': proof.ayah_key,
              'proof_sha256': proof.evidence_sha256, 'publication_enabled': False, 'writes_performed': False}
    if instagram_channel_id is not None:
        from engine.buffer_publisher import build_buffer_facebook_caption
        if not instagram_channel_id or instagram_channel_id == facebook_channel_id or not record.instagram_media_id:
            raise ReadOnlyVerificationError('OWNER_V1_INSTAGRAM_TARGET_MISSING')
        response = buffer.graphql('query ExistingInstagramPost($input: PostInput!) { post(input: $input) { '
            'id channelId channelService schedulingType status sentAt externalLink text assets { source } } }',
            {'input': {'id': record.instagram_media_id}})
        post = response.get('data', {}).get('post') if not response.get('errors') else None
        if (not isinstance(post, Mapping) or post.get('id') != record.instagram_media_id
                or post.get('channelId') != instagram_channel_id or post.get('channelService') != 'instagram'
                or post.get('schedulingType') != 'automatic' or post.get('text') != build_buffer_facebook_caption(record)
                or post.get('assets') != [{'source': url}]):
            raise ReadOnlyVerificationError('OWNER_V1_INSTAGRAM_IDENTITY_MISMATCH')
        if post.get('status') != 'sent' or not post.get('sentAt'):
            raise ReadOnlyVerificationError('OWNER_V1_INSTAGRAM_NOT_SENT')
        sent = datetime.fromisoformat(post['sentAt'].replace('Z', '+00:00'))
        if sent.tzinfo is None or not timedelta(0) <= datetime.now(timezone.utc) - sent <= timedelta(days=90):
            raise ReadOnlyVerificationError('OWNER_V1_INSTAGRAM_SENT_TIME_INVALID')
        link = post.get('externalLink')
        if not isinstance(link, str) or not re.fullmatch(r'https://(?:www\.)?instagram\.com/(?:reel|p)/[A-Za-z0-9_-]{3,64}/?', link):
            raise ReadOnlyVerificationError('OWNER_V1_INSTAGRAM_PERMALINK_UNCONFIRMED')
        result.update(facebook=asdict(proof), instagram={'status': 'sent', 'buffer_post_id': post['id'],
            'channel_id': post['channelId'], 'sent_at': post['sentAt'], 'permalink_url': link})
    return result


def main(argv: Sequence[str] | None = None, *, store: Any = None, buffer: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, help='Existing owner-v1 RunRecord ID')
    parser.add_argument('--both', action='store_true', help='Read matched sent evidence for both saved Buffer posts; never write.')
    args = parser.parse_args(argv)
    try:
        from engine.buffer_publisher import BufferClient, load_buffer_config_from_env
        from engine.supabase_state import SupabaseConfig, SupabaseStateStore
        env = os.environ
        if store is None:
            store = SupabaseStateStore(SupabaseConfig.from_env(_local_supabase_environment()))
        if buffer is None:
            buffer = BufferClient(load_buffer_config_from_env(env))
        result = verified_read_only_payload(args.run_id, store=store, buffer=buffer,
            facebook_channel_id=env.get('BUFFER_FACEBOOK_CHANNEL_ID', ''),
            public_origin=env.get('PUBLIC_MEDIA_DELIVERY_ORIGIN', ''),
            instagram_channel_id=env.get('BUFFER_INSTAGRAM_CHANNEL_ID', '') if args.both else None)
    except ReadOnlyVerificationError as error:
        code = str(error)
    except FacebookVerificationError as error:
        reason = str(error)
        # Whitelist diagnostic categories, not provider responses or secrets.
        code = reason if args.both and re.fullmatch(r'OWNER_V1_[A-Z_]{2,64}', reason) else 'OWNER_V1_BUFFER_DELIVERY_UNCONFIRMED'
    except Exception:
        code = 'OWNER_V1_READ_ONLY_CHECK_UNAVAILABLE'
    else:
        print(json.dumps(result, sort_keys=True))
        return 0
    print(json.dumps({'state': 'UNAVAILABLE', 'error_code': code, 'publication_enabled': False,
                      'writes_performed': False}, sort_keys=True))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
