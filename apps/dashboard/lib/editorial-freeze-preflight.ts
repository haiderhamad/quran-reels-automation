import { CANONICAL_SURAH_AYAH_COUNTS, TOTAL_CANONICAL_AYAHS, isCanonicalAyahKey } from "./canonical-quran";

/** Structural preflight only. Authentic sources and independent reviewers need external audit. */
export interface ReviewProof {
  key: string;
  policy: string;
  status: "SCREENED" | "NOT_SCREENED";
  runId: string | null;
  arabicHash: string | null;
  translationHash: string | null;
  technicalFallback: boolean;
  passes: readonly {
    pass: 1 | 2;
    evaluationId: string;
    perspective: "primary" | "adversarial";
    runId: string;
    arabicHash: string;
    translationHash: string;
    promptHash: string;
    responseHash: string;
  }[];
}

const HASH = /^[a-f0-9]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;

function validStructure(row: ReviewProof, policy: string): boolean {
  if (row.status !== "SCREENED" || row.policy !== policy || row.technicalFallback
    || !row.runId || !IDENTIFIER.test(row.runId)
    || !row.arabicHash || !HASH.test(row.arabicHash)
    || !row.translationHash || !HASH.test(row.translationHash)
    || row.passes.length !== 2) return false;
  const [first, second] = [...row.passes].sort((a, b) => a.pass - b.pass);
  if (first.pass !== 1 || second.pass !== 2
    || first.perspective !== "primary" || second.perspective !== "adversarial"
    || !IDENTIFIER.test(first.evaluationId) || !IDENTIFIER.test(second.evaluationId)
    || first.evaluationId === second.evaluationId || first.promptHash === second.promptHash) return false;
  return [first, second].every((pass) =>
    pass.runId === row.runId && pass.arabicHash === row.arabicHash
    && pass.translationHash === row.translationHash
    && HASH.test(pass.promptHash) && HASH.test(pass.responseHash));
}

export interface ScreeningFreezePreflight {
  state: "INCOMPLETE" | "NEEDS_INDEPENDENT_SOURCE_AUDIT";
  canonicalTotal: number;
  suppliedRows: number;
  structurallyReviewed: number;
  missingRows: number;
  notScreened: number;
  invalidReviews: number;
  historicalFallbacks: number;
  completeSurahs: number;
  unresolvedKeysPreview: readonly string[];
}

/** Never automatically asserts safe-to-freeze merely from structural completeness. */
export function preflightQuranScreening(
  rows: readonly ReviewProof[], policy: string,
): ScreeningFreezePreflight {
  if (!IDENTIFIER.test(policy) || rows.length > TOTAL_CANONICAL_AYAHS) {
    throw new Error("Invalid policy or Quran export size");
  }
  const indexed = new Map<string, ReviewProof>();
  for (const row of rows) {
    if (!isCanonicalAyahKey(row.key) || indexed.has(row.key)) {
      throw new Error("Duplicate or noncanonical ayah identity");
    }
    if (row.status !== "SCREENED" && row.status !== "NOT_SCREENED") {
      throw new Error("Unsupported screening status");
    }
    indexed.set(row.key, row);
  }
  let missingRows = 0, notScreened = 0, invalidReviews = 0;
  let historicalFallbacks = 0, structurallyReviewed = 0, completeSurahs = 0;
  const unresolvedKeysPreview: string[] = [];
  for (let s = 1; s <= 114; s++) {
    let inSurah = 0;
    for (let a = 1; a <= CANONICAL_SURAH_AYAH_COUNTS[s - 1]; a++) {
      const key = `${s}:${a}`, row = indexed.get(key);
      if (!row) missingRows++;
      else if (row.status === "NOT_SCREENED") notScreened++;
      else if (!validStructure(row, policy)) {
        invalidReviews++;
        if (row.technicalFallback) historicalFallbacks++;
      } else {
        structurallyReviewed++;
        inSurah++;
        continue;
      }
      if (unresolvedKeysPreview.length < 12) unresolvedKeysPreview.push(key);
    }
    if (inSurah === CANONICAL_SURAH_AYAH_COUNTS[s - 1]) completeSurahs++;
  }
  return {
    state: structurallyReviewed === TOTAL_CANONICAL_AYAHS
      ? "NEEDS_INDEPENDENT_SOURCE_AUDIT" : "INCOMPLETE",
    canonicalTotal: TOTAL_CANONICAL_AYAHS, suppliedRows: indexed.size,
    structurallyReviewed, missingRows, notScreened, invalidReviews,
    historicalFallbacks, completeSurahs, unresolvedKeysPreview,
  };
}
