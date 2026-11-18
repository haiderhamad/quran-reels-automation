import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CANONICAL_SURAH_AYAH_COUNTS } from "../lib/canonical-quran";
import { editorialPagination, validateSurahProgress } from "../lib/editorial-review";
import { getEditorialAyahsFromSupabase, LIVE_TEST_WRITE_OPT_IN_ENV } from "../lib/supabase-state";
import { sourceEvidenceHash } from "../lib/editorial-evidence-audit";

function progress() {
  return CANONICAL_SURAH_AYAH_COUNTS.map((count, index) => ({ surah_number: index + 1,
    total_ayahs: count, screened_ayahs: 0, remaining_ayahs: count, eligible_candidates: 0, production_approved_ayahs: 0 }));
}

describe("canonical surah overview", () => {
  it("requires all 114 independent canonical counts summing to 6236", () => {
    expect(validateSurahProgress(progress()).reduce((sum, row) => sum + row.total_ayahs, 0)).toBe(6236);
    const wrong = progress(); wrong[1].total_ayahs -= 1;
    expect(() => validateSurahProgress(wrong)).toThrow();
    expect(() => validateSurahProgress(progress().slice(1))).toThrow();
    const duplicate = progress(); duplicate[1] = duplicate[0];
    expect(() => validateSurahProgress(duplicate)).toThrow();
  });
  it("bounds navigation and rejects impossible coverage", () => {
    expect(editorialPagination("-1", 6)).toBe(1);
    expect(editorialPagination("99999", 6)).toBe(6);
    const wrong = progress(); wrong[0].screened_ayahs = 8;
    expect(() => validateSurahProgress(wrong)).toThrow();
  });
});

describe("read-only bounded ayah evidence", () => {
  beforeEach(() => {
    vi.stubEnv("SUPABASE_URL", "https://fixture.supabase.co");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "test-only");
    vi.stubEnv(LIVE_TEST_WRITE_OPT_IN_ENV, "1");
  });
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
  it("pairs both passes and source hashes with the current ayah and run", async () => {
    const sourceHash = sourceEvidenceHash("trusted fixture"), translationHash = sourceEvidenceHash("test translation");
    const request = vi.fn(async (url: string) => {
      const table = new URL(url).pathname.split("/").pop();
      const rows = table === "editorial_ayah_reviews" ? [{ policy_version: "calm-inviting-v1",
        surah_number: 2, ayah_number: 21, canonical_key: "2:21",
        screening_status: "SCREENED", semantic_assessment: "ELIGIBLE_CANDIDATE", screening_run_id: "run-new",
        source_hash: sourceHash, translation_hash: translationHash }]
        : table === "editorial_source_evidence" ? [
          { policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 21,
            source_hash: "c".repeat(64), translation_hash: translationHash, arabic_text: "wrong historical source" },
          { policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 21,
            source_hash: sourceHash, translation_hash: translationHash, arabic_text: "trusted fixture",
            translation_text: "test translation", footnotes: "", footnotes_hash: sourceEvidenceHash(""),
            arabic_source_provider: "Quran Foundation", arabic_source_version: "v4",
            translation_provider: "QuranEnc", translation_version: "source-pinned" },
        ] : [1, 2].map((pass_number) => ({
          policy_version: "calm-inviting-v1", surah_number: 2, ayah_number: 21,
          run_id: "run-new", pass_number, source_hash: sourceHash, translation_hash: translationHash,
          footnotes_hash: sourceEvidenceHash(""), evidence_fingerprint: String(pass_number).repeat(64),
          evaluator_version: "test-native-reviewer", semantic_assessment: "ELIGIBLE_CANDIDATE",
          evidence: { pass_number, prompt_sha256: String(pass_number + 2).repeat(64),
            semantic_assessment: "ELIGIBLE_CANDIDATE", passage_first_ayah: 21, passage_last_ayah: 21,
            standalone_sufficient: true, exclusion_indicators: [], uncertainty: "clear",
            _evaluator_audit: { raw_assessment_sha256: String(pass_number + 4).repeat(64) } },
        }));
      return new Response(JSON.stringify(rows), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    const result = await getEditorialAyahsFromSupabase(2, "ALL", 1, new Set(["2:21"]));
    expect(result.error).toBeNull();
    expect(result.rows[0].productionApproved).toBe(true);
    expect(result.rows[0].source?.arabic_text).toBe("trusted fixture");
    expect(result.rows[0].assessments).toHaveLength(2);
    expect(result.rows[0].evidenceIssues).toEqual([]);
    const urls = request.mock.calls.map(([url]) => new URL(url));
    expect(urls[0].searchParams.get("limit")).toBe("26");
    expect(urls[1].searchParams.get("source_hash")).toBe(`in.(${sourceHash})`);
    expect(urls[2].searchParams.get("run_id")).toBe("in.(run-new)");
  });
  it("fails closed for malformed page inputs without a request", async () => {
    const request = vi.fn(); vi.stubGlobal("fetch", request);
    expect((await getEditorialAyahsFromSupabase(999, "ALL", 1, new Set())).error).toBeTruthy();
    request.mockClear();
    expect((await getEditorialAyahsFromSupabase(2, "ALL", 13, new Set())).error).toBeTruthy();
    expect(request).not.toHaveBeenCalled();
  });
});
