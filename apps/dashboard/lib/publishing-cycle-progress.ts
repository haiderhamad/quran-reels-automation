import { isCanonicalAyahKey } from "./canonical-quran";

export interface AuthorizedSurahProgress {
  eligibleSurahs: number;
  startedSurahs: number;
  completedSurahs: number;
  remainingSurahs: number;
  percentage: number;
  eligibleAyahs: number;
  completedAyahs: number;
}

/**
 * Count distinct approved ayahs and surahs without conflating editorial
 * approval, generated Reel coverage, and actual platform delivery.
 *
 * Call with verified platform deliveries for publication metrics, or with
 * the current coverage_state keys for generation-cycle metrics. Do not mix.
 */
export function summarizeAuthorizedSurahProgress(
  authorizedAyahKeys: ReadonlySet<string>,
  observedAyahKeys: Iterable<string>,
): AuthorizedSurahProgress {
  for (const key of authorizedAyahKeys) {
    if (!isCanonicalAyahKey(key)) {
      throw new Error("Approved corpus contains a non-canonical ayah identity.");
    }
  }

  const eligibleBySurah = new Map<number, number>();
  const observedBySurah = new Map<number, number>();
  const observedAyahs = new Set<string>();
  for (const key of authorizedAyahKeys) {
    const surah = Number(key.split(":")[0]);
    eligibleBySurah.set(surah, (eligibleBySurah.get(surah) ?? 0) + 1);
  }
  for (const key of observedAyahKeys) {
    // Unapproved/retired identities cannot raise the current policy percentage.
    if (!authorizedAyahKeys.has(key) || observedAyahs.has(key)) continue;
    observedAyahs.add(key);
    const surah = Number(key.split(":")[0]);
    observedBySurah.set(surah, (observedBySurah.get(surah) ?? 0) + 1);
  }
  const completedSurahs = [...eligibleBySurah].filter(([surah, target]) =>
    observedBySurah.get(surah) === target,
  ).length;

  return {
    eligibleSurahs: eligibleBySurah.size,
    startedSurahs: observedBySurah.size,
    completedSurahs,
    remainingSurahs: eligibleBySurah.size - completedSurahs,
    percentage: eligibleBySurah.size
      ? Number(((completedSurahs / eligibleBySurah.size) * 100).toFixed(1))
      : 0,
    eligibleAyahs: authorizedAyahKeys.size,
    completedAyahs: observedAyahs.size,
  };
}
