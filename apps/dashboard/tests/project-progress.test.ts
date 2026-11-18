vi.mock("../../../data/reel_theme_catalog.json", () => import("./fixtures/editorial-catalog"));
vi.mock("../lib/editorial-reconciliation", () => import("./fixtures/reconciliation"));
import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { RunRecordSchema } from "../lib/schema";
import { computeEditorialProgress } from "../lib/editorial-progress";
import { computePublishingProgress } from "../lib/publishing-progress";

const arabic = "نموذج مستقل لاختبار العرض";
const english = "An independent display test sample.";
const normalizedHash = (value: string) => createHash("sha256").update(value.trim().split(/\s+/u).join(" ")).digest("hex");

function approvedRun(overrides: Record<string, unknown> = {}) {
  const arabicHash = normalizedHash(arabic);
  const englishHash = normalizedHash(english);
  return RunRecordSchema.parse({
    run_id: "verified-delivery",
    date: "2026-10-09",
    slot: "morning",
    run_kind: "scheduled",
    status: "SUCCESS",
    started_at: "2026-10-09T07:00:00Z",
    completed_at: "2026-10-09T07:05:00Z",
    surah_number: 2,
    first_ayah: 21,
    last_ayah: 21,
    final_safety_passed: true,
    media_key: "reels/2026/10/verified-delivery.mp4",
    facebook_status: "SUCCESS",
    facebook_media_id: "fb-verified-id",
    instagram_status: "SUCCESS",
    instagram_media_id: "ig-verified-id",
    translation_languages: ["en"],
    translation_sources: { en: { translation_key: "english_rwwad", source: "QuranEnc", version: "1.0.19" } },
    provenance: {
      theme_policy_version: "calm-inviting-v1",
      quran_source: "Quran Foundation Content API v4 (text_uthmani)",
      editorial_reconciliation_policy_version: "editorial-reconciliation-v1",
      editorial_reconciliation_status: "APPROVED_AT_GENERATION",
      caption_ayahs: [{
        verse_key: "2:21",
        arabic_text: arabic,
        arabic_normalized_sha256: arabicHash,
        translations: { en: {
          text: english,
          normalized_sha256: englishHash,
          translation_key: "english_rwwad",
          translation_version: "1.0.19",
        } },
      }],
      subtitle_ayahs: [{ verse_key: "2:21", arabic_source_hash: arabicHash, english_source_hash: englishHash }],
    },
    ...overrides,
  });
}

function claim(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "verified-delivery",
    platform: "facebook" as const,
    publisher_backend: "buffer" as const,
    channel_id: "facebook-channel",
    state: "SUCCESS" as const,
    buffer_post_id: "fb-verified-id",
    buffer_post_status: "sent",
    external_link: "https://www.facebook.com/reel/1234567890123456",
    accepted_at: "2026-10-09T07:00:00Z",
    last_error_code: null,
    updated_at: "2026-10-09T07:05:00Z",
    ...overrides,
  };
}

describe("whole-project editorial and publishing metrics", () => {
  it("requires every recorded approval to appear in the matching operator catalog", () => {
    expect(() => computeEditorialProgress(
      [{ canonical_key: "2:21", editorial_eligibility_status: "APPROVED_REVIEWED" }],
      new Set(), 1,
    )).toThrow("identities do not match");
  });

  it("expands approved counts dynamically when reviewed catalog identities are added", () => {
    const initialRecords = [
      { canonical_key: "1:1", editorial_eligibility_status: "APPROVED_REVIEWED" as const },
      { canonical_key: "1:2", editorial_eligibility_status: "REVIEW_REQUIRED" as const },
      { canonical_key: "1:3", editorial_eligibility_status: "NOT_YET_REVIEWED" as const },
    ];
    const before = computeEditorialProgress(initialRecords, new Set(["1:1"]), 3, "calm-inviting-test-v1", 1);
    const after = computeEditorialProgress(
      initialRecords.map((row) => ({ ...row, editorial_eligibility_status: "APPROVED_REVIEWED" as const })),
      new Set(["1:1", "1:2", "1:3"]),
      3,
      "calm-inviting-test-v1",
      2,
    );
    expect(before).toMatchObject({ approvedAyahs: 1, requiresReviewAyahs: 1, notYetReviewedAyahs: 1, reviewCompletenessPercentage: 33.33, catalogRanges: 1 });
    expect(after).toMatchObject({ approvedAyahs: 3, requiresReviewAyahs: 0, notYetReviewedAyahs: 0, reviewCompletenessPercentage: 100, catalogRanges: 2 });
  });

  it("deduplicates a verse delivered on both providers and never equates it with current live presence", () => {
    const record = approvedRun({ publisher_backend: "buffer", buffer_post_id: "buffer-post-id" });
    const summary = computePublishingProgress(
      [record],
      [claim(), claim({ platform: "instagram", channel_id: "instagram-channel", buffer_post_id: "ig-verified-id" })],
      { facebook: true, instagram: true },
    );
    expect(summary).toMatchObject({
      approvedAyahs: 4,
      historicallyVerifiedApprovedAyahs: 1,
      facebookHistoricallyVerifiedAyahs: 1,
      instagramHistoricallyVerifiedAyahs: 1,
      approvedAyahsWithoutVerifiedHistoricalDelivery: 3,
      liveVerifiedPublishedAyahs: null,
      liveRemainingApprovedAyahs: null,
      livePublicationPercentage: null,
      unverifiedDeliveryOutcomes: 0,
    });
    expect(summary.platforms.facebook.historicallyVerifiedDeliveryRecords).toBe(1);
    expect(summary.platforms.instagram.historicallyVerifiedDeliveryRecords).toBe(1);
    expect(summary.platforms.facebook.lastConfirmedHistoricalDeliveryAt).toBe("2026-10-09T07:05:00Z");
    expect(summary.platforms.instagram.lastConfirmedHistoricalDeliveryAt).toBe("2026-10-09T07:05:00Z");
  });

  it("uses the supplied live automatic-approval identities instead of the static legacy catalog", () => {
    const record = approvedRun({
      surah_number: 3,
      first_ayah: 8,
      last_ayah: 8,
      provenance: {
        ...approvedRun().provenance,
        editorial_reconciliation_status: undefined,
        production_authorization: {
          policy_version: "calm-inviting-v1", surah_number: 3, first_ayah: 8, last_ayah: 8,
          authorization_ids: [1], authorization_fingerprint: "a".repeat(32),
        },
        caption_ayahs: [{
          ...approvedRun().provenance.caption_ayahs[0],
          verse_key: "3:8",
        }],
        subtitle_ayahs: [{
          ...approvedRun().provenance.subtitle_ayahs[0],
          verse_key: "3:8",
        }],
      },
    });
    const summary = computePublishingProgress(
      [record],
      [claim()],
      { facebook: true, instagram: false },
      new Set(["3:8"]),
      new Set(["3:8-8"]),
    );
    expect(summary).toMatchObject({
      approvedAyahs: 1,
      historicallyVerifiedApprovedAyahs: 1,
      approvedAyahsWithoutVerifiedHistoricalDelivery: 0,
    });
  });

  it("keeps coverage_state, unverified claims, confirmed removals, and retraction-required outcomes separate", () => {
    const summary = computePublishingProgress(
      [],
      [
        claim({ run_id: "no-run-evidence", state: "UNKNOWN_OUTCOME", buffer_post_status: null, external_link: null }),
        claim({ run_id: "fixture-retraction", platform: "facebook", state: "UNKNOWN_OUTCOME" }),
        claim({ run_id: "fixture-retraction", platform: "instagram", state: "UNKNOWN_OUTCOME" }),
      ],
      { facebook: true, instagram: true },
    );
    expect(summary.unverifiedDeliveryOutcomes).toBe(2);
    expect(summary.reconciliationUncertainReviews).toBe(2);
    expect(summary.reconciliationRetractionsRequired).toBe(1);
    expect(summary.reconciliationUnresolvedOutcomes).toBe(3);
    expect(summary.confirmedRetractionOutcomes).toBe(1);
    expect(summary.liveVerifiedPublishedAyahs).toBeNull();
  });

  it("does not treat a SUCCESS Buffer claim in sending state as a verified platform delivery", () => {
    const record = approvedRun({ publisher_backend: "buffer" });
    const summary = computePublishingProgress(
      [record],
      [claim({ buffer_post_status: "sending" })],
      { facebook: true, instagram: true },
    );
    expect(summary.historicallyVerifiedApprovedAyahs).toBe(0);
    expect(summary.facebookHistoricallyVerifiedAyahs).toBe(0);
    expect(summary.unverifiedDeliveryOutcomes).toBe(1);
  });

  it("requires a matching sent Buffer claim when the platform run remains ACCEPTED", () => {
    const record = approvedRun({
      publisher_backend: "buffer",
      facebook_status: "ACCEPTED",
      buffer_post_id: "fb-verified-id",
    });
    const sent = computePublishingProgress([record], [claim()], { facebook: true, instagram: false });
    expect(sent.facebookHistoricallyVerifiedAyahs).toBe(1);
    expect(sent.unverifiedDeliveryOutcomes).toBe(0);

    const sending = computePublishingProgress(
      [record], [claim({ buffer_post_status: "sending" })], { facebook: true, instagram: false },
    );
    expect(sending.facebookHistoricallyVerifiedAyahs).toBe(0);
    expect(sending.unverifiedDeliveryOutcomes).toBe(1);
  });

  it("fails closed when one run has multiple Buffer channel claims for the same platform", () => {
    const record = approvedRun({ publisher_backend: "buffer", buffer_post_id: "buffer-post-id" });
    const summary = computePublishingProgress(
      [record],
      [claim(), claim({ channel_id: "second-facebook-channel", buffer_post_id: "second-buffer-post-id" })],
      { facebook: true, instagram: false },
    );
    expect(summary.facebookHistoricallyVerifiedAyahs).toBe(0);
    expect(summary.historicallyVerifiedApprovedAyahs).toBe(0);
    expect(summary.unverifiedDeliveryOutcomes).toBe(2);
  });

  it("uses the newest verified platform timestamp or sent Buffer claim update", () => {
    const latest = approvedRun({
      run_id: "latest-delivery-without-platform-time",
      date: "2026-10-09",
      completed_at: "2026-10-09T07:05:00Z",
      publisher_backend: "buffer",
      buffer_post_id: "latest-buffer-id",
      facebook_media_id: "latest-buffer-id",
      facebook_published_at: null,
    });
    const earlier = approvedRun({
      run_id: "older-delivery-with-platform-time",
      date: "2026-10-08",
      completed_at: "2026-10-08T07:05:00Z",
      publisher_backend: "buffer",
      buffer_post_id: "older-buffer-id",
      facebook_media_id: "older-buffer-id",
      facebook_published_at: "2026-10-08T07:02:00Z",
    });
    const summary = computePublishingProgress(
      [earlier, latest],
      [
        claim({
          run_id: "older-delivery-with-platform-time",
          buffer_post_id: "older-buffer-id",
          updated_at: "2026-10-08T07:10:00Z",
        }),
        claim({
          run_id: "latest-delivery-without-platform-time",
          buffer_post_id: "latest-buffer-id",
          updated_at: "2026-10-09T07:06:00Z",
        }),
      ],
      { facebook: true, instagram: false },
    );
    expect(summary.platforms.facebook.lastConfirmedHistoricalDeliveryAt).toBe("2026-10-09T07:06:00Z");
  });

  it("reports channel-specific failures and does not mark configured Instagram healthy", () => {
    const latest = approvedRun({
      run_id: "instagram-channel-missing",
      date: "2026-10-09",
      started_at: "2026-10-09T07:00:00Z",
      completed_at: "2026-10-09T07:03:00Z",
      status: "SUCCESS",
      facebook_status: "ACCEPTED",
      facebook_media_id: null,
      instagram_status: "FAILED",
      instagram_media_id: null,
      instagram_failure_reason: "INSTAGRAM_CHANNEL_NOT_VISIBLE: No Instagram channel is visible to Buffer.",
    });
    const summary = computePublishingProgress([latest], [], { facebook: true, instagram: true });
    expect(summary.platforms.instagram).toMatchObject({
      configured: true,
      bufferReachability: "Not verified",
      operationalHealth: "Degraded",
      latestAttempt: {
        status: "FAILED",
        errorCode: "INSTAGRAM_CHANNEL_NOT_VISIBLE",
        occurredAt: "2026-10-09T07:03:00Z",
      },
    });
    expect(summary.platforms.facebook.operationalHealth).toBe("Awaiting confirmation");
    expect(summary.platforms.facebook.historicallyVerifiedDeliveryRecords).toBe(0);
  });
});
