import React from "react";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EditorialCoverageSection } from "../components/EditorialCoverageSection";
import DashboardLoading from "../app/(admin)/dashboard/loading";
import { computeEditorialCoverage, getEligibleAyahKeys, isEligibleEditorialRange } from "../lib/editorial-coverage";
import { getEditorialProgress } from "../lib/editorial-progress";
import { isCanonicalAyahKey, TOTAL_CANONICAL_AYAHS } from "../lib/canonical-quran";

const schedule = [
  { id: "morning", time: "09:00", enabled: true },
  { id: "afternoon", time: "13:00", enabled: false },
  { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
];
const unavailablePublishing = {
  status: "unavailable" as const,
  records: [] as [],
  claims: [] as [],
  error: "Publication evidence could not be verified from Supabase.",
};
const liveScreening = {
  status: "ready" as const,
  summary: {
    policy_version: "calm-inviting-v1",
    canonical_ayahs: 6236,
    screened_ayahs: 5,
    not_screened_ayahs: 6231,
    eligible_candidates: 2,
    ineligible_candidates: 2,
    ambiguous_candidates: 1,
    production_approved_ayahs: 1,
    automatic_production_approved_ayahs: 1,
    legacy_production_approved_ayahs: 0,
    production_approved_passages: 1,
    last_successful_screening_at: "2000-01-01T00:00:00Z",
  },
  worker: null,
  preliminaryDrafts: { total: 2, awaiting_second_pass: 2, source_unverified: 2 },
  productionAuthorizedCandidateKeys: ["2:21"],
  productionAuthorizedRanges: ["2:21-21"],
};

describe("editorial coverage and project progress", () => {
  it("uses unique catalog ayah keys and calculates historical coverage separately", () => {
    const eligible = getEligibleAyahKeys();
    expect(eligible.size).toBe(0);
    expect(TOTAL_CANONICAL_AYAHS).toBe(6236);
    expect(isCanonicalAyahKey("114:6")).toBe(true);
    expect(isCanonicalAyahKey("114:7")).toBe(false);

    const result = computeEditorialCoverage(
      ["2:81", "2:81", "2:82", "2:83"],
      1,
      "2000-01-01T01:00:00Z",
      "2000-01-01T02:00:00.000Z",
    );
    expect(result).toMatchObject({
      canonicalAyahs: 6236,
      eligibleAyahs: 0,
      eligibleCoveredAyahs: 0,
      eligibleRemainingAyahs: 0,
      coveragePercentage: 0,
      historicallyCoveredAyahs: 3,
      policyVersion: "calm-inviting-v1",
    });
  });

  it("rejects malformed or non-canonical coverage keys", () => {
    expect(() => computeEditorialCoverage(["999:1"], 1, null)).toThrow("non-canonical");
    expect(() => computeEditorialCoverage(["2:287"], 1, null)).toThrow("non-canonical");
    expect(() => computeEditorialCoverage(["garbage"], 1, null)).toThrow("non-canonical");
  });

  it("grants no eligibility from a fresh catalog", () => {
    expect(isEligibleEditorialRange(2, 21, 21)).toBe(false);
    expect(isEligibleEditorialRange(2, 7, 7)).toBe(false);
    expect(isEligibleEditorialRange(999, 1, 1)).toBe(false);
  });

  it("reports current Quran editorial inventory totals without treating proposals as complete review", () => {
    expect(getEditorialProgress()).toMatchObject({
      canonicalAyahs: 6236,
      approvedAyahs: 0,
      requiresReviewAyahs: 0,
      notYetReviewedAyahs: 6236,
      confirmedExcludedAyahs: 0,
      completedDecisions: 0,
      catalogRanges: 0,
      reviewCompletenessPercentage: 0,
      policyVersion: "calm-inviting-v1",
    });
  });

  it("renders editorial and publishing progress as separate, evidence-backed sections", () => {
    const summary = computeEditorialCoverage(
      ["2:81", "2:82", "2:83"],
      1,
      "2000-01-01T01:00:00Z",
      "2000-01-01T02:00:00Z",
    );
    const html = renderToStaticMarkup(
      <EditorialCoverageSection
        load={{ status: "ready", summary, error: null }}
        screeningLoad={liveScreening}
        publishingLoad={unavailablePublishing}
        schedule={schedule}
        configured={{ facebook: true, instagram: true }}
      />,
    );
    expect(html).toContain("Quran editorial review");
    expect(html).toContain("Actual publishing progress");
    expect(html).toContain("Approved-surah delivery progress");
    expect(html).toContain("Surahs with confirmed historical delivery of every approved ayah");
    expect(html).toContain("Surahs fully generated/uploaded in this cycle");
    expect(html).toContain("Verified posts within this specific cycle</dt><dd>Not verified by cycle");
    expect(html).toContain("Production-approved candidate identities");
    expect(html).toContain("Eligible candidates");
    expect(html).toContain("Ineligible candidates");
    expect(html).toContain("Ambiguous / review required");
    expect(html).toContain("Preliminary ChatGPT draft reviews");
    expect(html).toContain("Drafts awaiting independent second assessment");
    expect(html).toContain("6,231");
    expect(html).toContain("Currently verified published</dt><dd>Not verified");
    expect(html).toContain("Remaining unpublished</dt><dd>Not verified");
    expect(html).toContain("Publication evidence could not be verified from Supabase");
    expect(html).toContain("6,236");
    expect(html).toContain("Last successful screening");
    expect(html).toContain("Automatic from eligible screening");
    expect(html).toContain("Two successful independent editorial passes");
    expect(html).toContain("One-time full-Quran scan INCOMPLETE: 6,231 canonical ayahs remain unreviewed");
    expect(html).toContain("final catalog freeze is blocked.");
    expect(html).toContain("No connected source currently verifies present-day Facebook or Instagram presence");
    expect(html).toContain("All-corpus historical coverage keys: 3 distinct ayahs");
    expect(html).not.toContain("Currently verified published</dt><dd>3");
    expect(html).toContain("aria-label=\"Canonical ayahs with committed editorial screening\"");
  });

  it("does not turn unavailable publication evidence into a zero count", () => {
    const html = renderToStaticMarkup(
      <EditorialCoverageSection
        load={{ status: "missing", summary: null, error: null }}
        screeningLoad={{ status: "unavailable", summary: null, worker: null, productionAuthorizedCandidateKeys: [], error: "Live editorial screening is not configured." }}
        publishingLoad={unavailablePublishing}
        schedule={schedule}
        configured={{ facebook: false, instagram: false }}
      />,
    );
    expect(html).toContain("Currently verified published</dt><dd>Not verified");
    expect(html).toContain("Surahs with some verified historical delivery</dt><dd>Not verified");
    expect(html).toContain("Unverified delivery outcomes (platform claims)</dt><dd>Not verified");
    expect(html).not.toContain("Currently verified published</dt><dd>0");
    expect(html).toContain("09:00 / 16:00 Europe/Berlin");
    expect(html).not.toContain("13:00");
  });

  it("shows a non-numeric loading state while dashboard data is pending", () => {
    const html = renderToStaticMarkup(<DashboardLoading />);
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain("Loading");
    expect(html).not.toContain(">0</dd>");
  });
});
