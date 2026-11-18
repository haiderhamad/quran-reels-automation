// Calm Reels v1: authoritative FRESH posting cycle, never historic generated coverage.
// Import this only into server-side Supabase reads; no client credentials.
export const CALM_REELS_FROZEN_SHA256 =
  "df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d";

export interface CalmReelsCycleProgress {
  cycleNumber: number;
  totalAyahs: number;
  confirmedAyahs: number;
  nextAyahKey: string | null;
  startedAt: string;
}

export function calculateCalmReelsCycleProgress(
  state: unknown,
  recordedKeys: readonly string[],
  shortlistedKeys: ReadonlySet<string>,
): CalmReelsCycleProgress {
  if (!state || typeof state !== "object") throw new Error("Missing fresh publication cycle.");
  const row = state as Record<string, unknown>;
  if (row.catalog_id !== "calm-reels-editorial-shortlist-v1"
    || row.catalog_sha256 !== CALM_REELS_FROZEN_SHA256
    || row.total_ayahs !== 2211
    || !Number.isSafeInteger(row.cycle_number)
    || Number(row.cycle_number) < 1
    || typeof row.started_at !== "string"
    || !Number.isFinite(Date.parse(row.started_at))
    || shortlistedKeys.size !== 2211) {
    throw new Error("Fresh publication cycle does not match verified catalog.");
  }
  const posted = new Set<string>();
  for (const key of recordedKeys) {
    if (!shortlistedKeys.has(key) || posted.has(key)) {
      throw new Error("Malformed fresh publication receipt.");
    }
    posted.add(key);
  }
  return {
    cycleNumber: Number(row.cycle_number),
    totalAyahs: 2211,
    confirmedAyahs: posted.size,
    nextAyahKey: [...shortlistedKeys].find((key) => !posted.has(key)) ?? null,
    startedAt: row.started_at,
  };
}
