import { describe, expect, it } from "vitest";
import { CALM_REELS_SHORTLIST_SIZE, CALM_REELS_SHORTLIST_VERSION, getCalmReelsShortlistKeys, countHistoricallyDeliveredShortlistedAyahs, summarizeCalmReelsLiveAuthorizationOverlap } from "../lib/calm-reels-shortlist";

describe("frozen calm Quran Reels editorial inventory", () => {
  it("contains 2,211 canonical unique source-locked ayahs", () => {
    const keys = getCalmReelsShortlistKeys();
    expect(CALM_REELS_SHORTLIST_SIZE).toBe(2211);
    expect(CALM_REELS_SHORTLIST_VERSION).toBe("calm-reels-editorial-shortlist-v1");
    expect(keys.size).toBe(2211);
    expect(keys.has("39:53")).toBe(true);
    expect(keys.has("4:138")).toBe(false);
    expect(keys.has("2:40")).toBe(false);
  });
  it("counts historical intersection without assuming publishing cycle completion", () => {
    expect(countHistoricallyDeliveredShortlistedAyahs(new Set(["39:53","4:138","39:53","1:1"]))).toBe(2);
    expect(countHistoricallyDeliveredShortlistedAyahs(new Set())).toBe(0);
  });
  it("reports only complete passages with prior live approval", () => {
    const summary = summarizeCalmReelsLiveAuthorizationOverlap(
      ["39:53", "4:138", "1:3"],
      ["39:53-53", "4:138-138", "1:3-3"],
    );
    expect(summary).toEqual({
      shortlistAyahsAlreadyAuthorized: 2,
      fullyInScopeAuthorizedPassages: 2,
      ayahsInsideFullAuthorizedPassages: 2,
    });
  });
  it("will not split a context passage even if two of three verses are desired", () => {
    expect(summarizeCalmReelsLiveAuthorizationOverlap(
      ["1:1", "1:2", "1:3"], ["1:1-3"],
    )).toEqual({
      shortlistAyahsAlreadyAuthorized: 2,
      fullyInScopeAuthorizedPassages: 0,
      ayahsInsideFullAuthorizedPassages: 0,
    });
  });
  it("rejects duplicate, malformed and unapproved exact range evidence", () => {
    expect(() => summarizeCalmReelsLiveAuthorizationOverlap(["39:53", "39:53"], [])).toThrow("Malformed");
    expect(() => summarizeCalmReelsLiveAuthorizationOverlap(["39:53"], ["39:53-53", "39:53-53"])).toThrow("Duplicate");
    expect(() => summarizeCalmReelsLiveAuthorizationOverlap(["39:53"], ["115:1-1"])).toThrow("Noncanonical");
    expect(summarizeCalmReelsLiveAuthorizationOverlap(["39:53"], ["1:3-3"]).fullyInScopeAuthorizedPassages).toBe(0);
  });
});
