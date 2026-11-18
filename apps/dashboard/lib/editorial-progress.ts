import inventory from "../../../data/editorial_inventory.json";
import catalog from "../../../data/reel_theme_catalog.json";
import { getEligibleAyahKeys, EDITORIAL_POLICY_VERSION } from "./editorial-coverage";
import { isCanonicalAyahKey } from "./canonical-quran";
import { TOTAL_CANONICAL_AYAHS } from "./schema";

export type EditorialEligibilityStatus =
  | "APPROVED_REVIEWED"
  | "REVIEW_REQUIRED"
  | "NOT_YET_REVIEWED"
  | "EXCLUDED_CONFIRMED";

export interface EditorialInventoryRecord {
  canonical_key: string;
  editorial_eligibility_status: EditorialEligibilityStatus;
}

export interface EditorialProgressSummary {
  canonicalAyahs: number;
  approvedAyahs: number;
  requiresReviewAyahs: number;
  notYetReviewedAyahs: number;
  confirmedExcludedAyahs: number;
  completedDecisions: number;
  reviewCompletenessPercentage: number;
  policyVersion: string;
  catalogRanges: number;
}

const ALLOWED_STATUSES = new Set<EditorialEligibilityStatus>([
  "APPROVED_REVIEWED",
  "REVIEW_REQUIRED",
  "NOT_YET_REVIEWED",
  "EXCLUDED_CONFIRMED",
]);

/**
 * Count only recorded final editorial decisions as complete. A proposal that
 * still requires adjudication is incomplete. These counts summarize the local
 * inventory. Production authorization is checked separately by the worker.
 */
export function computeEditorialProgress(
  records: readonly EditorialInventoryRecord[],
  catalogKeys: ReadonlySet<string>,
  canonicalAyahs = TOTAL_CANONICAL_AYAHS,
  policyVersion = EDITORIAL_POLICY_VERSION,
  catalogRanges = catalog.ranges.length,
): EditorialProgressSummary {
  const seen = new Set<string>();
  const approvedKeys = new Set<string>();
  const counts: Record<EditorialEligibilityStatus, number> = {
    APPROVED_REVIEWED: 0,
    REVIEW_REQUIRED: 0,
    NOT_YET_REVIEWED: 0,
    EXCLUDED_CONFIRMED: 0,
  };

  for (const record of records) {
    if (!isCanonicalAyahKey(record.canonical_key) || seen.has(record.canonical_key)) {
      throw new Error("Editorial inventory contains a duplicate or non-canonical ayah key.");
    }
    if (!ALLOWED_STATUSES.has(record.editorial_eligibility_status)) {
      throw new Error("Editorial inventory contains an unsupported decision status.");
    }
    seen.add(record.canonical_key);
    counts[record.editorial_eligibility_status] += 1;
    if (record.editorial_eligibility_status === "APPROVED_REVIEWED") {
      approvedKeys.add(record.canonical_key);
    }
  }

  if (seen.size !== canonicalAyahs) {
    throw new Error("Editorial inventory does not cover the complete canonical Quran identity set.");
  }
  if (catalogKeys.size !== approvedKeys.size || [...catalogKeys].some((key) => !approvedKeys.has(key))) {
    throw new Error("Active catalog and approved editorial inventory identities do not match.");
  }

  const completedDecisions = counts.APPROVED_REVIEWED + counts.EXCLUDED_CONFIRMED;
  return {
    canonicalAyahs,
    approvedAyahs: counts.APPROVED_REVIEWED,
    requiresReviewAyahs: counts.REVIEW_REQUIRED,
    notYetReviewedAyahs: counts.NOT_YET_REVIEWED,
    confirmedExcludedAyahs: counts.EXCLUDED_CONFIRMED,
    completedDecisions,
    reviewCompletenessPercentage: canonicalAyahs
      ? Number(((completedDecisions / canonicalAyahs) * 100).toFixed(2))
      : 0,
    policyVersion,
    catalogRanges,
  };
}

export function getEditorialProgress(): EditorialProgressSummary {
  const records = inventory.records as EditorialInventoryRecord[];
  const declared = inventory.summary;
  const result = computeEditorialProgress(records, getEligibleAyahKeys());
  if (
    declared.APPROVED_REVIEWED !== result.approvedAyahs
    || declared.REVIEW_REQUIRED !== result.requiresReviewAyahs
    || declared.NOT_YET_REVIEWED !== result.notYetReviewedAyahs
    || declared.EXCLUDED_CONFIRMED !== result.confirmedExcludedAyahs
  ) {
    throw new Error("Editorial inventory summary does not match its ayah records.");
  }
  return result;
}
