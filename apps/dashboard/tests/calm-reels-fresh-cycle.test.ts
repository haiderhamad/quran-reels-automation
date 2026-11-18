import { describe, expect, it } from "vitest";
import { CALM_REELS_FROZEN_SHA256, calculateCalmReelsCycleProgress } from "../lib/calm-reels-fresh-cycle";
import { getCalmReelsShortlistKeys } from "../lib/calm-reels-shortlist";

const state = {
  catalog_id: "calm-reels-editorial-shortlist-v1",
  catalog_sha256: CALM_REELS_FROZEN_SHA256,
  total_ayahs: 2211,
  cycle_number: 1,
  started_at: "2026-10-28T21:16:47Z",
};

describe("fresh 2,211-ayah Supabase publication counter", () => {
  it("starts at zero and selects 1:1 first; never imports old coverage", () => {
    const output = calculateCalmReelsCycleProgress(state, [], getCalmReelsShortlistKeys());
    expect(output).toEqual({
      cycleNumber: 1, totalAyahs: 2211, confirmedAyahs: 0,
      nextAyahKey: "1:1", startedAt: state.started_at,
    });
  });
  it("advances only after recording a confirmed current-cycle ayah", () => {
    expect(calculateCalmReelsCycleProgress(state, ["1:1"], getCalmReelsShortlistKeys()))
      .toMatchObject({ confirmedAyahs: 1, nextAyahKey: "1:3" });
  });
  it("counts a confirmed adjacent pair as two ayahs, not two social posts", () => {
    const progress = calculateCalmReelsCycleProgress(state, ["1:1", "1:3", "1:4"], getCalmReelsShortlistKeys());
    expect(progress).toMatchObject({ confirmedAyahs: 3, nextAyahKey: "1:5" });
    expect((100 * progress.confirmedAyahs / progress.totalAyahs).toFixed(2)).toBe("0.14");
  });
  it("counts one verified five-ayah Fatiha Reel as five ayahs (six including previously posted 1:1)", () => {
    const progress = calculateCalmReelsCycleProgress(state,
      ["1:1", "1:3", "1:4", "1:5", "1:6", "1:7"], getCalmReelsShortlistKeys());
    expect(progress).toMatchObject({ confirmedAyahs: 6, nextAyahKey: "2:2" });
    expect((100 * progress.confirmedAyahs / progress.totalAyahs).toFixed(2)).toBe("0.27");
  });
  it("marks a complete cycle without silently starting or deleting the next", () => {
    const all = [...getCalmReelsShortlistKeys()];
    expect(calculateCalmReelsCycleProgress(state, all, new Set(all)))
      .toMatchObject({ confirmedAyahs: 2211, nextAyahKey: null });
  });
  it("rejects fake, duplicate and stale prior catalog data", () => {
    const keys = getCalmReelsShortlistKeys();
    expect(() => calculateCalmReelsCycleProgress(state, ["1:1","1:1"], keys)).toThrow("Malformed");
    expect(() => calculateCalmReelsCycleProgress(state, ["4:138"], keys)).toThrow("Malformed");
    expect(() => calculateCalmReelsCycleProgress({ ...state, catalog_sha256: "bad" }, [], keys)).toThrow("catalog");
    expect(() => calculateCalmReelsCycleProgress({ ...state, cycle_number: 0 }, [], keys)).toThrow("catalog");
    expect(() => calculateCalmReelsCycleProgress({ ...state, total_ayahs: 6236 }, [], keys)).toThrow("catalog");
  });
});
