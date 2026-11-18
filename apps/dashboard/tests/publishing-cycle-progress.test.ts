import { describe, expect, it } from "vitest";
import { summarizeAuthorizedSurahProgress } from "../lib/publishing-cycle-progress";

describe("approved-surah progress with distinct identity evidence", () => {
  const approved = new Set(["2:21", "2:22", "3:8", "114:6"]);

  it("counts unique surahs and ayahs, not repeated reels or unknown surahs", () => {
    const progress = summarizeAuthorizedSurahProgress(
      approved, ["2:21", "2:21", "2:22", "2:999", "7:1", "114:6"],
    );
    expect(progress).toEqual({
      eligibleSurahs: 3,
      startedSurahs: 2,
      completedSurahs: 2,
      remainingSurahs: 1,
      percentage: 66.7,
      eligibleAyahs: 4,
      completedAyahs: 3,
    });
  });

  it("does not fabricate eligible progress before the screened catalog exists", () => {
    expect(summarizeAuthorizedSurahProgress(new Set(), ["1:1"])).toEqual({
      eligibleSurahs: 0, startedSurahs: 0, completedSurahs: 0, remainingSurahs: 0,
      percentage: 0, eligibleAyahs: 0, completedAyahs: 0,
    });
  });

  it("rejects non-canonical authorization keys", () => {
    expect(() => summarizeAuthorizedSurahProgress(new Set(["115:1"]), [])).toThrow("non-canonical");
    expect(() => summarizeAuthorizedSurahProgress(new Set(["2:287"]), [])).toThrow("non-canonical");
  });

  it("does not confuse one ayah from a surah with full ayah delivery", () => {
    const progress = summarizeAuthorizedSurahProgress(approved, ["2:21"]);
    expect(progress.startedSurahs).toBe(1);
    expect(progress.completedSurahs).toBe(0);
    expect(progress.completedAyahs).toBe(1);
    expect(progress.eligibleAyahs).toBe(4);
  });
});
