import shortlist from "../../../data/editorial/calm_reels_shortlist_v1.json";
import { isCanonicalAyahKey } from "./canonical-quran";

/**
 * Frozen PRODUCT EDITORIAL topic scope; not an approved production catalog.
 * The verified publishing-cycle ledger must separately validate platform
 * confirmation, source/rights and reviewed passage boundaries before use.
 */
export const CALM_REELS_SHORTLIST_VERSION = shortlist.catalog_id;
export const CALM_REELS_SHORTLIST_SIZE = shortlist.ayah_count;

// This explicit operator setting defaults to false. Enable only after the
// owner-v1 worker, verified receipts and atomic database claim/rollover have
// been deployed and validated. The frozen list alone cannot authorize posting.
export const CALM_REELS_V1_MANUAL_PUBLISH_READY =
  process.env.QURAN_REELS_V1_MANUAL_PUBLISH_READY === "true";
export const CALM_REELS_V1_MANUAL_PUBLISH_BLOCK_REASON =
  "Owner-v1 publishing is not released yet. The verified Buffer send and cycle claim must be activated first.";

export function getCalmReelsShortlistKeys(): ReadonlySet<string> {
  if (
    shortlist.schema_version !== 1 ||
    shortlist.catalog_id !== "calm-reels-editorial-shortlist-v1" ||
    shortlist.status !== "FROZEN_OWNER_EDITORIAL_SCOPE_ONLY" ||
    shortlist.publication_authorized !== false ||
    shortlist.independent_context_review_required !== true ||
    shortlist.ayah_count !== 2211 ||
    shortlist.ayahs.length !== 2211
  ) {
    throw new Error("Unexpected or unverified calm-Reels editorial shortlist.");
  }

  const keys = new Set<string>();
  for (const entry of shortlist.ayahs) {
    if (!isCanonicalAyahKey(entry.key) ||
        !/^[a-f0-9]{64}$/.test(entry.arabic_sha256) ||
        keys.has(entry.key)) {
      throw new Error("Invalid or duplicated frozen ayah identity/source digest.");
    }
    keys.add(entry.key);
  }
  return keys;
}

export interface CalmReelsLiveAuthorizationOverlap {
  shortlistAyahsAlreadyAuthorized: number;
  fullyInScopeAuthorizedPassages: number;
  ayahsInsideFullAuthorizedPassages: number;
}

/**
 * Read-only intersection of the owner topic list and genuinely granted exact
 * Supabase passages. It NEVER grants new ayahs or fragments complete passages.
 * Consumer must pass one fully reconciled authoritative server snapshot.
 */
export function summarizeCalmReelsLiveAuthorizationOverlap(
  liveAuthorizedAyahs: readonly string[],
  exactAuthorizedRanges: readonly string[],
): CalmReelsLiveAuthorizationOverlap {
  const scope = getCalmReelsShortlistKeys();
  const authorized = new Set<string>();
  for (const key of liveAuthorizedAyahs) {
    if (!isCanonicalAyahKey(key) || authorized.has(key)) {
      throw new Error("Malformed live authorized ayah identities.");
    }
    authorized.add(key);
  }
  let shortlistAyahsAlreadyAuthorized = 0;
  for (const key of authorized) {
    if (scope.has(key)) shortlistAyahsAlreadyAuthorized += 1;
  }
  const members = new Set<string>();
  const ranges = new Set<string>();
  let fullyInScopeAuthorizedPassages = 0;
  for (const id of exactAuthorizedRanges) {
    if (ranges.has(id)) throw new Error("Duplicate live authorized passage.");
    ranges.add(id);
    const parsed = id.match(/^([1-9]\d*):([1-9]\d*)-([1-9]\d*)$/u);
    if (!parsed) throw new Error("Malformed live authorized passage.");
    const surah = Number(parsed[1]);
    const first = Number(parsed[2]);
    const last = Number(parsed[3]);
    if (!Number.isSafeInteger(surah) || !Number.isSafeInteger(first)
      || !Number.isSafeInteger(last) || last < first
      || !isCanonicalAyahKey(`${surah}:${first}`)
      || !isCanonicalAyahKey(`${surah}:${last}`)) {
      throw new Error("Noncanonical live authorization range.");
    }
    const keys: string[] = [];
    for (let ayah = first; ayah <= last; ayah += 1) {
      keys.push(`${surah}:${ayah}`);
    }
    if (keys.every((key) => scope.has(key) && authorized.has(key))) {
      fullyInScopeAuthorizedPassages += 1;
      for (const key of keys) members.add(key);
    }
  }
  return {
    shortlistAyahsAlreadyAuthorized,
    fullyInScopeAuthorizedPassages,
    ayahsInsideFullAuthorizedPassages: members.size,
  };
}

/** Informational only: historical delivery is NOT a verified current cycle. */
export function countHistoricallyDeliveredShortlistedAyahs(
  historicallyVerifiedKeys: ReadonlySet<string>,
): number {
  const shortlisted = getCalmReelsShortlistKeys();
  let count = 0;
  for (const key of historicallyVerifiedKeys) {
    if (shortlisted.has(key)) count += 1;
  }
  return count;
}
