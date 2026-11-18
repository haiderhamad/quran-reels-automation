import { describe, expect, it } from "vitest";
import { CANONICAL_SURAH_AYAH_COUNTS } from "../lib/canonical-quran";
import { preflightQuranScreening, type ReviewProof } from "../lib/editorial-freeze-preflight";

const POLICY = "calm-inviting-v1";
function reviewed(key: string): ReviewProof {
  const runId = "review-run-1";
  const arabicHash = "a".repeat(64), translationHash = "b".repeat(64);
  return {
    key, policy: POLICY, status: "SCREENED", runId, arabicHash, translationHash,
    technicalFallback: false,
    passes: [
      { pass: 1, evaluationId: "independent-first", perspective: "primary",
        runId, arabicHash, translationHash, promptHash: "1".repeat(64), responseHash: "3".repeat(64) },
      { pass: 2, evaluationId: "independent-second", perspective: "adversarial",
        runId, arabicHash, translationHash, promptHash: "2".repeat(64), responseHash: "4".repeat(64) },
    ],
  };
}
function entireQuran(): ReviewProof[] {
  return CANONICAL_SURAH_AYAH_COUNTS.flatMap((count, index) =>
    Array.from({ length: count }, (_, i) => reviewed(`${index + 1}:${i + 1}`)));
}

describe("one-time Quran screening structural freeze preflight", () => {
  it("never treats an empty or partial Quran review export as complete", () => {
    const blank = preflightQuranScreening([], POLICY);
    expect(blank).toMatchObject({
      state: "INCOMPLETE", canonicalTotal: 6236, suppliedRows: 0,
      structurallyReviewed: 0, missingRows: 6236, completeSurahs: 0,
    });
    expect(blank.unresolvedKeysPreview).toEqual(["1:1", "1:2", "1:3", "1:4",
      "1:5", "1:6", "1:7", "2:1", "2:2", "2:3", "2:4", "2:5"]);
    const partial = preflightQuranScreening([reviewed("27:64")], POLICY);
    expect(partial.structurallyReviewed).toBe(1);
    expect(partial.missingRows).toBe(6235);
  });

  it("counts a complete surah only when every canonical ayah has both proof shapes", () => {
    const surahOne = Array.from({ length: 7 }, (_, i) => reviewed(`1:${i + 1}`));
    const full = preflightQuranScreening(surahOne, POLICY);
    expect(full.completeSurahs).toBe(1);
    const incomplete = preflightQuranScreening(surahOne.slice(0, 6), POLICY);
    expect(incomplete.completeSurahs).toBe(0);
  });

  it("requires a further authentic independent source audit even for 6236 structurally valid rows", () => {
    const all = entireQuran();
    const ready = preflightQuranScreening(all, POLICY);
    expect(ready).toMatchObject({
      state: "NEEDS_INDEPENDENT_SOURCE_AUDIT", canonicalTotal: 6236,
      structurallyReviewed: 6236, missingRows: 0, notScreened: 0,
      invalidReviews: 0, historicalFallbacks: 0, completeSurahs: 114,
    });
    expect(ready.unresolvedKeysPreview).toEqual([]);
  });

  it("refuses to count a ledger SCREENED status without two independent attempt identities", () => {
    for (const invalid of [
      { passes: [reviewed("1:1").passes[0]] },
      { passes: [{ ...reviewed("1:1").passes[0] }, { ...reviewed("1:1").passes[0], pass: 2 as const }] },
      { passes: reviewed("1:1").passes.map((p) => ({ ...p, promptHash: "1".repeat(64) })) },
      { passes: reviewed("1:1").passes.map((p) => ({ ...p, evaluationId: "same" })) },
    ]) {
      const result = preflightQuranScreening([{ ...reviewed("1:1"), ...invalid }], POLICY);
      expect(result.invalidReviews).toBe(1);
      expect(result.structurallyReviewed).toBe(0);
    }
  });

  it("rejects run/source mismatch, invalid hash formats and wrong policy", () => {
    const original = reviewed("1:1");
    for (const mutation of [
      { arabicHash: "incorrect" },
      { translationHash: "incorrect" },
      { policy: "other-policy" },
      { passes: [original.passes[0], { ...original.passes[1], runId: "wrong-run" }] },
      { passes: [original.passes[0], { ...original.passes[1], arabicHash: "0".repeat(64) }] },
      { passes: [original.passes[0], { ...original.passes[1], responseHash: "malformed" }] },
    ]) {
      expect(preflightQuranScreening([{ ...original, ...mutation }], POLICY).invalidReviews).toBe(1);
    }
  });

  it("does not silently certify seventy historical technical fallbacks", () => {
    const all = entireQuran();
    for (let i = 0; i < 70; i++) {
      all[i] = { ...all[i], technicalFallback: true };
    }
    const result = preflightQuranScreening(all, POLICY);
    expect(result.historicalFallbacks).toBe(70);
    expect(result.invalidReviews).toBe(70);
    expect(result.structurallyReviewed).toBe(6166);
    expect(result.state).toBe("INCOMPLETE");
    expect(result.completeSurahs).toBeLessThan(114);
  });

  it("does not infer completion from an explicitly not-screened row", () => {
    const row = { ...reviewed("1:1"), status: "NOT_SCREENED" as const };
    const result = preflightQuranScreening([row], POLICY);
    expect(result.notScreened).toBe(1);
    expect(result.invalidReviews).toBe(0);
    expect(result.structurallyReviewed).toBe(0);
  });

  it("fails closed on duplicates, out-of-range keys, excessive exports and illegal status", () => {
    expect(() => preflightQuranScreening([reviewed("1:1"), reviewed("1:1")], POLICY))
      .toThrow("Duplicate or noncanonical");
    expect(() => preflightQuranScreening([reviewed("114:7")], POLICY))
      .toThrow("Duplicate or noncanonical");
    expect(() => preflightQuranScreening([
      { ...reviewed("1:1"), status: "SKIPPED" as "SCREENED" },
    ], POLICY)).toThrow("Unsupported screening status");
    expect(() => preflightQuranScreening(entireQuran().concat(reviewed("1:1")), POLICY))
      .toThrow("Invalid policy or Quran export size");
    expect(() => preflightQuranScreening([], "")).toThrow("Invalid policy");
  });
});
