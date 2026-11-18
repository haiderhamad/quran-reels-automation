import type { RunRecord } from "./schema";
import { getEligibleAyahKeys } from "./editorial-coverage";
import { isApprovedPlatformDelivery, type AuthorizedEditorialRanges } from "./approved-reels";
import reconciliation from "./editorial-reconciliation";
import { z } from "zod";

export type PublishingPlatform = "facebook" | "instagram";
export type BufferClaimState = "DISPATCHING" | "SUCCESS" | "FAILED" | "UNKNOWN_OUTCOME";

export interface BufferPublicationClaim {
  run_id: string;
  platform: PublishingPlatform;
  publisher_backend: "buffer";
  channel_id: string;
  state: BufferClaimState;
  buffer_post_id: string | null;
  buffer_post_status: string | null;
  external_link: string | null;
  accepted_at: string | null;
  last_error_code: string | null;
  updated_at: string;
}

export const BufferPublicationClaimSchema = z.object({
  run_id: z.string().min(1),
  platform: z.enum(["facebook", "instagram"]),
  publisher_backend: z.literal("buffer"),
  channel_id: z.string().min(1),
  state: z.enum(["DISPATCHING", "SUCCESS", "FAILED", "UNKNOWN_OUTCOME"]),
  buffer_post_id: z.string().nullable(),
  buffer_post_status: z.string().nullable(),
  external_link: z.string().nullable(),
  accepted_at: z.string().nullable(),
  last_error_code: z.string().nullable(),
  updated_at: z.string().min(1),
});

export interface PlatformOperationsSummary {
  configured: boolean;
  bufferReachability: "Not verified";
  latestAttempt: {
    status: string;
    occurredAt: string | null;
    errorCode: string | null;
  } | null;
  lastConfirmedHistoricalDeliveryAt: string | null;
  historicallyVerifiedDeliveryRecords: number;
  operationalHealth: "Not configured" | "Degraded" | "Awaiting confirmation" | "Needs attention" | "No recent attempt" | "Historical delivery evidence";
}

export interface PublishingProgressSummary {
  approvedAyahs: number;
  liveVerifiedPublishedAyahs: null;
  liveRemainingApprovedAyahs: null;
  livePublicationPercentage: null;
  historicallyVerifiedApprovedAyahs: number;
  historicallyVerifiedAyahKeys: readonly string[];
  facebookHistoricallyVerifiedAyahs: number;
  instagramHistoricallyVerifiedAyahs: number;
  approvedAyahsWithoutVerifiedHistoricalDelivery: number;
  unverifiedDeliveryOutcomes: number;
  reconciliationUncertainReviews: number;
  reconciliationRetractionsRequired: number;
  reconciliationUnresolvedOutcomes: number;
  reconciliationValidHistoricalDeliveries: number;
  confirmedRetractionOutcomes: number;
  reconciliationGeneratedAt: string | null;
  platforms: Record<PublishingPlatform, PlatformOperationsSummary>;
}

const RECONCILIATION_BY_KEY = new Map(
  reconciliation.entries.map((entry) => [entry.key, entry]),
);

function attemptStatus(record: RunRecord, platform: PublishingPlatform): string {
  return platform === "facebook" ? record.facebook_status : record.instagram_status;
}

function attemptFailureCode(record: RunRecord, platform: PublishingPlatform): string | null {
  const value = platform === "facebook" ? record.facebook_failure_reason : record.instagram_failure_reason;
  if (typeof value !== "string") return null;
  const code = value.trim().match(/^([A-Z][A-Z0-9_]{2,79})(?::|$)/u)?.[1];
  return code ?? null;
}

function attemptTimestamp(record: RunRecord): string | null {
  const value = record.completed_at || record.started_at;
  return value && Number.isFinite(Date.parse(value)) ? value : null;
}

function compareAttemptNewestFirst(a: RunRecord, b: RunRecord): number {
  return Date.parse(attemptTimestamp(b) || "") - Date.parse(attemptTimestamp(a) || "");
}

function confirmedDeliveryTimestamp(
  record: RunRecord,
  platform: PublishingPlatform,
  matchingClaims: readonly BufferPublicationClaim[],
): string | null {
  const platformPublishedAt = platform === "facebook" ? record.facebook_published_at : null;
  if (platformPublishedAt && Number.isFinite(Date.parse(platformPublishedAt))) return platformPublishedAt;

  const [claim] = matchingClaims;
  return claim?.state === "SUCCESS"
    && claim.buffer_post_status === "sent"
    && Number.isFinite(Date.parse(claim.updated_at))
    ? claim.updated_at
    : null;
}

function hasVerifiedHistoricalDelivery(
  record: RunRecord,
  platform: PublishingPlatform,
  matchingClaims: readonly BufferPublicationClaim[],
  authorizedRanges?: AuthorizedEditorialRanges,
): boolean {
  const reconciliation = RECONCILIATION_BY_KEY.get(`${record.run_id}:${platform}`);
  const needsBufferEvidence = record.publisher_backend === "buffer"
    || !!reconciliation?.buffer_post_id
    || matchingClaims.length > 0;
  if (!needsBufferEvidence) return isApprovedPlatformDelivery(record, platform, undefined, authorizedRanges);
  if (record.publisher_backend === "meta" || matchingClaims.length !== 1) return false;

  const [claim] = matchingClaims;
  const platformMediaId = platform === "facebook" ? record.facebook_media_id : record.instagram_media_id;
  if (!claim || claim.state !== "SUCCESS" || claim.buffer_post_status !== "sent"
    || !claim.buffer_post_id || !platformMediaId || claim.buffer_post_id !== platformMediaId
    || (reconciliation?.buffer_post_id && claim.buffer_post_id !== reconciliation.buffer_post_id)) return false;
  return isApprovedPlatformDelivery(record, platform, claim, authorizedRanges);
}

function platformOperations(
  records: readonly RunRecord[],
  claimsByRunPlatform: ReadonlyMap<string, BufferPublicationClaim[]>,
  platform: PublishingPlatform,
  configured: boolean,
  authorizedRanges?: AuthorizedEditorialRanges,
): PlatformOperationsSummary {
  const eligibleAttempts = records.filter((record) =>
    ["scheduled", "manual_production"].includes(record.run_kind || "")
    && !["NOT_ATTEMPTED", "NOT_CONFIGURED"].includes(attemptStatus(record, platform))
  ).sort(compareAttemptNewestFirst);
  const latest = eligibleAttempts[0];
  const verifiedRecords = records
    .filter((record) => ["scheduled", "manual_production"].includes(record.run_kind || "")
      && hasVerifiedHistoricalDelivery(record, platform, claimsByRunPlatform.get(`${record.run_id}:${platform}`) ?? [], authorizedRanges))
    .sort(compareAttemptNewestFirst);
  const verifiedRunIds = new Set(verifiedRecords.map((record) => record.run_id));
  const publishedAt = verifiedRecords
    .map((record) => confirmedDeliveryTimestamp(
      record,
      platform,
      claimsByRunPlatform.get(`${record.run_id}:${platform}`) ?? [],
    ))
    .filter((value): value is string => !!value)
    .sort((a, b) => Date.parse(b) - Date.parse(a))[0] ?? null;

  let operationalHealth: PlatformOperationsSummary["operationalHealth"];
  if (!configured) operationalHealth = "Not configured";
  else if (!latest) operationalHealth = "No recent attempt";
  else {
    const status = attemptStatus(latest, platform);
    if (status === "FAILED") operationalHealth = "Degraded";
    else if (status === "ACCEPTED") operationalHealth = "Awaiting confirmation";
    else if (status === "UNKNOWN" || status === "BLOCKED") operationalHealth = "Needs attention";
    else if (status === "SUCCESS" && hasVerifiedHistoricalDelivery(
      latest,
      platform,
      claimsByRunPlatform.get(`${latest.run_id}:${platform}`) ?? [],
      authorizedRanges,
    )) {
      operationalHealth = "Historical delivery evidence";
    } else operationalHealth = "Needs attention";
  }

  return {
    configured,
    bufferReachability: "Not verified",
    latestAttempt: latest ? {
      status: attemptStatus(latest, platform),
      occurredAt: attemptTimestamp(latest),
      errorCode: attemptFailureCode(latest, platform),
    } : null,
    lastConfirmedHistoricalDeliveryAt: publishedAt && Number.isFinite(Date.parse(publishedAt))
      ? publishedAt
      : null,
    historicallyVerifiedDeliveryRecords: verifiedRunIds.size,
    operationalHealth,
  };
}

/**
 * Current live presence is deliberately left unknown: the project has no
 * platform presence feed. Historical delivery proof and Buffer queue state
 * remain separate and never become a claim that a Reel is currently live.
 */
export function computePublishingProgress(
  records: readonly RunRecord[],
  claims: readonly BufferPublicationClaim[],
  configured: Record<PublishingPlatform, boolean>,
  approvedKeys: ReadonlySet<string> = getEligibleAyahKeys(),
  authorizedRanges?: AuthorizedEditorialRanges,
): PublishingProgressSummary {
  const publishedByPlatform: Record<PublishingPlatform, Set<string>> = {
    facebook: new Set<string>(),
    instagram: new Set<string>(),
  };
  const recordById = new Map(records.map((record) => [record.run_id, record]));
  const claimsByRunPlatform = new Map<string, BufferPublicationClaim[]>();
  for (const claim of claims) {
    const key = `${claim.run_id}:${claim.platform}`;
    claimsByRunPlatform.set(key, [...(claimsByRunPlatform.get(key) ?? []), claim]);
  }

  for (const record of records) {
    if (!["scheduled", "manual_production"].includes(record.run_kind || "")) continue;
    for (const platform of ["facebook", "instagram"] as const) {
      if (!hasVerifiedHistoricalDelivery(record, platform, claimsByRunPlatform.get(`${record.run_id}:${platform}`) ?? [], authorizedRanges)) continue;
      if (!Number.isInteger(record.surah_number) || !Number.isInteger(record.first_ayah) || !Number.isInteger(record.last_ayah)) continue;
      for (let ayah = record.first_ayah!; ayah <= record.last_ayah!; ayah += 1) {
        const key = `${record.surah_number}:${ayah}`;
        if (approvedKeys.has(key)) publishedByPlatform[platform].add(key);
      }
    }
  }

  const verifiedKeys = new Set([...publishedByPlatform.facebook, ...publishedByPlatform.instagram]);
  const seenClaims = new Set<string>();
  let unverifiedDeliveryOutcomes = 0;
  for (const claim of claims) {
    const claimIdentity = `${claim.run_id}:${claim.platform}:${claim.publisher_backend}:${claim.channel_id}`;
    if (seenClaims.has(claimIdentity)) continue;
    seenClaims.add(claimIdentity);
    const claimKey = `${claim.run_id}:${claim.platform}`;
    const entry = RECONCILIATION_BY_KEY.get(claimKey);
    const record = recordById.get(claim.run_id);

    if (entry?.classification === "RETRACTION_CONFIRMED") continue;
    if (entry?.classification === "RETRACTION_REQUIRED" || entry?.classification === "UNCERTAIN_REVIEW") {
      unverifiedDeliveryOutcomes += 1;
      continue;
    }
    if (entry?.classification === "VALID_HISTORICAL_DELIVERY") {
      if (record && hasVerifiedHistoricalDelivery(record, claim.platform, claimsByRunPlatform.get(claimKey) ?? [], authorizedRanges)) continue;
      unverifiedDeliveryOutcomes += 1;
      continue;
    }
    if (record && hasVerifiedHistoricalDelivery(record, claim.platform, claimsByRunPlatform.get(claimKey) ?? [], authorizedRanges)) continue;
    if (claim.state === "FAILED") continue;
    // Buffer acceptance, a sending state, missing identity evidence, or an
    // unknown dispatch result all require verification before they count.
    unverifiedDeliveryOutcomes += 1;
  }

  const reconciliationUncertainReviews = reconciliation.entries.filter((entry) =>
    entry.classification === "UNCERTAIN_REVIEW"
  ).length;
  const reconciliationRetractionsRequired = reconciliation.entries.filter((entry) =>
    entry.classification === "RETRACTION_REQUIRED"
  ).length;
  const reconciliationUnresolvedOutcomes = reconciliationUncertainReviews + reconciliationRetractionsRequired;
  const reconciliationValidHistoricalDeliveries = reconciliation.entries.filter((entry) =>
    entry.classification === "VALID_HISTORICAL_DELIVERY" && entry.editorial_verdict === "KEEP"
  ).length;
  const confirmedRetractionOutcomes = reconciliation.entries.filter((entry) =>
    entry.classification === "RETRACTION_CONFIRMED"
  ).length;

  return {
    approvedAyahs: approvedKeys.size,
    liveVerifiedPublishedAyahs: null,
    liveRemainingApprovedAyahs: null,
    livePublicationPercentage: null,
    historicallyVerifiedApprovedAyahs: verifiedKeys.size,
    historicallyVerifiedAyahKeys: [...verifiedKeys].sort(),
    facebookHistoricallyVerifiedAyahs: publishedByPlatform.facebook.size,
    instagramHistoricallyVerifiedAyahs: publishedByPlatform.instagram.size,
    approvedAyahsWithoutVerifiedHistoricalDelivery: approvedKeys.size - verifiedKeys.size,
    unverifiedDeliveryOutcomes,
    reconciliationUncertainReviews,
    reconciliationRetractionsRequired,
    reconciliationUnresolvedOutcomes,
    reconciliationValidHistoricalDeliveries,
    confirmedRetractionOutcomes,
    reconciliationGeneratedAt: reconciliation.generated_at_utc,
    platforms: {
      facebook: platformOperations(records, claimsByRunPlatform, "facebook", configured.facebook, authorizedRanges),
      instagram: platformOperations(records, claimsByRunPlatform, "instagram", configured.instagram, authorizedRanges),
    },
  };
}
