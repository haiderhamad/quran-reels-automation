import { createHash } from "node:crypto";
import type { RunRecord } from "./schema";
import { EDITORIAL_POLICY_VERSION, isEligibleEditorialRange } from "./editorial-coverage";
import reconciliationArtifact from "./editorial-reconciliation";

export const APPROVED_REELS_RECONCILIATION_POLICY_VERSION = "editorial-reconciliation-v1";
export type AuthorizedEditorialRanges = ReadonlySet<string>;
export function editorialRangeKey(surah: number, first: number, last: number): string {
  return `${surah}:${first}-${last}`;
}

/** Exact Facebook object identity, independent of host aliases and tracking parameters. */
function facebookUrlIdentity(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port
      || !["facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"].includes(url.hostname)) return null;
    const path = url.pathname.replace(/\/+$/u, "");
    const reel = path.match(/^\/(?:reel|reels)\/(\d+)$/u);
    if (reel) return reel[1];
    const post = path.match(/^\/(\d+_\d+)$/u);
    if (post) return post[1];
    const pagePost = path.match(/^\/(\d+)\/posts\/(\d+)$/u);
    if (pagePost) return `${pagePost[1]}_${pagePost[2]}`;
    const video = path.match(/^\/[^/]+\/videos\/(\d+)$/u);
    if (video) return video[1];
    return null;
  } catch { return null; }
}

function facebookIdentityAliases(value: string): string[] {
  const identity = facebookUrlIdentity(value) ?? value;
  return /^\d+_\d+$/u.test(identity) ? [identity, identity.split("_")[1]] : [identity];
}

const RECONCILIATION_BY_KEY = new Map(
  reconciliationArtifact.entries.map((entry) => [entry.key, entry]),
);
const BLOCKED_EDITORIAL_RUN_IDS = new Set(reconciliationArtifact.entries
  .filter((entry) => entry.editorial_verdict !== "KEEP")
  .map((entry) => entry.run_id));
const BLOCKED_PLATFORM_KEYS = new Set(reconciliationArtifact.entries
  .filter((entry) => entry.editorial_verdict !== "KEEP"
    || entry.classification !== "VALID_HISTORICAL_DELIVERY")
  .map((entry) => entry.key));
const BLOCKED_PUBLICATION_IDENTITIES = new Set(reconciliationArtifact.entries
  .filter((entry) => entry.editorial_verdict !== "KEEP"
    || entry.classification !== "VALID_HISTORICAL_DELIVERY")
  .flatMap((entry) => [entry.platform_media_id, entry.public_url, entry.facebook_page_post_id]
    .filter((value): value is string => typeof value === "string" && !!value)
    .flatMap((value) => (entry.platform === "facebook" ? facebookIdentityAliases(value) : [value])
      .map((identity) => `${entry.platform}:${identity}`))));

const TRANSLATION_KEYS: Record<string, string> = {
  en: "english_rwwad",
  de: "german_rwwad",
};
const SUBTITLE_HASH_FIELDS: Record<string, string> = {
  en: "english_source_hash",
  de: "german_source_hash",
};
const REMOVED_OR_UNREVIEWED = new Set([
  "DELETE_CONFIRMED", "REMOVED", "DELETED", "RETRACTED", "REVIEW", "EXCLUDED_CONFIRMED",
  "RETRACTION_CONFIRMED", "RETRACTION_REQUIRED", "UNCERTAIN_REVIEW",
]);

export interface BufferPlatformDeliveryEvidence {
  state: string;
  buffer_post_id: string | null;
  buffer_post_status: string | null;
}

function normalizedHash(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const normalized = value.trim().split(/\s+/u).join(" ");
  return createHash("sha256").update(normalized, "utf8").digest("hex");
}

function isTimestamp(value: string | null | undefined): boolean {
  if (!value) return false;
  const time = Date.parse(value);
  return Number.isFinite(time);
}

function hasPerRecordDeliveryEvidence(record: RunRecord, platform: "facebook" | "instagram", authorizedRanges?: AuthorizedEditorialRanges): boolean {
  const reconciled = RECONCILIATION_BY_KEY.get(`${record.run_id}:${platform}`);
  if (reconciled) {
    if (reconciled.classification !== "VALID_HISTORICAL_DELIVERY"
      || reconciled.editorial_verdict !== "KEEP"
      || reconciled.run_id !== record.run_id || reconciled.platform !== platform) return false;
    const caption = record.provenance.caption_ayahs;
    if (!Array.isArray(caption) || caption.length !== reconciled.verse_keys.length
      || caption.length !== reconciled.content_identity_sha256.length
      || caption.some((ayah, index) => ayah.verse_key !== reconciled.verse_keys[index]
        || ayah.arabic_normalized_sha256 !== reconciled.content_identity_sha256[index])) return false;
    const mediaId = platform === "facebook" ? record.facebook_media_id : record.instagram_media_id;
    return !!(reconciled.platform_media_id && reconciled.platform_media_id === mediaId)
      || !!(platform === "facebook" && reconciled.public_url
        && facebookUrlIdentity(reconciled.public_url)
        && facebookUrlIdentity(reconciled.public_url) === facebookUrlIdentity(record.facebook_reel_url));
  }
  const provenance = record.provenance;
  const authorization = provenance.production_authorization;
  if (authorizedRanges && authorization && typeof authorization === "object"
    && authorization.policy_version === EDITORIAL_POLICY_VERSION
    && authorization.surah_number === record.surah_number
    && authorization.first_ayah === record.first_ayah
    && authorization.last_ayah === record.last_ayah
    && Array.isArray(authorization.authorization_ids) && authorization.authorization_ids.length > 0
    && authorization.authorization_ids.every((id: unknown) => Number.isSafeInteger(id) && Number(id) > 0)
    && typeof authorization.authorization_fingerprint === "string"
    && /^[0-9a-f]{32}$/u.test(authorization.authorization_fingerprint)) return true;
  return provenance.editorial_reconciliation_policy_version === APPROVED_REELS_RECONCILIATION_POLICY_VERSION
    && provenance.editorial_reconciliation_status === "APPROVED_AT_GENERATION";
}

/** Validate the reel identity and its Quran/source-bound production record. */
function isApprovedSourceBoundProductionRun(record: RunRecord, platform: "facebook" | "instagram", authorizedRanges?: AuthorizedEditorialRanges): boolean {
  const reconciliation = RECONCILIATION_BY_KEY.get(`${record.run_id}:${platform}`);
  const hasValidatedHistoricalPolicy = reconciliation?.classification === "VALID_HISTORICAL_DELIVERY"
    && reconciliation.editorial_verdict === "KEEP";
  if (
    !["scheduled", "manual_production"].includes(record.run_kind || "")
    || record.status !== "SUCCESS"
    || record.final_safety_passed !== true
    || !record.media_key
    || !isTimestamp(record.started_at)
    || !isTimestamp(record.completed_at)
    || !Number.isInteger(record.surah_number)
    || !Number.isInteger(record.first_ayah)
    || !Number.isInteger(record.last_ayah)
    || !(authorizedRanges
      ? authorizedRanges.has(editorialRangeKey(record.surah_number!, record.first_ayah!, record.last_ayah!))
      : isEligibleEditorialRange(record.surah_number!, record.first_ayah!, record.last_ayah!))
    || BLOCKED_EDITORIAL_RUN_IDS.has(record.run_id)
    || BLOCKED_PLATFORM_KEYS.has(`${record.run_id}:${platform}`)
    || reconciliationArtifact.policy_version !== APPROVED_REELS_RECONCILIATION_POLICY_VERSION
    || reconciliationArtifact.editorial_policy_version !== EDITORIAL_POLICY_VERSION
  ) return false;

  const provenance = record.provenance;
  if (!provenance || typeof provenance !== "object"
    || (provenance.theme_policy_version !== EDITORIAL_POLICY_VERSION && !hasValidatedHistoricalPolicy)
    || provenance.quran_source !== "Quran Foundation Content API v4 (text_uthmani)") return false;
  if (REMOVED_OR_UNREVIEWED.has(provenance.editorial_cleanup_status)
    || REMOVED_OR_UNREVIEWED.has(provenance.publication_state)) return false;

  const caption = provenance.caption_ayahs;
  const subtitle = provenance.subtitle_ayahs;
  const languages = record.translation_languages;
  const count = record.last_ayah! - record.first_ayah! + 1;
  if (!Array.isArray(caption) || !Array.isArray(subtitle)
    || caption.length !== count || subtitle.length !== count
    || !Array.isArray(languages) || !languages.length
    || new Set(languages).size !== languages.length
    || languages.some((language) => !(language in TRANSLATION_KEYS))) return false;

  for (let index = 0; index < count; index += 1) {
    const key = `${record.surah_number}:${record.first_ayah! + index}`;
    const source = caption[index];
    const render = subtitle[index];
    if (!source || typeof source !== "object" || !render || typeof render !== "object"
      || source.verse_key !== key || render.verse_key !== key) return false;
    const arabicHash = normalizedHash(source.arabic_text);
    if (!arabicHash || source.arabic_normalized_sha256 !== arabicHash
      || render.arabic_source_hash !== arabicHash) return false;
    const translations = source.translations;
    if (!translations || typeof translations !== "object" || Array.isArray(translations)
      || Object.keys(translations).length !== languages.length) return false;
    for (const language of languages) {
      const translation = translations[language];
      const runSource = record.translation_sources[language];
      const translatedHash = normalizedHash(translation?.text);
      if (!translatedHash || translation.normalized_sha256 !== translatedHash
        || render[SUBTITLE_HASH_FIELDS[language]] !== translatedHash
        || translation.translation_key !== TRANSLATION_KEYS[language]
        || runSource?.translation_key !== TRANSLATION_KEYS[language]
        || !translation.translation_version
        || translation.translation_version !== runSource.version) return false;
    }
  }

  return true;
}

/**
 * True only when this platform has identity-bound completed delivery evidence.
 * The other platform on the same run is evaluated independently because a
 * Buffer acceptance or failure on one channel says nothing about the other.
 */
export function isApprovedPlatformDelivery(
  record: RunRecord,
  platform: "facebook" | "instagram",
  bufferEvidence?: BufferPlatformDeliveryEvidence,
  authorizedRanges?: AuthorizedEditorialRanges,
): boolean {
  if (!isApprovedSourceBoundProductionRun(record, platform, authorizedRanges)) return false;

  if (platform === "facebook") {
    const identity = facebookUrlIdentity(record.facebook_reel_url) ?? record.facebook_media_id;
    const acceptedBufferDelivery = record.facebook_status === "ACCEPTED"
      && bufferEvidence?.state === "SUCCESS"
      && bufferEvidence.buffer_post_status === "sent"
      && bufferEvidence.buffer_post_id === record.facebook_media_id;
    if ((record.facebook_status !== "SUCCESS" && !acceptedBufferDelivery) || !identity
      || facebookIdentityAliases(identity).some((alias) => BLOCKED_PUBLICATION_IDENTITIES.has(`facebook:${alias}`))) {
      return false;
    }
  } else {
    const acceptedBufferDelivery = record.instagram_status === "ACCEPTED"
      && bufferEvidence?.state === "SUCCESS"
      && bufferEvidence.buffer_post_status === "sent"
      && bufferEvidence.buffer_post_id === record.instagram_media_id;
    if ((record.instagram_status !== "SUCCESS" && !acceptedBufferDelivery) || !record.instagram_media_id
      || BLOCKED_PUBLICATION_IDENTITIES.has(`instagram:${record.instagram_media_id}`)) return false;
  }

  return hasPerRecordDeliveryEvidence(record, platform, authorizedRanges);
}

/** True only when every completed platform delivery on the run is verified. */
export function isApprovedDeliveredReel(record: RunRecord, authorizedRanges?: AuthorizedEditorialRanges): boolean {
  const deliveredPlatforms: Array<"facebook" | "instagram"> = [];
  if (record.facebook_status === "SUCCESS" && !!(record.facebook_media_id || facebookUrlIdentity(record.facebook_reel_url))) {
    deliveredPlatforms.push("facebook");
  }
  if (record.instagram_status === "SUCCESS" && !!record.instagram_media_id) {
    deliveredPlatforms.push("instagram");
  }
  return deliveredPlatforms.length > 0
    && deliveredPlatforms.every((platform) => isApprovedPlatformDelivery(record, platform, undefined, authorizedRanges));
}
