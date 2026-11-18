vi.mock("../../../data/reel_theme_catalog.json", () => import("./fixtures/editorial-catalog"));
vi.mock("../lib/editorial-reconciliation", () => import("./fixtures/reconciliation"));
import React from "react";
import { createHash } from "node:crypto";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RecentApprovedReels } from "../components/RecentApprovedReels";
import { isEligibleEditorialRange } from "../lib/editorial-coverage";
import { isApprovedDeliveredReel } from "../lib/approved-reels";
import { RunRecordSchema } from "../lib/schema";
import { listRecentApprovedReelsFromSupabase } from "../lib/supabase-state";
import reconciliation from "./fixtures/reconciliation";
import { computePublishingProgress } from "../lib/publishing-progress";

const arabic = "نموذج مستقل لاختبار العرض";
const english = "An independent display test sample.";
const hash = (text: string) => createHash("sha256").update(text.trim().split(/\s+/u).join(" ")).digest("hex");

function approvedRun(overrides: Record<string, unknown> = {}) {
  const ah = hash(arabic);
  const eh = hash(english);
  return RunRecordSchema.parse({
    run_id: "approved-1",
    date: "2026-10-09",
    slot: "morning",
    run_kind: "scheduled",
    status: "SUCCESS",
    started_at: "2026-10-09T07:00:00Z",
    completed_at: "2026-10-09T07:05:00Z",
    surah_number: 2,
    first_ayah: 21,
    last_ayah: 21,
    surah_name_en: "Al-Baqarah",
    final_safety_passed: true,
    media_key: "reels/2026/10/approved-1.mp4",
    facebook_status: "SUCCESS",
    facebook_media_id: "fb-1",
    translation_languages: ["en"],
    translation_sources: { en: { translation_key: "english_rwwad", source: "QuranEnc", version: "1.0.19" } },
    provenance: {
      theme_policy_version: "calm-inviting-v1",
      quran_source: "Quran Foundation Content API v4 (text_uthmani)",
      editorial_reconciliation_policy_version: "editorial-reconciliation-v1",
      editorial_reconciliation_status: "APPROVED_AT_GENERATION",
      caption_ayahs: [{ verse_key: "2:21", arabic_text: arabic, arabic_normalized_sha256: ah,
        translations: { en: { text: english, normalized_sha256: eh, translation_key: "english_rwwad", translation_version: "1.0.19" } } }],
      subtitle_ayahs: [{ verse_key: "2:21", arabic_source_hash: ah, english_source_hash: eh }],
    },
    ...overrides,
  });
}

function historicalKeepRun() {
  const texts = ["سطر تجريبي أول", "سطر تجريبي ثان", "سطر تجريبي ثالث"];
  const base = approvedRun();
  const caption = texts.map((text, index) => ({
    ...base.provenance.caption_ayahs[0], verse_key: `2:${index + 81}`,
    arabic_text: text, arabic_normalized_sha256: hash(text),
  }));
  return approvedRun({
    run_id: "fixture-keep", surah_number: 2, first_ayah: 81, last_ayah: 83,
    started_at: "2026-09-20T11:55:20Z", completed_at: "2026-09-20T12:00:00Z",
    facebook_media_id: "101_202",
    provenance: { ...base.provenance, editorial_reconciliation_status: undefined,
      caption_ayahs: caption,
      subtitle_ayahs: caption.map((ayah) => ({
        verse_key: ayah.verse_key, arabic_source_hash: ayah.arabic_normalized_sha256,
        english_source_hash: hash(english),
      })),
    },
  });
}

describe("Recent Approved Reels", () => {
  it("counts newly authorized exact ranges outside the legacy catalog only with completed delivery", () => {
    const original = approvedRun();
    const provenance = {
      ...original.provenance,
      editorial_reconciliation_status: undefined,
      production_authorization: {
        policy_version: "calm-inviting-v1", surah_number: 2, first_ayah: 22, last_ayah: 22,
        authorization_ids: [99], authorization_fingerprint: "a".repeat(32),
      },
      caption_ayahs: original.provenance.caption_ayahs.map((row: Record<string, unknown>) => ({ ...row, verse_key: "2:22" })),
      subtitle_ayahs: original.provenance.subtitle_ayahs.map((row: Record<string, unknown>) => ({ ...row, verse_key: "2:22" })),
    };
    const run = approvedRun({ run_id: "new-automatic-approval", first_ayah: 22, last_ayah: 22, provenance });
    const ranges = new Set(["2:22-22"]);
    expect(isApprovedDeliveredReel(run)).toBe(false);
    expect(isApprovedDeliveredReel(run, ranges)).toBe(true);
    expect(isApprovedDeliveredReel(run, new Set())).toBe(false);
    expect(isApprovedDeliveredReel({ ...run, facebook_status: "ACCEPTED" }, ranges)).toBe(false);
    const summary = computePublishingProgress([run], [], { facebook: true, instagram: false }, new Set(["2:22"]), ranges);
    expect(summary.historicallyVerifiedApprovedAyahs).toBe(1);
    expect(summary.historicallyVerifiedAyahKeys).toEqual(["2:22"]);
    expect(summary.approvedAyahsWithoutVerifiedHistoricalDelivery).toBe(0);
    expect(summary.liveVerifiedPublishedAyahs).toBeNull();
  });
  it("accepts source-bound production delivery with per-record evidence", () => {
    expect(isApprovedDeliveredReel(approvedRun())).toBe(true);
    expect(isApprovedDeliveredReel(approvedRun({ facebook_status: "ACCEPTED", facebook_media_id: null,
      instagram_status: "SUCCESS", instagram_media_id: "ig-1" }))).toBe(true);
  });

  it("excludes tests, failures, missing safety and uncertain publication", () => {
    for (const change of [
      { run_kind: "test" }, { status: "FAILED" }, { final_safety_passed: false },
      { media_key: null }, { facebook_status: "ACCEPTED" },
      { facebook_status: "UNKNOWN" }, { facebook_media_id: null },
      { provenance: { ...approvedRun().provenance, editorial_reconciliation_status: undefined } },
    ]) expect(isApprovedDeliveredReel(approvedRun(change))).toBe(false);
  });

  it("blocks a confirmed retraction even when the RunRecord is otherwise valid", () => {
    const run = approvedRun({ run_id: "fixture-retraction" });
    expect(isApprovedDeliveredReel(run)).toBe(false);
    expect(isApprovedDeliveredReel({ ...run, facebook_status: "FAILED",
      instagram_status: "SUCCESS", instagram_media_id: "ig-1" })).toBe(false);
  });

  it("accepts an old audited KEEP as historical delivery with bound identity", () => {
    expect(isApprovedDeliveredReel(historicalKeepRun())).toBe(true);
  });

  it("rejects an audited delivery when its platform or content identity changes", () => {
    const run = historicalKeepRun();
    expect(isApprovedDeliveredReel({ ...run, facebook_media_id: "different" })).toBe(false);
    expect(isApprovedDeliveredReel({ ...run, facebook_status: "FAILED",
      instagram_status: "SUCCESS", instagram_media_id: run.facebook_media_id })).toBe(false);
    expect(isApprovedDeliveredReel({ ...run, surah_number: 2, first_ayah: 21, last_ayah: 21,
      provenance: approvedRun().provenance })).toBe(false);
    const caption = run.provenance.caption_ayahs.map((ayah: Record<string, unknown>) => ({
      ...ayah, arabic_text: arabic, arabic_normalized_sha256: hash(arabic),
    }));
    expect(isApprovedDeliveredReel({ ...run, provenance: { ...run.provenance,
      caption_ayahs: caption, subtitle_ayahs: caption.map((ayah: Record<string, unknown>) => ({
        verse_key: ayah.verse_key, arabic_source_hash: hash(arabic), english_source_hash: hash(english),
      })),
    } })).toBe(false);
  });

  it("rejects unknown records without generation approval and mismatched policies", () => {
    for (const provenance of [
      { ...approvedRun().provenance, editorial_reconciliation_status: "DELIVERY_RECORDED" },
      { ...approvedRun().provenance, editorial_reconciliation_policy_version: "unreviewed" },
      { ...approvedRun().provenance, theme_policy_version: "old-policy" },
    ]) expect(isApprovedDeliveredReel(approvedRun({ provenance }))).toBe(false);
  });

  it("does not upgrade an audited Buffer sending record with a new marker", () => {
    expect(isApprovedDeliveredReel(approvedRun({ run_id: "fixture-sending" }))).toBe(false);
  });

  it("keeps an audited REVIEW blocked despite complete technical delivery evidence", () => {
    expect(isApprovedDeliveredReel(approvedRun({ run_id: "fixture-review" }))).toBe(false);
  });

  it("blocks every audited negative identity and prevents reuse under another run ID", () => {
    for (const entry of reconciliation.entries.filter((item) => item.editorial_verdict !== "KEEP")) {
      expect(isApprovedDeliveredReel(approvedRun({ run_id: entry.run_id })), entry.key).toBe(false);
      if (entry.platform_media_id) {
        const platform = entry.platform === "facebook"
          ? { facebook_media_id: entry.platform_media_id }
          : { facebook_status: "FAILED", instagram_status: "SUCCESS", instagram_media_id: entry.platform_media_id };
        expect(isApprovedDeliveredReel(approvedRun(platform)), entry.key).toBe(false);
      }
    }
  });

  it("blocks negative Facebook URL identities across host and tracking variants", () => {
    const entry = reconciliation.entries.find((item) => item.platform === "facebook"
      && item.editorial_verdict === "DELETE_CONFIRMED" && item.public_url)!;
    const source = new URL(entry.public_url!);
    for (const host of ["facebook.com", "www.facebook.com", "m.facebook.com"]) {
      for (const suffix of ["?tracking=historical", "#history"]) {
        const facebook_reel_url = `https://${host}${source.pathname}${suffix}`;
        expect(isApprovedDeliveredReel(approvedRun({ facebook_media_id: null, facebook_reel_url }))).toBe(false);
      }
    }
  });

  it("rejects arbitrary URLs as sole Facebook delivery evidence", () => {
    for (const facebook_reel_url of [
      "not-a-url", "https://example.com/reel/123/", "https://facebook.com.evil.example/reel/123/",
      "https://www.facebook.com/profile.php?id=123", "https://facebook.com/reel/not-an-id/",
      "http://facebook.com/reel/123/", "https://user" + "@" + "facebook.com/reel/123/",
    ]) expect(isApprovedDeliveredReel({ ...approvedRun(), facebook_media_id: null, facebook_reel_url })).toBe(false);
  });

  it("preserves historical canonical Facebook delivery URLs and harmless variants", () => {
    const run = historicalKeepRun();
    for (const facebook_reel_url of [
      "https://facebook.com/101_202",
      "https://www.facebook.com/101_202/?tracking=history",
      "https://m.facebook.com/101/posts/202/#history",
    ]) expect(isApprovedDeliveredReel({ ...run, facebook_media_id: null, facebook_reel_url })).toBe(true);
    expect(isApprovedDeliveredReel(approvedRun({ facebook_media_id: null,
      facebook_reel_url: "https://www.facebook.com/reel/9999999999999/?tracking=history" }))).toBe(true);
  });

  it("excludes ineligible, context-poor, removed and review records", () => {
    expect(isApprovedDeliveredReel(approvedRun({ first_ayah: 7, last_ayah: 7 }))).toBe(false);
    expect(isEligibleEditorialRange(2, 82, 82)).toBe(false);
    expect(isEligibleEditorialRange(2, 81, 82)).toBe(true);
    for (const editorial_cleanup_status of ["REMOVED", "REVIEW", "DELETE_CONFIRMED"]) {
      const run = approvedRun();
      expect(isApprovedDeliveredReel({ ...run, provenance: { ...run.provenance, editorial_cleanup_status } })).toBe(false);
    }
  });

  it("rejects missing and mismatched source provenance", () => {
    const run = approvedRun();
    for (const provenance of [
      {},
      { ...run.provenance, caption_ayahs: [] },
      { ...run.provenance, caption_ayahs: [{ ...run.provenance.caption_ayahs[0], arabic_text: "changed" }] },
      { ...run.provenance, subtitle_ayahs: [{ ...run.provenance.subtitle_ayahs[0], english_source_hash: "bad" }] },
    ]) expect(isApprovedDeliveredReel({ ...run, provenance })).toBe(false);
  });

  it("labels historical delivery without claiming a current live platform state", () => {
    const html = renderToStaticMarkup(<RecentApprovedReels load={{ status: "ready", records: [approvedRun()] }} />);
    expect(html).toContain("Recent Approved Reels");
    expect(html).toContain("Facebook delivered");
    expect(html).toContain("Current platform presence is not verified");
    expect(html).not.toContain("Currently live");
  });

  it("describes insufficient reconciliation evidence without implying a date cutoff", () => {
    const html = renderToStaticMarkup(<RecentApprovedReels load={{ status: "ready", records: [] }} />);
    expect(html).toContain("No eligible deliveries have sufficient reconciliation evidence");
    expect(html).not.toContain("since the editorial cleanup");
  });
});

describe("Supabase approved history pagination", () => {
  beforeEach(() => {
    vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "test-only-key");
    vi.stubEnv("NODE_ENV", "production");
  });
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

  it("filters before final limit and requests only bounded production candidates", async () => {
    const invalid = approvedRun({ facebook_status: "ACCEPTED", facebook_media_id: null });
    const first = Array.from({ length: 50 }, (_, i) => ({ value: { ...invalid, run_id: `invalid-${i}` } }));
    const valid = approvedRun({ run_id: "later-valid" });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{ value: valid }, { value: valid }]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await listRecentApprovedReelsFromSupabase(2);
    expect(result.status).toBe("ready");
    expect(result.records.map((record) => record.run_id)).toEqual(["later-valid"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const firstUrl = new URL(fetchMock.mock.calls[0][0]);
    const secondUrl = new URL(fetchMock.mock.calls[1][0]);
    expect(firstUrl.searchParams.get("run_kind")).toBe("in.(scheduled,manual_production)");
    expect(firstUrl.searchParams.get("status")).toBe("eq.SUCCESS");
    expect(firstUrl.searchParams.get("completed_at")).toBeNull();
    expect(firstUrl.searchParams.get("limit")).toBe("50");
    expect(secondUrl.searchParams.get("offset")).toBe("50");
  });

  it("returns a clear empty result and fails closed on transport errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
    expect(await listRecentApprovedReelsFromSupabase()).toEqual({ status: "ready", records: [] });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("private detail")));
    expect(await listRecentApprovedReelsFromSupabase()).toEqual({
      status: "error", records: [], error: "Approved Reel history is unavailable. Technical Run History remains accessible.",
    });
  });
});
