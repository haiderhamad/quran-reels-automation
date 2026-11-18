import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CANONICAL_SURAH_AYAH_COUNTS } from "../lib/canonical-quran";

const reads = vi.hoisted(() => ({ summary: vi.fn(), screening: vi.fn(), ayahs: vi.fn(), fresh: vi.fn() }));
vi.mock("../lib/supabase-state", () => ({ getEditorialSurahProgressFromSupabase: reads.summary,
  getEditorialScreeningFromSupabase: reads.screening, getEditorialAyahsFromSupabase: reads.ayahs,
  getCalmReelsFreshCycleFromSupabase: reads.fresh }));
import EditorialPage from "../app/(admin)/editorial/page";

describe("editorial overview and evidence drilldown", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    reads.fresh.mockResolvedValue({ status: "ready", progress: { cycleNumber: 1, confirmedAyahs: 0, totalAyahs: 2211, nextAyahKey: "1:1", startedAt: "2026-10-28T21:16:47Z" } });
    reads.summary.mockResolvedValue({ error: null, rows: CANONICAL_SURAH_AYAH_COUNTS.map((total, index) => ({
      surah_number: index + 1, total_ayahs: total, screened_ayahs: index === 1 ? 1 : 0,
      remaining_ayahs: total - (index === 1 ? 1 : 0), eligible_candidates: index === 1 ? 1 : 0,
      production_approved_ayahs: index === 1 ? 1 : 0,
    })) });
    reads.screening.mockResolvedValue({ status: "ready", productionAuthorizedCandidateKeys: ["2:21"],
      summary: { screened_ayahs: 1, eligible_candidates: 1, production_approved_ayahs: 1 } });
    reads.ayahs.mockResolvedValue({ error: null, hasNext: true, rows: [{ ayah_number: 21,
      screening_status: "SCREENED", semantic_assessment: "ELIGIBLE_CANDIDATE", productionApproved: true,
      source: { arabic_text: "نص موثوق", translation_text: "Trusted fixture translation", translation_language: "en", translation_version: "1.0.19" },
      assessments: [1, 2].map((pass) => ({ pass_number: pass, semantic_assessment: "ELIGIBLE_CANDIDATE",
        assessment_rationale: `Independent rationale ${pass}`, context_notes: "Coherent context checked", evaluator_version: "fixture", run_id: "run-fixture" })),
    }] });
  });
  it("renders a bounded canonical overview and navigation", async () => {
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({}) }));
    expect(html).toContain("6,236 canonical ayahs");
    expect(html).toContain("114 matching surahs");
    expect(html).toContain("Surah 20");
    expect(html).not.toContain("Surah 21</a>");
    expect(html).toContain("overviewPage=2");
    expect(reads.ayahs).not.toHaveBeenCalled();
  });
  it("renders source and both assessments behind the existing admin route", async () => {
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({ surah: "2", page: "1", status: "ELIGIBLE_CANDIDATE" }) }));
    expect(html).toContain('lang="ar" dir="rtl"');
    expect(html).toContain("Trusted fixture translation");
    expect(html).toContain("Independent rationale 1");
    expect(html).toContain("Independent rationale 2");
    expect(html).toContain("Production approval: Approved");
    expect(html).toContain("Cycle 1: 0 / 2211 confirmed published");
    expect(html).toContain("Current publication cycle: see the fresh v1 counter above");
    expect(html).toContain("page=2#ayahs");
    expect(reads.ayahs).toHaveBeenCalledWith(2, "ELIGIBLE_CANDIDATE", 1, new Set(["2:21"]));
  });
  it("ignores old published records and displays the fresh zero baseline", async () => {
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({ surah: "2" }) }));
    expect(html).toContain("Cycle 1: 0 / 2211 confirmed published");
    expect(html).toContain("0.00% complete");
    expect(html).toContain('max="2211"');
    expect(html).toContain("Next ayah 1:1");
    expect(html).not.toContain("Verified published (historical ayahs)");
    expect(html).not.toContain("Historical delivery verified");
  });
  it("does not fake zero when the new Supabase ledger is missing", async () => {
    reads.fresh.mockResolvedValue({ status: "unavailable", progress: null });
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({}) }));
    expect(html).toContain("Current publication counter unavailable");
    expect(html).not.toContain("Cycle 1: 0");
  });
  it("flags preserved legacy approvals that conflict with completed screening", async () => {
    reads.ayahs.mockResolvedValue({ error: null, hasNext: false, rows: [{ ayah_number: 21,
      screening_status: "SCREENED", semantic_assessment: "AMBIGUOUS_REVIEW_REQUIRED", productionApproved: true,
      source: null, assessments: [],
    }] });
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({ surah: "2" }) }));
    expect(html).toContain("Preserved legacy approval conflicts with current screening; review required.");
    expect(html).toContain("Production approval: Approved");
  });
  it("shows concrete source/reviewer audit findings without equating SCREENED with verified evidence", async () => {
    reads.ayahs.mockResolvedValue({ error: null, hasNext: false, rows: [{
      ayah_number: 21, screening_status: "SCREENED", semantic_assessment: "ELIGIBLE_CANDIDATE",
      productionApproved: false, source: null, assessments: [],
      evidenceIssues: ["LEGACY_PASSAGE_SCHEMA", "MISSING_RAW_RESPONSE_HASH"],
    }] });
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({ surah: "2" }) }));
    expect(html).toContain("Two-pass source/evidence audit needs attention");
    expect(html).toContain("LEGACY PASSAGE SCHEMA");
    expect(html).toContain("MISSING RAW RESPONSE HASH");
    expect(html).not.toContain("Two-pass evidence is structurally consistent");
  });

  it("does not show counts or read privileged evidence when summary reconciliation fails", async () => {
    reads.screening.mockResolvedValue({ status: "unavailable" });
    const html = renderToStaticMarkup(await EditorialPage({ searchParams: Promise.resolve({ surah: "2" }) }));
    expect(html).toContain('role="alert"');
    expect(reads.ayahs).not.toHaveBeenCalled();
    expect(html).not.toContain("Trusted fixture translation");
  });
});
