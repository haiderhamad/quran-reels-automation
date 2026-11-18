import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCalmReelsFreshCycleFromSupabase } from "../lib/supabase-state";
import { CALM_REELS_FROZEN_SHA256 } from "../lib/calm-reels-fresh-cycle";

describe("Supabase fresh cycle read excludes every old published run", () => {
  beforeEach(() => {
    vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "test-only-server-key");
    vi.stubEnv("NODE_ENV", "production");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("reports current cycle 0/2,211 and next 1:1; never queries old run history", async () => {
    const paths: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      paths.push(String(url));
      if (String(url).includes("calm_reels_v1_cycle_state")) return new Response(JSON.stringify([{
        catalog_id: "calm-reels-editorial-shortlist-v1",
        catalog_sha256: CALM_REELS_FROZEN_SHA256,
        total_ayahs: 2211,
        cycle_number: 1,
        started_at: "2026-10-28T21:16:47Z",
      }]), { status: 200 });
      if (String(url).includes("calm_reels_v1_confirmed_posts")) return new Response("[]", { status: 200 });
      throw new Error("Attempt to load old publishing history");
    }));
    const result = await getCalmReelsFreshCycleFromSupabase();
    expect(result.status).toBe("ready");
    if (result.status !== "ready") throw new Error("Expected fresh cycle");
    expect(result.progress).toMatchObject({
      cycleNumber: 1, confirmedAyahs: 0, totalAyahs: 2211, nextAyahKey: "1:1",
    });
    expect(paths).toHaveLength(2);
    expect(paths.join(" ")).not.toMatch(/run_records|coverage_state|buffer_publication_claims/u);
  });

  it("reads six per-ayah receipts from just two verified Facebook Reels", async () => {
    const confirmed = ["1:1", "1:3", "1:4", "1:5", "1:6", "1:7"];
    const paths: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      paths.push(String(url));
      if (String(url).includes("calm_reels_v1_cycle_state")) return new Response(JSON.stringify([{
        catalog_id: "calm-reels-editorial-shortlist-v1",
        catalog_sha256: CALM_REELS_FROZEN_SHA256,
        total_ayahs: 2211, cycle_number: 1, started_at: "2026-10-28T21:16:47Z",
      }]), { status: 200 });
      if (String(url).includes("calm_reels_v1_confirmed_posts")) return new Response(JSON.stringify(
        confirmed.map((ayah_key) => ({
          ayah_key, run_id: ayah_key === "1:1" ? "first-run" : "reviewed-fatiha-run",
          media_key: "media/verified.mp4",
          external_post_id: ayah_key === "1:1" ? "123456789001" : "123456789002",
          evidence_sha256: "a".repeat(64),
          verified_at: "2026-11-02T13:01:02Z", cycle_number: 1,
        }))), { status: 200 });
      throw new Error("Unexpected historical ledger read");
    }));
    const result = await getCalmReelsFreshCycleFromSupabase();
    expect(result.status).toBe("ready");
    if (result.status !== "ready") throw new Error("Expected confirmed receipts");
    expect(result.progress).toMatchObject({
      confirmedAyahs: 6, totalAyahs: 2211, nextAyahKey: "2:2",
    });
    expect(paths).toHaveLength(2);
  });
  it("reports unavailable, NOT zero, on missing/mismatched active cycle", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[]", { status: 200 })));
    expect(await getCalmReelsFreshCycleFromSupabase()).toMatchObject({ status: "unavailable", progress: null });
  });
});
