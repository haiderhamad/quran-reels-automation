import { describe, it, expect } from "vitest";
import { auditEditorialEvidencePage, embeddedReviewAssessmentSha256, sourceEvidenceHash,
  type EditorialLedgerRow, type EditorialSourceRow, type EditorialAssessmentRow,
} from "../lib/editorial-evidence-audit";
import { preflightQuranScreening } from "../lib/editorial-freeze-preflight";

const policy = "calm-inviting-v1";
const source: EditorialSourceRow = {
  policy_version: policy, surah_number: 27, ayah_number: 63,
  source_hash: sourceEvidenceHash("  آية  عربية  "), translation_hash: sourceEvidenceHash(" Sample translation "),
  footnotes_hash: sourceEvidenceHash(""),
  arabic_text: "  آية  عربية  ", translation_text: " Sample translation ", footnotes: "",
  arabic_source_provider: "Quran Foundation", arabic_source_version: "v4",
  translation_provider: "QuranEnc", translation_version: "source-pinned",
};
const review: EditorialLedgerRow = {
  policy_version: policy, surah_number: 27, ayah_number: 63, canonical_key: "27:63",
  screening_status: "SCREENED", semantic_assessment: "ELIGIBLE_CANDIDATE",
  screening_run_id: "run-27-63",
  source_hash: source.source_hash, translation_hash: source.translation_hash,
};
const pass = (pass_number: 1 | 2): EditorialAssessmentRow => ({
  policy_version: policy, surah_number: 27, ayah_number: 63, run_id: "run-27-63",
  pass_number, source_hash: source.source_hash,
  translation_hash: source.translation_hash, footnotes_hash: source.footnotes_hash,
  evidence_fingerprint: String(pass_number).repeat(64),
  evaluator_version: "source-reviewer-v1",
  evidence: {
    pass_number, prompt_sha256: String(pass_number + 2).repeat(64),
    passage_first_ayah: 63, passage_last_ayah: 63, standalone_sufficient: true,
    exclusion_indicators: [], uncertainty: "clear", semantic_assessment: "ELIGIBLE_CANDIDATE",
    _evaluator_audit: { raw_assessment_sha256: String(pass_number + 4).repeat(64) },
  },
});

function nativeV3Pass(n: 1 | 2, reviewerId: string): EditorialAssessmentRow {
  const original = pass(n);
  return {
    ...original,
    evaluator_version: "native-codex:gpt-6.1-sol:isolated-dual-review-v3",
    evidence: {
      ...(original.evidence as Record<string, unknown>),
      _evaluator_audit: {
        review_protocol: "native-codex-isolated-dual-review-v3",
        reviewer_id: reviewerId,
        reviewer_perspective: n,
        isolation_attested: true,
        source_packet_sha256: "d".repeat(64),
        raw_output_sha256: String(n + 4).repeat(64),
      },
    },
  };
}

describe("live-format editorial reviewer evidence audit (no Supabase writes)", () => {
  it("reconstructs canonical Python historical assessment SHA-256 for Arabic and nonempty arrays", () => {
    const assessment = {
      standalone_sufficient: true, key: "20:74",
      exclusion_indicators: ["شدة", "خوف"], passage_first_ayah: 74,
      passage_last_ayah: 74, semantic_assessment: "AMBIGUOUS_REVIEW_REQUIRED",
      context_notes: "السلام والسياق", assessment_rationale: "متابعة",
      uncertainty: "uncertain", candidate_theme: "peace",
    };
    expect(embeddedReviewAssessmentSha256(assessment))
      .toBe("6e51f0a64a0a8c5daba8590b7bcd164cbdf374c588b2019ea4e1d4867bcef277");
    expect(embeddedReviewAssessmentSha256(Object.fromEntries(Object.entries(assessment).reverse())))
      .toBe("6e51f0a64a0a8c5daba8590b7bcd164cbdf374c588b2019ea4e1d4867bcef277");
    expect(() => embeddedReviewAssessmentSha256({ unsafe: 1.5 }))
      .toThrow("unsupported value");
  });

  it("detects legacy embedded reviewer tampering instead of trusting a matching-looking SHA field", () => {
    const embedded = {
      semantic_assessment: "ELIGIBLE_CANDIDATE", candidate_theme: "calm",
      context_notes: "source checked", exclusion_indicators: [],
    };
    const raw_assessment_sha256 = embeddedReviewAssessmentSha256(embedded);
    const first = pass(1);
    const withEmbedded = {
      ...first, evidence: {
        ...(first.evidence as Record<string, unknown>),
        _evaluator_audit: { reviewer_assessment: embedded, raw_assessment_sha256 },
      },
    };
    expect(auditEditorialEvidencePage([review], [source], [withEmbedded, pass(2)], policy).issues)
      .toEqual([]);
    const forged = {
      ...withEmbedded, evidence: {
        ...(withEmbedded.evidence as Record<string, unknown>),
        _evaluator_audit: {
          reviewer_assessment: { ...embedded, candidate_theme: "tampered" },
          raw_assessment_sha256,
        },
      },
    };
    const result = auditEditorialEvidencePage([review], [source], [forged, pass(2)], policy);
    expect(result.countStructurallyReady).toBe(0);
    expect(result.issues[0].reasons).toContain("EMBEDDED_REVIEW_DIGEST_MISMATCH");
  });

  it("can reconcile two properly shaped pass records and text-pinned sources", () => {
    const audit = auditEditorialEvidencePage([review], [source], [pass(1), pass(2)], policy);
    expect(audit).toMatchObject({
      countScreened: 1, countStructurallyReady: 1, countTechnicalFallback: 0, issues: [],
    });
    expect(audit.proofs[0].passes).toHaveLength(2);
    expect(preflightQuranScreening(audit.proofs, policy).structurallyReviewed).toBe(1);
  });

  it("accepts the genuine native-v3 raw_output_sha256 field instead of false missing-hash alarms", () => {
    const result = auditEditorialEvidencePage(
      [review], [source], [nativeV3Pass(1, "reviewer-alpha"), nativeV3Pass(2, "reviewer-beta")], policy,
    );
    expect(result.issues).toEqual([]);
    expect(result.countStructurallyReady).toBe(1);
    expect(preflightQuranScreening(result.proofs, policy).structurallyReviewed).toBe(1);
  });

  it("rejects false independence claims even when two native-v3 raw digests exist", () => {
    const first = nativeV3Pass(1, "same-reviewer");
    const second = nativeV3Pass(2, "same-reviewer");
    const check = (secondPass: EditorialAssessmentRow) =>
      auditEditorialEvidencePage([review], [source], [first, secondPass], policy);
    expect(check(second).issues[0].reasons).toContain("MISSING_INDEPENDENCE_ATTESTATION");
    const originalEvidence = second.evidence as Record<string, unknown>;
    const originalAudit = originalEvidence._evaluator_audit as Record<string, unknown>;
    for (const mutation of [
      { reviewer_id: "different-reviewer", isolation_attested: false },
      { reviewer_id: "different-reviewer", source_packet_sha256: "e".repeat(64) },
      { reviewer_id: "different-reviewer", reviewer_perspective: 1 },
    ]) {
      expect(check({ ...second, evidence: {
        ...originalEvidence, _evaluator_audit: { ...originalAudit, ...mutation },
      } }).issues[0].reasons).toContain("MISSING_INDEPENDENCE_ATTESTATION");
    }
  });

  it("never accepts an eligible final decision when either reviewer disagrees", () => {
    const inconsistent = { ...pass(2), evidence: {
      ...(pass(2).evidence as Record<string, unknown>),
      semantic_assessment: "INELIGIBLE_CANDIDATE",
    } };
    const a = auditEditorialEvidencePage([review], [source], [pass(1), inconsistent], policy);
    expect(a.countStructurallyReady).toBe(0);
    expect(a.issues[0].reasons).toContain("ELIGIBILITY_RECONCILIATION");
  });

  it("refuses a ledger SCREENED row without real two-pass records", () => {
    const a = auditEditorialEvidencePage([review], [source], [], policy);
    expect(a.countStructurallyReady).toBe(0);
    expect(a.issues[0].reasons).toContain("MISSING_PASS");
    expect(preflightQuranScreening(a.proofs, policy).invalidReviews).toBe(1);
  });

  it("reports legacy incomplete passage schema rather than quietly approving", () => {
    const old = { ...pass(1), evidence: {
      pass_number: 1, prompt_sha256: "3".repeat(64),
      semantic_assessment: "ELIGIBLE_CANDIDATE", _evaluator_audit: {},
    } };
    const a = auditEditorialEvidencePage([review], [source], [old, pass(2)], policy);
    expect(a.countTechnicalFallback).toBe(1);
    expect(a.countStructurallyReady).toBe(0);
    expect(a.issues[0].reasons).toContain("LEGACY_PASSAGE_SCHEMA");
    expect(a.issues[0].reasons).toContain("MISSING_RAW_RESPONSE_HASH");
  });

  it("detects actual source text corruption, not merely a formally valid hash", () => {
    const a = auditEditorialEvidencePage([review], [{ ...source, arabic_text: "other" }], [pass(1), pass(2)], policy);
    expect(a.issues[0].reasons).toContain("SOURCE_MISMATCH");
    expect(a.countStructurallyReady).toBe(0);
  });

  it("rejects contradictory run, footnote or source evidence linkage", () => {
    const extra = { ...pass(2), translation_hash: "9".repeat(64) };
    expect(auditEditorialEvidencePage([review], [source], [pass(1), extra], policy)
      .issues[0].reasons).toContain("PASS_SOURCE_MISMATCH");
    expect(auditEditorialEvidencePage([review], [source], [pass(1)], policy)
      .issues[0].reasons).toContain("MISSING_PASS");
  });

  it("does not count duplicated first pass as two reviewer evaluations", () => {
    const a = auditEditorialEvidencePage([review], [source], [pass(1), { ...pass(1) }], policy);
    expect(a.countStructurallyReady).toBe(0);
    expect(a.issues[0].reasons).toContain("MALFORMED_PASS_IDENTITY");
  });

  it("rejects two differently identified passes that reuse an identical prompt", () => {
    const second = { ...pass(2), evidence: {
      ...(pass(2).evidence as Record<string, unknown>),
      prompt_sha256: "3".repeat(64),
    } };
    const a = auditEditorialEvidencePage([review], [source], [pass(1), second], policy);
    expect(a.countStructurallyReady).toBe(0);
    expect(a.issues[0].reasons).toContain("MALFORMED_PASS_IDENTITY");
  });

  it("treats not-screened rows as pending without inventing source or assessments", () => {
    const pending = { ...review, screening_status: "NOT_SCREENED", screening_run_id: null,
      source_hash: null, translation_hash: null };
    const a = auditEditorialEvidencePage([pending], [], [], policy);
    expect(a).toMatchObject({ countScreened: 0, countStructurallyReady: 0, issues: [] });
    expect(preflightQuranScreening(a.proofs, policy).notScreened).toBe(1);
  });

  it("rejects foreign source, wrong policy, and duplicate canonical rows", () => {
    expect(() => auditEditorialEvidencePage([review, review], [source], [], policy)).toThrow("duplicate");
    expect(() => auditEditorialEvidencePage([review], [{ ...source, ayah_number: 62 }], [], policy))
      .toThrow("Foreign or wrong-policy");
    expect(() => auditEditorialEvidencePage([{ ...review, canonical_key: "27:64" }], [], [], policy))
      .toThrow("Invalid or duplicate");
    expect(() => auditEditorialEvidencePage([], [], [], "bad|policy")).toThrow("bounded");
  });
});
