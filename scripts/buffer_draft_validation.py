#!/usr/bin/env python3
"""Controlled non-public Buffer draft validation for one final-safe TEST run.

This script never calls Buffer's public share path.  It uses `saveToDraft`,
persists the temporary draft ID for recovery, and deletes the draft when the
explicit `--delete-after` flag is present.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from engine.buffer_publisher import (
    BufferClient,
    BufferPublisherError,
    BufferPublishRequest,
    build_buffer_facebook_caption,
    build_buffer_hashtags,
    load_buffer_config_from_env,
)
from engine.buffer_lifecycle import (
    DRAFT_CLEANUP_PENDING,
    DRAFT_CREATED,
    DRAFT_CREATING,
    DRAFT_DELETED,
    publication_fingerprint,
    publication_source,
)
from engine.public_media_delivery import PublicMediaDeliveryError, ensure_public_media_delivery
from engine.state_authority import get_active_state_store
from engine.storage import create_b2_client, load_b2_config_from_env


def _with_draft_audit(record: Any, status: str, **extra: str) -> Any:
    provenance = dict(record.provenance)
    provenance["buffer_draft_validation"] = {"status": status, **extra}
    return replace(record, provenance=provenance)


def _delete_recorded_draft(state_store: Any, client: BufferClient, validation_id: str, draft_id: str) -> None:
    """Delete only a durable validation-draft identity, or leave recovery state."""
    try:
        client.delete_post(draft_id)
    except BufferPublisherError as err:
        state_store.transition_buffer_draft_validation(
            validation_id, (DRAFT_CREATED, DRAFT_CLEANUP_PENDING), DRAFT_CLEANUP_PENDING,
            {"draft_post_id": draft_id, "last_error_code": err.code},
        )
        raise
    state_store.transition_buffer_draft_validation(
        validation_id, (DRAFT_CREATED, DRAFT_CLEANUP_PENDING), DRAFT_DELETED,
        {"draft_post_id": draft_id, "last_error_code": None},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and remove one Buffer Facebook Reel draft for a test RunRecord")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--create-draft", action="store_true", help="Required acknowledgement before any Buffer write")
    parser.add_argument("--delete-after", action="store_true", help="Delete the created Buffer draft after verification")
    parser.add_argument("--cleanup-only", action="store_true", help="Recover/delete an already-recorded validation draft; never create one")
    parser.add_argument("--print-caption", action="store_true", help="Print exact source-bound caption for reviewed validation")
    args = parser.parse_args(argv)
    if not args.create_draft and not args.cleanup_only:
        print("BUFFER_DRAFT_VALIDATION_BLOCKED: --create-draft is required")
        return 2

    try:
        b2_config = load_b2_config_from_env()
        state_store = get_active_state_store(b2_client=create_b2_client(b2_config), b2_config=b2_config)
        record = state_store.get_run(args.run_id)
        if record is None or record.run_kind != "test":
            raise BufferPublisherError("BUFFER_INVALID_POST", "Draft validation requires one existing TEST RunRecord.")
        if record.final_safety_passed is not True or not record.media_key:
            raise BufferPublisherError("BUFFER_INVALID_POST", "Draft validation requires final-safety-passed test media.")
        caption = build_buffer_facebook_caption(record)
        delivery_record, public_url = ensure_public_media_delivery(record)
        if delivery_record is not record:
            state_store.save_run(delivery_record)
            record = delivery_record
        if args.print_caption:
            print("BUFFER_CAPTION_PREVIEW_BEGIN")
            print(caption)
            print("BUFFER_CAPTION_PREVIEW_END")
        assert record.first_ayah is not None and record.last_ayah is not None and record.surah_name_en is not None
        print(f"BUFFER_DRAFT_TARGET_RUN_ID={record.run_id}")
        print(f"BUFFER_DRAFT_MEDIA_HOST={urlparse(public_url).hostname}")
        print(f"BUFFER_DRAFT_CAPTION_UTF16_UNITS={len(caption.encode('utf-16-le')) // 2}")
        print("BUFFER_DRAFT_HASHTAGS=" + " ".join(build_buffer_hashtags(record.surah_name_en, record.first_ayah, record.last_ayah)))

        if not all(hasattr(state_store, name) for name in (
            "get_buffer_draft_validation", "claim_buffer_draft_validation", "transition_buffer_draft_validation"
        )):
            raise BufferPublisherError("BUFFER_DRAFT_STATESTORE_REQUIRED", "Draft validation requires the Supabase lifecycle store.")
        client = BufferClient(load_buffer_config_from_env())
        organization, channel = client.resolve_facebook_channel()
        fingerprint = publication_fingerprint(
            run_id=record.run_id, platform="facebook", channel_id=channel.id,
            media_identity=str(record.provenance.get("media_sha256") or record.media_key), caption=caption,
        )
        validation_id = f"draft_{fingerprint[:40]}"
        source = publication_source(record.run_id, fingerprint, draft=True)
        existing = state_store.get_buffer_draft_validation(validation_id)
        if existing is None:
            existing = state_store.claim_buffer_draft_validation({
                "validation_id": validation_id, "run_id": record.run_id, "channel_id": channel.id,
                "buffer_source": source, "state": DRAFT_CREATING,
            })
        claim_was_created = bool(existing.get("_claim_created"))
        value = existing.get("value") if isinstance(existing.get("value"), dict) else existing
        draft_id = existing.get("draft_post_id") or value.get("draft_post_id")
        existing_state = str(existing.get("state") or value.get("state") or "")
        if existing_state == DRAFT_DELETED:
            # A completed temporary draft is an idempotent cleanup success.
            print("BUFFER_DRAFT_DELETED=YES")
            return 0

        # Recovery only ever acts on a deterministic *draft* state row.  It
        # matches Buffer's documented channel/text/video fields and restricts
        # history to draft status, so it can never target a public Reel.
        if not draft_id and not claim_was_created and existing_state in {DRAFT_CREATING, DRAFT_CLEANUP_PENDING}:
            recovered = client.reconcile_publication(
                organization.id,
                channel.id,
                caption,
                public_url,
                not_before=str(value.get("created_at") or existing.get("created_at") or "") or None,
                drafts_only=True,
            )
            if recovered.outcome == "FOUND":
                draft_id = recovered.post_id
                state_store.transition_buffer_draft_validation(
                    validation_id, (DRAFT_CREATING, DRAFT_CLEANUP_PENDING), DRAFT_CREATED,
                    {"draft_post_id": draft_id},
                )
            else:
                state_store.transition_buffer_draft_validation(
                    validation_id, (DRAFT_CREATING, DRAFT_CLEANUP_PENDING), DRAFT_CLEANUP_PENDING,
                    {"last_error_code": "BUFFER_DRAFT_RECOVERY_INDETERMINATE" if recovered.outcome == "INDETERMINATE" else "BUFFER_DRAFT_RECOVERY_ABSENT"},
                )
                print("BUFFER_DRAFT_CLEANUP_PENDING=YES")
                return 1

        if args.cleanup_only and not draft_id:
            print("BUFFER_DRAFT_CLEANUP_PENDING=YES")
            return 1
        if not draft_id and claim_was_created:
            # The create request is made once.  A transport ambiguity remains
            # cleanup-pending and is reconciled on a later explicit run.
            try:
                created = client.create_facebook_reel(channel.id, BufferPublishRequest(
                    run_id=record.run_id, caption=caption, public_media_url=public_url,
                    save_to_draft=True, source=source,
                ))
            except BufferPublisherError as err:
                if err.possibly_dispatched:
                    state_store.transition_buffer_draft_validation(
                        validation_id, (DRAFT_CREATING,), DRAFT_CLEANUP_PENDING,
                        {"last_error_code": err.code},
                    )
                raise
            draft_id = created.id
            checkpoint = state_store.transition_buffer_draft_validation(
                validation_id, (DRAFT_CREATING,), DRAFT_CREATED, {"draft_post_id": draft_id},
            )
            if checkpoint is None:
                # Buffer may already hold the draft, but without a durable ID
                # no cleanup can safely continue in this process.  A later
                # reconciliation uses the documented channel/text/video
                # identity rather than risking a second create.
                state_store.transition_buffer_draft_validation(
                    validation_id, (DRAFT_CREATING, DRAFT_CLEANUP_PENDING), DRAFT_CLEANUP_PENDING,
                    {"last_error_code": "BUFFER_DRAFT_SUCCESS_CHECKPOINT_PENDING"},
                )
                raise BufferPublisherError(
                    "BUFFER_DRAFT_SUCCESS_CHECKPOINT_PENDING",
                    "Buffer accepted a draft but its durable cleanup checkpoint did not persist.",
                )
            record = _with_draft_audit(record, "CREATED", buffer_post_id=draft_id,
                organization_id=organization.id, channel_id=channel.id, channel_name=channel.display_name)
            # Once Buffer has accepted the draft, the ID is already durable in
            # its dedicated state row.  If this later audit write crashes, a
            # finally cleanup still removes it when temporary cleanup is asked.
            if args.delete_after:
                try:
                    state_store.save_run(record)
                finally:
                    _delete_recorded_draft(state_store, client, validation_id, str(draft_id))
                    print("BUFFER_DRAFT_DELETED=YES")
                return 0
            state_store.save_run(record)
            print(f"BUFFER_DRAFT_CREATED_ID={draft_id}")

        if not draft_id:
            # An existing claim never reaches a new mutation merely because
            # an in-progress/orphaned draft is currently absent from history.
            # That absence is not enough to distinguish a concurrent request
            # from a crashed process, so preserve durable cleanup recovery.
            print("BUFFER_DRAFT_CLEANUP_PENDING=YES")
            return 1

        if args.delete_after or args.cleanup_only:
            _delete_recorded_draft(state_store, client, validation_id, str(draft_id))
            state_store.save_run(_with_draft_audit(record, "DELETED", buffer_post_id=str(draft_id)))
            print("BUFFER_DRAFT_DELETED=YES")
        return 0
    except (BufferPublisherError, PublicMediaDeliveryError) as err:
        print(f"BUFFER_DRAFT_VALIDATION_FAILED={getattr(err, 'code', type(err).__name__)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
