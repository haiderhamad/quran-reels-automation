import { CANONICAL_SURAH_AYAH_COUNTS, TOTAL_CANONICAL_AYAHS } from "./canonical-quran";

export interface EditorialSurahProgress {
  surah_number: number;
  total_ayahs: number;
  screened_ayahs: number;
  remaining_ayahs: number;
  eligible_candidates: number;
  production_approved_ayahs: number;
}

export function validateSurahProgress(value: unknown): EditorialSurahProgress[] {
  if (!Array.isArray(value) || value.length !== 114) throw new Error("Expected 114 canonical surahs.");
  const rows = [...value].sort((a, b) => a.surah_number - b.surah_number);
  rows.forEach((row, index) => {
    if (!row || row.surah_number !== index + 1
      || row.total_ayahs !== CANONICAL_SURAH_AYAH_COUNTS[index]
      || [row.screened_ayahs, row.remaining_ayahs, row.eligible_candidates, row.production_approved_ayahs]
        .some((number) => !Number.isInteger(number) || number < 0)
      || row.screened_ayahs + row.remaining_ayahs !== row.total_ayahs
      || row.eligible_candidates > row.screened_ayahs
      || row.production_approved_ayahs > row.total_ayahs) throw new Error("Invalid canonical surah progress.");
  });
  if (rows.reduce((sum, row) => sum + row.total_ayahs, 0) !== TOTAL_CANONICAL_AYAHS) throw new Error("Canonical total mismatch.");
  return rows;
}

export const EDITORIAL_FILTERS = ["ALL", "NOT_SCREENED", "ELIGIBLE_CANDIDATE", "INELIGIBLE_CANDIDATE", "AMBIGUOUS_REVIEW_REQUIRED"] as const;
export type EditorialFilter = typeof EDITORIAL_FILTERS[number];
export function editorialPagination(value: string | undefined, maxPage = 250): number {
  const page = Number(value ?? 1);
  return Number.isSafeInteger(page) && page >= 1 ? Math.min(page, maxPage) : 1;
}
