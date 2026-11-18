import { createHash } from "node:crypto";
import { isCanonicalAyahKey } from "./canonical-quran";
import type { ReviewProof } from "./editorial-freeze-preflight";

/**
 * Read-only reconciliation against ACTUAL public.editorial_* table shapes.
 * Structural matches are not independent reviewer/source authenticity proofs.
 * This module must only run on the authenticated server; raw source content
 * and reviewer evidence must not be sent to a client or committed to Git.
 */
export interface EditorialLedgerRow {
  policy_version: string;
  surah_number: number;
  ayah_number: number;
  canonical_key: string;
  screening_status: string;
  semantic_assessment: string | null;
  screening_run_id: string | null;
  source_hash: string | null;
  translation_hash: string | null;
}

export interface EditorialSourceRow {
  policy_version: string;
  surah_number: number;
  ayah_number: number;
  source_hash: string;
  translation_hash: string;
  footnotes_hash: string;
  arabic_text: string;
  translation_text: string;
  footnotes: string;
  arabic_source_provider: string;
  arabic_source_version: string;
  translation_provider: string;
  translation_version: string;
}

export interface EditorialAssessmentRow {
  policy_version: string;
  surah_number: number;
  ayah_number: number;
  run_id: string;
  pass_number: number;
  source_hash: string;
  translation_hash: string;
  footnotes_hash: string;
  evidence_fingerprint: string;
  evaluator_version: string;
  evidence: unknown;
}

export type EditorialEvidenceIssue =
  | "MISSING_SOURCE" | "SOURCE_MISMATCH" | "MISSING_PASS"
  | "DUPLICATE_OR_EXTRA_PASS" | "PASS_SOURCE_MISMATCH"
  | "LEGACY_PASSAGE_SCHEMA" | "MISSING_EVALUATOR_AUDIT"
  | "MISSING_RAW_RESPONSE_HASH" | "MALFORMED_PASS_IDENTITY"
  | "MISSING_INDEPENDENCE_ATTESTATION" | "ELIGIBILITY_RECONCILIATION"
  | "EMBEDDED_REVIEW_DIGEST_MISMATCH";

export interface EditorialEvidencePageAudit {
  proofs: readonly ReviewProof[];
  issues: readonly { key: string; reasons: readonly EditorialEvidenceIssue[] }[];
  countScreened: number;
  countStructurallyReady: number;
  countTechnicalFallback: number;
}

const SHA = /^[0-9a-f]{64}$/;

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/** Match Python source_text_sha256: collapse Unicode whitespace before UTF-8. */
export function sourceEvidenceHash(value: string): string {
  return createHash("sha256").update(value.split(/[\p{White_Space}\u001C-\u001F]+/gu).filter(Boolean).join(" "), "utf8").digest("hex");
}

/**
 * Reproduce Python's json.dumps(value, ensure_ascii=False, sort_keys=True,
 * separators=(",", ":")) for the restricted historical assessment schema.
 * Does not attest reviewer origin; the JSON object is reconstructed from DB
 * and must still be bound to independently authenticated source/run evidence.
 * Reject ambiguous floating-point values instead of guessing the original
 * Python number serialization. Never emit reviewer content in audit output.
 */
function canonicalHistoricalReviewJson(value: unknown, depth = 0): string {
  if (depth > 8) throw new Error("Historical review JSON nesting is unsupported");
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) {
    return "[" + value.map((item) => canonicalHistoricalReviewJson(item, depth + 1)).join(",") + "]";
  }
  if (isRecord(value)) {
    return "{" + Object.keys(value).sort().map((key) =>
      JSON.stringify(key) + ":" + canonicalHistoricalReviewJson(value[key], depth + 1),
    ).join(",") + "}";
  }
  throw new Error("Historical review JSON contains unsupported value");
}

export function embeddedReviewAssessmentSha256(value: unknown): string {
  const canonical = canonicalHistoricalReviewJson(value);
  if (Buffer.byteLength(canonical, "utf8") > 32 * 1024) {
    throw new Error("Historical review assessment exceeds bounded size");
  }
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

/** Reconcile 1–25 ayahs at a time; never silently discard an unexpected row. */
export function auditEditorialEvidencePage(
  reviews: readonly EditorialLedgerRow[],
  sources: readonly EditorialSourceRow[],
  assessments: readonly EditorialAssessmentRow[],
  policy: string,
): EditorialEvidencePageAudit {
  if (reviews.length > 25 || !/^[\w.:-]{1,128}$/.test(policy)) {
    throw new Error("Editorial audit page exceeds bounded review or policy limit");
  }
  const seen = new Set<string>();
  const keys = new Set<string>();
  for (const review of reviews) {
    const key = `${review.surah_number}:${review.ayah_number}`;
    if (!isCanonicalAyahKey(key) || review.canonical_key !== key
      || review.policy_version !== policy || seen.has(key)
      || (review.screening_status !== "SCREENED" && review.screening_status !== "NOT_SCREENED")) {
      throw new Error("Invalid or duplicate canonical review in live evidence page");
    }
    seen.add(key); keys.add(key);
  }
  for (const record of [...sources, ...assessments]) {
    const key = `${record.surah_number}:${record.ayah_number}`;
    if (!keys.has(key) || record.policy_version !== policy) {
      throw new Error("Foreign or wrong-policy evidence in bounded source page");
    }
  }
  const proofs: ReviewProof[] = [];
  const issues: { key: string; reasons: EditorialEvidenceIssue[] }[] = [];
  let countScreened = 0, countStructurallyReady = 0, countTechnicalFallback = 0;

  for (const review of reviews) {
    const key = review.canonical_key;
    const reasons: EditorialEvidenceIssue[] = [];
    const selectedSources = sources.filter((row) =>
      row.surah_number === review.surah_number && row.ayah_number === review.ayah_number
      && row.source_hash === review.source_hash && row.translation_hash === review.translation_hash);
    const matchingSource = selectedSources.length === 1 ? selectedSources[0] : null;
    const matchingPasses = assessments.filter((row) =>
      row.surah_number === review.surah_number && row.ayah_number === review.ayah_number
      && row.run_id === review.screening_run_id);
    let technicalFallback = false;
    const normalizedPasses: ReviewProof["passes"][number][] = [];

    if (review.screening_status === "SCREENED") {
      countScreened++;
      if (!matchingSource) reasons.push("MISSING_SOURCE");
      else if (!SHA.test(matchingSource.source_hash) || !SHA.test(matchingSource.translation_hash)
        || !SHA.test(matchingSource.footnotes_hash)
        || sourceEvidenceHash(matchingSource.arabic_text) !== matchingSource.source_hash
        || sourceEvidenceHash(matchingSource.translation_text) !== matchingSource.translation_hash
        || sourceEvidenceHash(matchingSource.footnotes) !== matchingSource.footnotes_hash
        || !matchingSource.arabic_source_provider || !matchingSource.arabic_source_version
        || !matchingSource.translation_provider || !matchingSource.translation_version) {
        reasons.push("SOURCE_MISMATCH");
      }
      if (matchingPasses.length === 2 && review.semantic_assessment === "ELIGIBLE_CANDIDATE"
        && matchingPasses.some((pass) => !isRecord(pass.evidence)
          || pass.evidence.semantic_assessment !== "ELIGIBLE_CANDIDATE")) {
        reasons.push("ELIGIBILITY_RECONCILIATION");
      }
      if (!["ELIGIBLE_CANDIDATE", "INELIGIBLE_CANDIDATE", "AMBIGUOUS_REVIEW_REQUIRED"]
        .includes(String(review.semantic_assessment))) {
        reasons.push("ELIGIBILITY_RECONCILIATION");
      }
      if (matchingPasses.length !== 2) {
        reasons.push(matchingPasses.length < 2 ? "MISSING_PASS" : "DUPLICATE_OR_EXTRA_PASS");
      } else {
        if (matchingPasses[0].pass_number === matchingPasses[1].pass_number
          || ![1, 2].includes(matchingPasses[0].pass_number)
          || ![1, 2].includes(matchingPasses[1].pass_number)
          || matchingPasses[0].evidence_fingerprint === matchingPasses[1].evidence_fingerprint
          || (isRecord(matchingPasses[0].evidence) && isRecord(matchingPasses[1].evidence)
            && matchingPasses[0].evidence.prompt_sha256 === matchingPasses[1].evidence.prompt_sha256)) {
          reasons.push("MALFORMED_PASS_IDENTITY");
        }
        const nativeV3 = matchingPasses.some((pass) =>
          pass.evaluator_version === "native-codex:gpt-6.1-sol:isolated-dual-review-v3");
        if (nativeV3) {
          const audits = matchingPasses.map((pass) =>
            isRecord(pass.evidence) && isRecord(pass.evidence._evaluator_audit)
              ? pass.evidence._evaluator_audit : null);
          if (matchingPasses.some((pass) =>
            pass.evaluator_version !== "native-codex:gpt-6.1-sol:isolated-dual-review-v3")
            || audits.some((audit, i) => !audit
              || typeof audit.reviewer_id !== "string" || !audit.reviewer_id.trim()
              || audit.isolation_attested !== true
              || audit.reviewer_perspective !== matchingPasses[i].pass_number
              || audit.review_protocol !== "native-codex-isolated-dual-review-v3"
              || typeof audit.source_packet_sha256 !== "string"
              || !SHA.test(audit.source_packet_sha256))
            || audits[0]?.reviewer_id === audits[1]?.reviewer_id
            || audits[0]?.source_packet_sha256 !== audits[1]?.source_packet_sha256) {
            reasons.push("MISSING_INDEPENDENCE_ATTESTATION");
          }
        }
        for (const pass of matchingPasses) {
          if (pass.source_hash !== review.source_hash || pass.translation_hash !== review.translation_hash
            || (matchingSource && pass.footnotes_hash !== matchingSource.footnotes_hash)) {
            reasons.push("PASS_SOURCE_MISMATCH");
          }
          const evidence = isRecord(pass.evidence) ? pass.evidence : {};
          if (!Number.isInteger(evidence.passage_first_ayah)
            || !Number.isInteger(evidence.passage_last_ayah)
            || !isCanonicalAyahKey(`${review.surah_number}:${evidence.passage_first_ayah}`)
            || !isCanonicalAyahKey(`${review.surah_number}:${evidence.passage_last_ayah}`)
            || Number(evidence.passage_first_ayah) > Number(evidence.passage_last_ayah)
            || Number(evidence.passage_first_ayah) > review.ayah_number
            || Number(evidence.passage_last_ayah) < review.ayah_number
            || typeof evidence.standalone_sufficient !== "boolean"
            || !Array.isArray(evidence.exclusion_indicators)
            || !["clear", "uncertain"].includes(String(evidence.uncertainty))) {
            technicalFallback = true;
            reasons.push("LEGACY_PASSAGE_SCHEMA");
          }
          const audit = isRecord(evidence._evaluator_audit) ? evidence._evaluator_audit : null;
          if (!audit) reasons.push("MISSING_EVALUATOR_AUDIT");
          // Historical native reviews use raw_assessment_sha256, while the
          // validated v3 native protocol hashes each original reviewer FILE
          // under raw_output_sha256. Both are actual stored hash fields, not
          // proof that original bytes can still be independently retrieved.
          const raw = audit?.raw_output_sha256 ?? audit?.raw_assessment_sha256;
          if (typeof raw !== "string" || !SHA.test(raw)) {
            reasons.push("MISSING_RAW_RESPONSE_HASH");
          }
          // Only the legacy raw_assessment_sha256 is based on canonical
          // per-assessment JSON. Native-v3 raw_output_sha256 hashes the whole
          // original reviewer output FILE; never compare that to one object.
          if (audit && Object.prototype.hasOwnProperty.call(audit, "reviewer_assessment")) {
            if (typeof audit.raw_assessment_sha256 !== "string"
              || !SHA.test(audit.raw_assessment_sha256)) {
              reasons.push("MISSING_RAW_RESPONSE_HASH");
            } else {
              try {
                if (embeddedReviewAssessmentSha256(audit.reviewer_assessment)
                  !== audit.raw_assessment_sha256) {
                  reasons.push("EMBEDDED_REVIEW_DIGEST_MISMATCH");
                }
              } catch {
                reasons.push("EMBEDDED_REVIEW_DIGEST_MISMATCH");
              }
            }
          }
          if (!SHA.test(pass.evidence_fingerprint) || !SHA.test(String(evidence.prompt_sha256 ?? ""))
            || !pass.evaluator_version || pass.pass_number !== evidence.pass_number
            || typeof evidence.semantic_assessment !== "string"
            || !["ELIGIBLE_CANDIDATE", "INELIGIBLE_CANDIDATE", "AMBIGUOUS_REVIEW_REQUIRED"].includes(evidence.semantic_assessment)) {
            reasons.push("MALFORMED_PASS_IDENTITY");
          }
          normalizedPasses.push({
            pass: pass.pass_number as 1 | 2,
            evaluationId: pass.evidence_fingerprint,
            perspective: pass.pass_number === 1 ? "primary" : "adversarial",
            runId: pass.run_id,
            arabicHash: pass.source_hash,
            translationHash: pass.translation_hash,
            promptHash: String(evidence.prompt_sha256 ?? ""),
            responseHash: typeof raw === "string" ? raw : "",
          });
        }
      }
    }
    if (technicalFallback) countTechnicalFallback++;
    if (reasons.length === 0 && review.screening_status === "SCREENED") countStructurallyReady++;
    if (reasons.length) issues.push({ key, reasons: [...new Set(reasons)] });
    proofs.push({
      key, policy, status: review.screening_status as ReviewProof["status"],
      runId: review.screening_run_id,
      arabicHash: review.source_hash,
      translationHash: review.translation_hash,
      technicalFallback,
      passes: normalizedPasses,
    });
  }
  return { proofs, issues, countScreened, countStructurallyReady, countTechnicalFallback };
}
