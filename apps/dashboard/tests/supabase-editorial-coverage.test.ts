import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getEditorialCoverageFromSupabase, getEditorialScreeningFromSupabase } from "../lib/supabase-state";

describe("Supabase editorial coverage read", () => {
  beforeEach(() => {
    vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "test-only-key");
    vi.stubEnv("NODE_ENV", "production");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("calculates editorial counts from the current Supabase covered keys", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([
      { value: {
        cycle_number: 1,
        total_ayahs: 6236,
        covered_count: 3,
        covered_ayahs: ["2:81", "2:81", "2:82", "2:83"],
        last_updated_at: "2000-01-01T00:00:00Z",
      } },
    ]), { status: 200 })));

    const result = await getEditorialCoverageFromSupabase();
    expect(result.status).toBe("ready");
    if (result.status !== "ready") throw new Error("Expected ready coverage");
    expect(result.summary).toMatchObject({
      canonicalAyahs: 6236,
      eligibleAyahs: 0,
      eligibleCoveredAyahs: 0,
      eligibleRemainingAyahs: 0,
      historicallyCoveredAyahs: 3,
      policyVersion: "calm-inviting-v1",
    });
    expect(result.summary.refreshedAt).toBeTruthy();
    expect(result.coveredAyahKeys).toEqual(["2:81", "2:81", "2:82", "2:83"]);
  });

  it("reports a missing row without turning missing coverage into zero progress", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("[]", { status: 200 })));
    expect(await getEditorialCoverageFromSupabase()).toEqual({
      status: "missing",
      summary: null,
      error: null,
    });
  });

  it("reports a malformed response as an error rather than an empty table", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ value: {} }), { status: 200 })));
    const result = await getEditorialCoverageFromSupabase();
    expect(result.status).toBe("error");
    expect(result.summary).toBeNull();
  });

  it("rejects a Supabase record containing non-canonical verse keys", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([
      { value: {
        cycle_number: 1,
        covered_ayahs: ["2:157", "999:1"],
      } },
    ]), { status: 200 })));
    const result = await getEditorialCoverageFromSupabase();
    expect(result.status).toBe("error");
    expect(result.summary).toBeNull();
  });

  it("rejects a row without authoritative covered ayah keys", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([
      { value: { cycle_number: 1, covered_count: 0 } },
    ]), { status: 200 })));
    const result = await getEditorialCoverageFromSupabase();
    expect(result.status).toBe("error");
    expect(result.error).toContain("could not be verified");
    expect(result.summary).toBeNull();
  });

  it("reports transport failures as unavailable and does not leak response detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("private response detail")));
    const result = await getEditorialCoverageFromSupabase();
    expect(result.status).toBe("error");
    expect(result.error).toContain("could not be verified");
    expect(result.error).not.toContain("private response detail");
  });

  it("reads committed screening evidence separately from historical coverage", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        policy_version: "calm-inviting-v1",
        canonical_ayahs: 6236,
        screened_ayahs: 5,
        not_screened_ayahs: 6231,
        eligible_candidates: 2,
        ineligible_candidates: 2,
        ambiguous_candidates: 1,
        production_approved_ayahs: 3,
        automatic_production_approved_ayahs: 2,
        legacy_production_approved_ayahs: 1,
        production_approved_passages: 2,
        last_successful_screening_at: "2000-01-01T00:00:00Z",
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response("[]", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([
        { policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 81, passage_first_ayah: 81, passage_last_ayah: 82 },
        { policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 82, passage_first_ayah: 81, passage_last_ayah: 82 },
        { policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 83, passage_first_ayah: 83, passage_last_ayah: 83 },
      ]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([
        { surah_number: 2, passage_first_ayah: 81, passage_last_ayah: 82 },
        { surah_number: 2, passage_first_ayah: 83, passage_last_ayah: 83 },
      ]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(Array.from({ length: 2 }, () => ({
        preliminary_assessment: "AMBIGUOUS_REVIEW_REQUIRED",
        source_integrity_state: "UNVERIFIED",
        second_pass_state: "NOT_PERFORMED",
      }))), { status: 200 })));

    const result = await getEditorialScreeningFromSupabase();
    expect(result).toMatchObject({
      status: "ready",
      summary: {
        canonical_ayahs: 6236,
        screened_ayahs: 5,
        not_screened_ayahs: 6231,
        eligible_candidates: 2,
        production_approved_ayahs: 3,
        automatic_production_approved_ayahs: 2,
        legacy_production_approved_ayahs: 1,
        production_approved_passages: 2,
      },
      worker: null,
    });
    if (result.status === "ready") {
      expect(result.productionAuthorizedCandidateKeys).toHaveLength(3);
      expect(result.preliminaryDrafts).toEqual({ total: 2, awaiting_second_pass: 2, source_unverified: 2 });
    }
  });

  it("rejects a production authorization candidate outside its approved passage", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        policy_version: "calm-inviting-v1",
        canonical_ayahs: 6236,
        screened_ayahs: 1,
        not_screened_ayahs: 6235,
        eligible_candidates: 1,
        ineligible_candidates: 0,
        ambiguous_candidates: 0,
        production_approved_ayahs: 1,
        automatic_production_approved_ayahs: 1,
        legacy_production_approved_ayahs: 0,
        production_approved_passages: 1,
        last_successful_screening_at: null,
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response("[]", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        policy_version: "calm-inviting-v1",
        surah_number: 2,
        passage_first_ayah: 21,
        passage_last_ayah: 21,
        ayah_number: 22,
      }]), { status: 200 })));

    expect((await getEditorialScreeningFromSupabase()).status).toBe("unavailable");
  });

  it.each([
    { canonical_ayahs: 6235 }, { screened_ayahs: 2 }, { eligible_candidates: -1 },
    { production_approved_passages: 2 }, { production_approved_ayahs: 2 },
  ])("rejects inconsistent committed totals %j", async (override) => {
    const summary = {
      policy_version: "calm-inviting-v1", canonical_ayahs: 6236, screened_ayahs: 1,
      not_screened_ayahs: 6235, eligible_candidates: 1, ineligible_candidates: 0,
      ambiguous_candidates: 0, production_approved_ayahs: 1, automatic_production_approved_ayahs: 1,
      legacy_production_approved_ayahs: 0, production_approved_passages: 1,
      last_successful_screening_at: null, ...override,
    };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
      url.includes("editorial_screening_worker_summary") ? [summary]
        : url.includes("editorial_screening_worker_jobs") ? []
          : [{ policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 21, passage_first_ayah: 21, passage_last_ayah: 21 }],
    ), { status: 200 })));
    expect((await getEditorialScreeningFromSupabase()).status).toBe("unavailable");
  });
});
