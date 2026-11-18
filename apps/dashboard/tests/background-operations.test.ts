import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BACKGROUND_SAFETY_POLICY_FINGERPRINT as fp, getBackgroundPoolOperationsSummaryFromSupabase as pool, getBackgroundDiscoveryStateFromSupabase as discovery, getRecentBackgroundReuseFromSupabase as reuse, getBackgroundCandidateSummaryFromSupabase as queue } from "../lib/supabase-state";
const originalEnv = process.env;
function mockRows(tables: Record<string, any[]>) {
  const mock = vi.fn(async (url: string) => {
    const parsed = new URL(url), rows = tables[parsed.pathname.split("/").at(-1)!] || [];
    const offset = Number(parsed.searchParams.get("offset") || 0);
    return new Response(JSON.stringify(rows.slice(offset, offset + Number(parsed.searchParams.get("limit") || 1000))));
  });
  vi.stubGlobal("fetch", mock); return mock;
}
function asset(id: string, extra: Record<string, any> = {}) {
  return { provider: "pexels", provider_asset_id: id, provider_variant_id: "v1", content_sha256: "hash", policy_fingerprint: fp, status: "verified", value: { safety: { verdict: "SAFE", person_result: "CLEAR", face_result: "CLEAR", animal_verdict: "CLEAR", full_source_scan: true, policy_fingerprint: fp } }, ...extra };
}
beforeEach(() => { process.env = { ...originalEnv, NODE_ENV: "test", SUPABASE_URL: "https://fixture.invalid", SUPABASE_SERVICE_ROLE_KEY: "fixture", QURAN_REELS_ALLOW_LIVE_TEST_WRITES: "1" }; });
afterEach(() => { process.env = originalEnv; vi.unstubAllGlobals(); });
describe("background operations", () => {
  it("deduplicates variants, distinguishes providers, and excludes unsafe and stale records", async () => {
    mockRows({ background_assets: [asset("fresh"), asset("fresh", { provider_variant_id: "v2" }), asset("used"), asset("used", { provider: "pixabay" }), asset("old", { policy_fingerprint: "old" }), asset("stale", { status: "stale" }), asset("risk", { value: { ...asset("x").value, render_robustness: { risk_tier: "HIGH_RISK" } } }), asset("unsafe", { value: {} })], background_events: [{ event_type: "usage", provider: "pexels", provider_asset_id: "used" }] });
    expect(await pool()).toEqual({ available: true, total: 8, verified: 7, stale: 2, highRisk: 1, selectable: 3, unused: 2, used: 1 });
  });
  it("honors stale events", async () => {
    mockRows({ background_assets: [asset("a")], background_events: [{ event_type: "stale", value: { verified_record_id: `pexels:a:v1:hash:${fp}` } }] });
    expect(await pool()).toMatchObject({ stale: 1, selectable: 0 });
  });
  it("paginates queue totals", async () => {
    const mock = mockRows({ background_candidates: Array.from({ length: 501 }, () => ({ status: "PENDING" })) });
    expect(await queue()).toMatchObject({ total: 501, pending: 501 }); expect(mock).toHaveBeenCalledTimes(2);
  });
  it("reads durable discovery state independently of candidate creation", async () => {
    mockRows({ background_builder_runs: [{ value: { category: "water", next_category: "forest", completed_at: "2026-09-20T12:00:00Z", query_cursor: 2, page_cursor: 3 } }] });
    expect(await discovery()).toEqual({ category: "water", nextCategory: "forest", completedAt: "2026-09-20T12:00:00Z", queryCursor: 2, pageCursor: 3 });
  });
  it("reads explicit reuse provenance and marks unavailable data", async () => {
    mockRows({ run_records: [{ completed_at: "2026-09-20T12:00:00Z", value: { provenance: { background_reuse_warnings: [{ event: "BACKGROUND_REUSE_FALLBACK" }] } } }, { value: {} }] });
    expect(await reuse()).toEqual({ available: true, count: 1, lastAt: "2026-09-20T12:00:00Z" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    expect(await pool()).toMatchObject({ available: false }); expect(await discovery()).toBeNull(); expect(await reuse()).toMatchObject({ available: false });
  });
});
