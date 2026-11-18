import { createHash } from "node:crypto";
import { isCanonicalAyahKey } from "./canonical-quran";
import { summarizeAuthorizedSurahProgress } from "./publishing-cycle-progress";

/**
 * Offline, read-only contract for the proposed durable publishing-cycle backend.
 *
 * Inputs MUST be assembled by a trusted, source-verified server adapter.
 * This pure module neither verifies an external platform itself, nor mutates
 * Supabase, schedules a Reel, or advances a live production cycle.
 */
export interface FrozenPublicationCatalog {
  version: string;
  policyVersion: string;
  arabicSourceVersion: string;
  translationSourceVersion: string;
  fingerprint: string;
  approvedAyahKeys: readonly string[];
  sourceEvidence: readonly {
    key: string;
    arabicSha256: string;
    translationSha256: string;
  }[];
  passages: readonly {
    id: string;
    authorizationFingerprint: string;
    ayahKeys: readonly string[];
  }[];
}

/**
 * Hash the EXACT ordered selection manifest, including all approved source
 * evidence and exact passage authorizations. Pure SHA-256 is not a digital
 * signature: only a trusted backend may attest this digest and freeze the
 * manifest. Do not accept an arbitrary client-provided hash as authority.
 *
 * The canonical byte format is the UTF-8 JSON.stringify result of this
 * fixed-field object; do not silently reorder entries after freezing.
 */
export function fingerprintFrozenPublicationCatalog(
  catalog: Omit<FrozenPublicationCatalog, "fingerprint">,
): string {
  const canonical = JSON.stringify({
    protocol: "quran-reels-frozen-publication-catalog-v1",
    version: catalog.version,
    policyVersion: catalog.policyVersion,
    arabicSourceVersion: catalog.arabicSourceVersion,
    translationSourceVersion: catalog.translationSourceVersion,
    approvedAyahKeys: catalog.approvedAyahKeys,
    sourceEvidence: catalog.sourceEvidence.map((entry) => ({
      key: entry.key,
      arabicSha256: entry.arabicSha256,
      translationSha256: entry.translationSha256,
    })),
    passages: catalog.passages.map((entry) => ({
      id: entry.id,
      authorizationFingerprint: entry.authorizationFingerprint,
      ayahKeys: entry.ayahKeys,
    })),
  });
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

export interface PublicationCycleSnapshot {
  cycleNumber: number;
  catalog: FrozenPublicationCatalog;
}

export interface ConfirmedPublicationReceipt {
  cycleNumber: number;
  catalogFingerprint: string;
  platform: "facebook" | "instagram";
  runId: string;
  passageId: string;
  externalPostId: string;
  confirmedAt: string;
  ayahKeys: readonly string[];
}

export interface PendingPublicationAttempt {
  cycleNumber: number;
  catalogFingerprint: string;
  runId: string;
  passageId: string;
  state: "IN_FLIGHT" | "UNKNOWN_OUTCOME";
}

export type PublicationCycleState = "IN_PROGRESS" | "BLOCKED" | "READY_FOR_ROLLOVER";

export interface PublicationCycleEvaluation {
  cycleNumber: number;
  catalogVersion: string;
  state: PublicationCycleState;
  approvedSurahs: number;
  startedSurahs: number;
  completedSurahs: number;
  completedSurahPercentage: number;
  approvedAyahs: number;
  confirmedFacebookAyahs: number;
  confirmedFacebookAyahPercentage: number;
  remainingAyahs: number;
  pendingAttempts: number;
  conflictingDeliveries: number;
  nextEligiblePassageId: string | null;
}

const SHA256 = /^[a-f0-9]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const UTC_DATE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;

function assertIdentifier(value: string, label: string): void {
  if (!IDENTIFIER.test(value)) throw new Error(`Invalid ${label}`);
}

function assertExactArray(actual: readonly string[], expected: readonly string[], label: string): void {
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`Invalid ${label}: passage ayahs do not match the frozen catalog`);
  }
}

function validateCatalog(catalog: FrozenPublicationCatalog): {
  approved: ReadonlySet<string>;
  passagesById: ReadonlyMap<string, readonly string[]>;
} {
  assertIdentifier(catalog.version, "catalog version");
  assertIdentifier(catalog.policyVersion, "catalog policy version");
  assertIdentifier(catalog.arabicSourceVersion, "Arabic source version");
  assertIdentifier(catalog.translationSourceVersion, "translation source version");
  if (!SHA256.test(catalog.fingerprint)) throw new Error("Invalid frozen catalog fingerprint");
  if (!catalog.approvedAyahKeys.length || !catalog.passages.length) {
    throw new Error("An empty or unfinished Quran catalog cannot complete a publication cycle");
  }

  const approved = new Set<string>();
  for (const key of catalog.approvedAyahKeys) {
    if (!isCanonicalAyahKey(key)) throw new Error("Noncanonical approved ayah identity");
    if (approved.has(key)) throw new Error("Duplicate approved ayah identity");
    approved.add(key);
  }

  const evidenceKeys = new Set<string>();
  if (catalog.sourceEvidence.length !== approved.size) {
    throw new Error("Frozen catalog source evidence count does not match approved ayahs");
  }
  for (const entry of catalog.sourceEvidence) {
    if (!approved.has(entry.key) || evidenceKeys.has(entry.key)
      || !SHA256.test(entry.arabicSha256) || !SHA256.test(entry.translationSha256)) {
      throw new Error("Frozen catalog contains missing, duplicate or malformed Quran source evidence");
    }
    evidenceKeys.add(entry.key);
  }

  const passagesById = new Map<string, readonly string[]>();
  const represented = new Set<string>();
  for (const passage of catalog.passages) {
    assertIdentifier(passage.id, "passage id");
    if (passagesById.has(passage.id) || !passage.ayahKeys.length) {
      throw new Error("Duplicate or empty authorized passage");
    }
    if (!/^[a-f0-9]{32,64}$/.test(passage.authorizationFingerprint)) {
      throw new Error("Frozen authorized passage fingerprint is invalid");
    }
    const surah = Number(passage.ayahKeys[0].split(":")[0]);
    let previousAyah = 0;
    for (const [index, key] of passage.ayahKeys.entries()) {
      if (!approved.has(key) || !isCanonicalAyahKey(key)) {
        throw new Error("Passage contains unapproved or noncanonical ayah");
      }
      const [currentSurah, currentAyah] = key.split(":").map(Number);
      if (currentSurah !== surah || (index > 0 && currentAyah !== previousAyah + 1)) {
        throw new Error("Authorized passage is not a single contiguous surah range");
      }
      previousAyah = currentAyah;
      represented.add(key);
    }
    passagesById.set(passage.id, passage.ayahKeys);
  }
  if (represented.size !== approved.size) {
    throw new Error("Frozen approved ayahs lack an exact authorized passage");
  }
  if (fingerprintFrozenPublicationCatalog(catalog) !== catalog.fingerprint) {
    throw new Error("Frozen catalog fingerprint does not match Quran source or passage manifest");
  }
  return { approved, passagesById };
}

/**
 * The adapter MUST already have verified platform delivery evidence and mapped
 * it to the frozen policy/source-pinned catalog. A string supplied as a receipt
 * by an untrusted client is NOT independent evidence of publication.
 */
export function evaluatePublicationCycle(
  snapshot: PublicationCycleSnapshot,
  confirmedReceipts: readonly ConfirmedPublicationReceipt[],
  pendingAttempts: readonly PendingPublicationAttempt[],
): PublicationCycleEvaluation {
  if (!Number.isSafeInteger(snapshot.cycleNumber) || snapshot.cycleNumber < 1) {
    throw new Error("Invalid publication cycle number");
  }
  const { approved, passagesById } = validateCatalog(snapshot.catalog);
  const knownReceipts = new Map<string, string>();
  const runPostIds = new Map<string, string>();
  const postRunIds = new Map<string, string>();
  const runPassageIds = new Map<string, string>();
  const delivered = new Set<string>();
  let conflicts = 0;

  for (const receipt of confirmedReceipts) {
    if (receipt.cycleNumber !== snapshot.cycleNumber
      || receipt.catalogFingerprint !== snapshot.catalog.fingerprint) {
      throw new Error("Publication receipt belongs to a different cycle/catalog");
    }
    if (receipt.platform !== "facebook" && receipt.platform !== "instagram") {
      throw new Error("Unknown publication platform");
    }
    assertIdentifier(receipt.runId, "verified run id");
    assertIdentifier(receipt.externalPostId, "verified external post id");
    const passage = passagesById.get(receipt.passageId);
    if (!passage) throw new Error("Receipt references unapproved passage");
    assertExactArray(receipt.ayahKeys, passage, "confirmed delivery");
    if (!UTC_DATE.test(receipt.confirmedAt) || !Number.isFinite(Date.parse(receipt.confirmedAt))) {
      throw new Error("Receipt missing valid UTC confirmation time");
    }

    const key = `${receipt.platform}:${receipt.runId}:${receipt.externalPostId}`;
    const fingerprint = JSON.stringify(receipt);
    const existing = knownReceipts.get(key);
    if (existing !== undefined) {
      if (existing !== fingerprint) throw new Error("Contradictory replay of verified receipt");
      continue;
    }
    knownReceipts.set(key, fingerprint);

    const runKey = `${receipt.platform}:${receipt.runId}`;
    const postKey = `${receipt.platform}:${receipt.externalPostId}`;
    if ((runPostIds.has(runKey) && runPostIds.get(runKey) !== receipt.externalPostId)
      || (postRunIds.has(postKey) && postRunIds.get(postKey) !== receipt.runId)
      || (runPassageIds.has(runKey) && runPassageIds.get(runKey) !== receipt.passageId)) {
      throw new Error("Conflicting verified run/post identity");
    }
    runPostIds.set(runKey, receipt.externalPostId);
    postRunIds.set(postKey, receipt.runId);
    runPassageIds.set(runKey, receipt.passageId);

    // Only the designated primary platform completes the publication cycle.
    if (receipt.platform !== "facebook") continue;
    if (passage.some((ayah) => delivered.has(ayah))) conflicts++;
    for (const ayah of passage) delivered.add(ayah);
  }

  const reserved = new Set<string>();
  const uniqueAttempts = new Set<string>();
  const pendingRunPassages = new Map<string, string>();
  const pendingAyahRun = new Map<string, string>();
  let unknownOutcomes = 0;
  for (const attempt of pendingAttempts) {
    if (attempt.cycleNumber !== snapshot.cycleNumber
      || attempt.catalogFingerprint !== snapshot.catalog.fingerprint) {
      throw new Error("Unresolved attempt belongs to a different cycle/catalog");
    }
    assertIdentifier(attempt.runId, "pending run id");
    if (attempt.state !== "IN_FLIGHT" && attempt.state !== "UNKNOWN_OUTCOME") {
      throw new Error("Unknown pending publication state");
    }
    const passage = passagesById.get(attempt.passageId);
    if (!passage) throw new Error("Unresolved attempt references unapproved passage");
    if (pendingRunPassages.has(attempt.runId)
      && pendingRunPassages.get(attempt.runId) !== attempt.passageId) {
      throw new Error("Pending run has conflicting reserved passage");
    }
    pendingRunPassages.set(attempt.runId, attempt.passageId);
    const id = `${attempt.runId}:${attempt.passageId}`;
    if (uniqueAttempts.has(id)) continue;
    uniqueAttempts.add(id);
    if (attempt.state === "UNKNOWN_OUTCOME") unknownOutcomes++;
    for (const key of passage) {
      if (pendingAyahRun.has(key) && pendingAyahRun.get(key) !== attempt.runId) {
        throw new Error("Overlapping active passage reservations");
      }
      pendingAyahRun.set(key, attempt.runId);
      reserved.add(key);
    }
  }

  const progress = summarizeAuthorizedSurahProgress(approved, delivered);
  const remainingAyahs = approved.size - delivered.size;
  const nextEligible = snapshot.catalog.passages.find((passage) =>
    passage.ayahKeys.every((key) => !delivered.has(key) && !reserved.has(key)),
  );
  const hasUnresolved = pendingAttempts.length > 0;
  const blocked = conflicts > 0 || unknownOutcomes > 0
    || (remainingAyahs > 0 && !nextEligible && !hasUnresolved);

  return {
    cycleNumber: snapshot.cycleNumber,
    catalogVersion: snapshot.catalog.version,
    state: blocked ? "BLOCKED"
      : remainingAyahs === 0 && !hasUnresolved ? "READY_FOR_ROLLOVER"
        : "IN_PROGRESS",
    approvedSurahs: progress.eligibleSurahs,
    startedSurahs: progress.startedSurahs,
    completedSurahs: progress.completedSurahs,
    completedSurahPercentage: progress.percentage,
    approvedAyahs: approved.size,
    confirmedFacebookAyahs: delivered.size,
    confirmedFacebookAyahPercentage: Number(((delivered.size / approved.size) * 100).toFixed(1)),
    remainingAyahs,
    pendingAttempts: uniqueAttempts.size,
    conflictingDeliveries: conflicts,
    nextEligiblePassageId: blocked ? null : (nextEligible?.id ?? null),
  };
}

/** Pure rollover proposal. Only a future atomic service-role transaction may commit it. */
export function proposeNextPublicationCycle(
  snapshot: PublicationCycleSnapshot,
  confirmedReceipts: readonly ConfirmedPublicationReceipt[],
  pendingAttempts: readonly PendingPublicationAttempt[],
): PublicationCycleSnapshot {
  const evaluation = evaluatePublicationCycle(snapshot, confirmedReceipts, pendingAttempts);
  if (evaluation.state !== "READY_FOR_ROLLOVER") {
    throw new Error("Publication cycle cannot roll over without full verified primary delivery");
  }
  if (!Number.isSafeInteger(snapshot.cycleNumber + 1)) {
    throw new Error("Publication cycle number overflow");
  }
  return { cycleNumber: snapshot.cycleNumber + 1, catalog: snapshot.catalog };
}
