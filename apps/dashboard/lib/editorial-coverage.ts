import catalogData from "../../../data/reel_theme_catalog.json";
import { isCanonicalAyahKey } from "./canonical-quran";
import { TOTAL_CANONICAL_AYAHS } from "./schema";

interface EditorialCatalog {
  policy_version: string;
  ranges: readonly { surah: number; first_ayah: number; last_ayah: number }[];
  context_requirements: readonly { required_when_contains: string; require_any_of: readonly string[] }[];
}

const catalog: EditorialCatalog = catalogData;

export const EDITORIAL_POLICY_VERSION = catalog.policy_version;
export const CANONICAL_AYAH_COUNT = TOTAL_CANONICAL_AYAHS;

export interface EditorialCoverageSummary {
  canonicalAyahs: number;
  eligibleAyahs: number;
  eligibleCoveredAyahs: number;
  eligibleRemainingAyahs: number;
  coveragePercentage: number;
  historicallyCoveredAyahs: number;
  cycleNumber: number;
  policyVersion: string;
  sourceUpdatedAt: string | null;
  refreshedAt: string;
}

export function getEligibleAyahKeys(): Set<string> {
  const keys = new Set<string>();
  for (const range of catalog.ranges) {
    if (
      !Number.isInteger(range.surah) ||
      !Number.isInteger(range.first_ayah) ||
      !Number.isInteger(range.last_ayah) ||
      range.last_ayah < range.first_ayah ||
      !isCanonicalAyahKey(`${range.surah}:${range.first_ayah}`) ||
      !isCanonicalAyahKey(`${range.surah}:${range.last_ayah}`)
    ) {
      throw new Error("Editorial catalog contains an invalid ayah range.");
    }
    for (let ayah = range.first_ayah; ayah <= range.last_ayah; ayah += 1) {
      keys.add(`${range.surah}:${ayah}`);
    }
  }
  return keys;
}

/** Match the engine's complete-range and surrounding-context policy. */
export function isEligibleEditorialRange(surah: number, firstAyah: number, lastAyah: number): boolean {
  if (!Number.isInteger(surah) || !Number.isInteger(firstAyah) || !Number.isInteger(lastAyah)
    || firstAyah < 1 || lastAyah < firstAyah) return false;
  const eligible = getEligibleAyahKeys();
  const selected = new Set<string>();
  for (let ayah = firstAyah; ayah <= lastAyah; ayah += 1) {
    const key = `${surah}:${ayah}`;
    if (!eligible.has(key)) return false;
    selected.add(key);
  }
  return catalog.context_requirements.every((requirement) =>
    !selected.has(requirement.required_when_contains)
    || requirement.require_any_of.some((anchor) => selected.has(anchor)));
}

export function computeEditorialCoverage(
  coveredAyahs: readonly string[],
  cycleNumber: number,
  sourceUpdatedAt: string | null,
  refreshedAt = new Date().toISOString(),
): EditorialCoverageSummary {
  const eligible = getEligibleAyahKeys();
  if (coveredAyahs.some((key) => !isCanonicalAyahKey(key))) {
    throw new Error("Coverage state contains a non-canonical ayah key.");
  }
  const covered = new Set(coveredAyahs);
  let eligibleCoveredAyahs = 0;
  for (const key of eligible) {
    if (covered.has(key)) eligibleCoveredAyahs += 1;
  }

  const eligibleAyahs = eligible.size;
  return {
    canonicalAyahs: TOTAL_CANONICAL_AYAHS,
    eligibleAyahs,
    eligibleCoveredAyahs,
    eligibleRemainingAyahs: eligibleAyahs - eligibleCoveredAyahs,
    coveragePercentage: eligibleAyahs
      ? Number(((eligibleCoveredAyahs / eligibleAyahs) * 100).toFixed(1))
      : 0,
    historicallyCoveredAyahs: covered.size,
    cycleNumber,
    policyVersion: catalog.policy_version,
    sourceUpdatedAt,
    refreshedAt,
  };
}
