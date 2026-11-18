import "server-only";

import {
  S3Client,
  GetObjectCommand,
  PutObjectCommand,
  ListObjectsV2Command,
  HeadObjectCommand,
  DeleteObjectCommand,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import {
  StoredConfig,
  StoredConfigSchema,
  RunRecord,
  RunRecordSchema,
  CoverageStateSchema,
  CoverageSummary,
  TOTAL_CANONICAL_AYAHS,
} from "./schema";
import {
  getStoredConfigStateFromSupabase,
  saveStoredConfigToSupabase,
  getCoverageSummaryFromSupabase,
  getEncryptedMetaCredentialsFromSupabase,
  saveEncryptedMetaCredentialsToSupabase,
  deleteEncryptedMetaCredentialsFromSupabase,
  getRunRecordFromSupabase,
  getRunRecordByIdFromSupabase,
  listRecentRunRecordsFromSupabase,
  claimSchedulerSlotInSupabase,
  updateSchedulerClaimInSupabase,
  getSchedulerClaimFromSupabase,
  releaseSchedulerClaimInSupabase,
} from "./supabase-state";

export interface B2ConnectionConfig {
  endpoint: string;
  region: string;
  applicationKeyId: string;
  applicationKey: string;
  bucketName: string;
  prefix: string;
}

export function getB2ConnectionConfig(): B2ConnectionConfig {
  const endpoint = process.env.B2_ENDPOINT?.trim() || "";
  const region = process.env.B2_REGION?.trim() || "";
  const applicationKeyId = process.env.B2_APPLICATION_KEY_ID?.trim() || "";
  const applicationKey = process.env.B2_APPLICATION_KEY?.trim() || "";
  const bucketName = process.env.B2_BUCKET_NAME?.trim() || "";
  let prefix = process.env.B2_PREFIX?.trim() || "quran-reels/";

  if (!prefix.endsWith("/")) {
    prefix += "/";
  }

  return {
    endpoint,
    region,
    applicationKeyId,
    applicationKey,
    bucketName,
    prefix,
  };
}

let cachedS3Client: S3Client | null = null;

type B2CommandClient = {
  send(command: unknown): Promise<any>;
};

export type StoredConfigLoadState = {
  config: StoredConfig;
  authoritative: boolean;
  writable: boolean;
  source: "b2" | "missing" | "fallback";
  errorCode: string | null;
};

export function getB2Client(): S3Client {
  if (cachedS3Client) {
    return cachedS3Client;
  }
  const config = getB2ConnectionConfig();
  if (
    !config.endpoint ||
    !config.region ||
    !config.applicationKeyId ||
    !config.applicationKey ||
    !config.bucketName
  ) {
    throw new Error(
      "Backblaze B2 is not fully configured. Missing one or more required B2 environment variables (B2_ENDPOINT, B2_REGION, B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME)."
    );
  }

  cachedS3Client = new S3Client({
    endpoint: config.endpoint,
    region: config.region,
    credentials: {
      accessKeyId: config.applicationKeyId,
      secretAccessKey: config.applicationKey,
    },
  });

  return cachedS3Client;
}

export function buildConfigKey(prefix: string): string {
  const cleanPrefix = prefix.replace(/^\/+|\/+$/g, "");
  return cleanPrefix ? `${cleanPrefix}/config/current.json` : "config/current.json";
}

export function buildCoverageKey(prefix: string): string {
  const cleanPrefix = prefix.replace(/^\/+|\/+$/g, "");
  return cleanPrefix ? `${cleanPrefix}/coverage/current.json` : "coverage/current.json";
}

export function buildMetaCredentialsKey(prefix: string): string {
  const cleanPrefix = prefix.replace(/^\/+|\/+$/g, "");
  return cleanPrefix
    ? `${cleanPrefix}/config/secrets/meta-credentials.enc`
    : "config/secrets/meta-credentials.enc";
}

export async function getEncryptedMetaCredentials(): Promise<string | null> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return getEncryptedMetaCredentialsFromSupabase();
  }
  const b2 = getB2ConnectionConfig();
  try {
    const response = await getB2Client().send(new GetObjectCommand({
      Bucket: b2.bucketName,
      Key: buildMetaCredentialsKey(b2.prefix),
    }));
    return response.Body ? await response.Body.transformToString("utf-8") : null;
  } catch (err: any) {
    if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404 || err.Code === "NoSuchKey") {
      return null;
    }
    throw err;
  }
}

export async function saveEncryptedMetaCredentials(envelope: string): Promise<void> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return saveEncryptedMetaCredentialsToSupabase(envelope);
  }
  const b2 = getB2ConnectionConfig();
  await getB2Client().send(new PutObjectCommand({
    Bucket: b2.bucketName,
    Key: buildMetaCredentialsKey(b2.prefix),
    Body: Buffer.from(envelope, "utf-8"),
    ContentType: "application/octet-stream",
    CacheControl: "no-store",
  }));
}

export async function deleteEncryptedMetaCredentials(): Promise<void> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return deleteEncryptedMetaCredentialsFromSupabase();
  }
  const b2 = getB2ConnectionConfig();
  await getB2Client().send(new DeleteObjectCommand({
    Bucket: b2.bucketName,
    Key: buildMetaCredentialsKey(b2.prefix),
  }));
}

export function buildSchedulerClaimKey(prefix: string, date: string, slotId: string): string {
  if (!DATE_SAFE.test(date)) throw new Error("Invalid scheduler claim date.");
  if (!SLOT_SAFE.test(slotId)) throw new Error("Invalid scheduler slot ID.");
  const cleanPrefix = prefix.replace(/^\/+|\/+$/g, "");
  const base = cleanPrefix ? `${cleanPrefix}/scheduler/claims` : "scheduler/claims";
  return `${base}/${date}/${slotId}.json`;
}

export function buildScheduledRunKey(prefix: string, date: string, slotId: string): string {
  if (!DATE_SAFE.test(date)) throw new Error("Invalid scheduled run date.");
  if (!SLOT_SAFE.test(slotId)) throw new Error("Invalid schedule slot ID.");
  const cleanPrefix = prefix.replace(/^\/+|\/+$/g, "");
  const base = cleanPrefix ? `${cleanPrefix}/runs` : "runs";
  return `${base}/${date}/scheduled/${slotId}.json`;
}

export function buildScheduledRunLookupKeys(prefix: string, date: string, slotId: string): string[] {
  const keys = [buildScheduledRunKey(prefix, date, slotId)];
  if (slotId === "morning" || slotId === "evening") {
    keys.push(buildRunKey(prefix, date, slotId));
  }
  return keys;
}

const DATE_SAFE = /^\d{4}-\d{2}-\d{2}$/;
const SLOT_SAFE = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/;

export function buildRunKey(
  prefix: string,
  date: string,
  slot: string,
  runId?: string,
  runKind?: "test" | "manual_production" | "scheduled"
): string {
  const cleanPrefix = prefix.replace(/^\/+|\/+$/g, "");
  const base = cleanPrefix ? `${cleanPrefix}/runs` : "runs";

  if (runKind === "manual_production" || slot === "manual-production") {
    if (!runId) throw new Error("runId is required for manual production run records.");
    return `${base}/manual-production/${runId}.json`;
  }
  if (runKind === "test" || slot === "test") {
    if (!runId) {
      throw new Error("runId is required for test run records.");
    }
    return `${base}/test/${runId}.json`;
  }
  if (runKind === "scheduled") {
    if (!SLOT_SAFE.test(slot)) throw new Error("Invalid schedule slot ID.");
    return `${base}/${date}/scheduled/${slot}.json`;
  }
  // Backward-compatible lookup for pre-D-014 morning/evening records.
  return `${base}/${date}/${slot}.json`;
}

/**
 * Fetch and parse current Quran Coverage State from B2 (coverage/current.json).
 * Strictly read-only: does NOT mutate or auto-create B2 objects.
 * Returns default CoverageSummary (Cycle 1, 0/6236) if missing, or error indicator if malformed.
 */
export async function getCoverageSummary(): Promise<{ summary: CoverageSummary; error?: string }> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return getCoverageSummaryFromSupabase();
  }
  const b2 = getB2ConnectionConfig();
  const defaultSummary: CoverageSummary = {
    cycleNumber: 1,
    totalAyahs: TOTAL_CANONICAL_AYAHS,
    coveredCount: 0,
    remainingCount: TOTAL_CANONICAL_AYAHS,
    progressPercentage: 0.0,
    isComplete: false,
    startedAt: null,
    lastUpdatedAt: null,
  };

  if (!b2.endpoint || !b2.region || !b2.applicationKeyId || !b2.applicationKey || !b2.bucketName) {
    return { summary: defaultSummary };
  }

  try {
    const client = getB2Client();
    const key = buildCoverageKey(b2.prefix);
    const cmd = new GetObjectCommand({
      Bucket: b2.bucketName,
      Key: key,
    });
    const resp = await client.send(cmd);
    if (!resp.Body) {
      return { summary: defaultSummary };
    }
    const text = await resp.Body.transformToString("utf-8");
    const raw = JSON.parse(text);
    const parsed = CoverageStateSchema.parse(raw);

    const remaining = Math.max(0, parsed.total_ayahs - parsed.covered_count);
    const progress = parsed.total_ayahs > 0
      ? Number(((parsed.covered_count / parsed.total_ayahs) * 100).toFixed(2))
      : 0;

    return {
      summary: {
        cycleNumber: parsed.cycle_number,
        totalAyahs: parsed.total_ayahs,
        coveredCount: parsed.covered_count,
        remainingCount: remaining,
        progressPercentage: progress,
        isComplete: parsed.covered_count >= parsed.total_ayahs,
        startedAt: parsed.started_at || null,
        lastUpdatedAt: parsed.last_updated_at || null,
      },
    };
  } catch (err: any) {
    if (
      err.name === "NoSuchKey" ||
      err.$metadata?.httpStatusCode === 404 ||
      err.Code === "NoSuchKey"
    ) {
      return { summary: defaultSummary };
    }
    console.error("[B2] Failed to fetch or parse coverage state:", err);
    return {
      summary: defaultSummary,
      error: `Could not verify coverage state from Backblaze B2: ${err.message || "Malformed record"}`,
    };
  }
}

/**
 * Fetch and parse runtime configuration from B2 (config/current.json).
 * Returns default StoredConfig if not found.
 */
export async function getStoredConfigState(
  clientOverride?: B2CommandClient,
): Promise<StoredConfigLoadState> {
  if (!clientOverride && (process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return getStoredConfigStateFromSupabase();
  }
  const b2 = getB2ConnectionConfig();
  if (!b2.endpoint || !b2.region || !b2.applicationKeyId || !b2.applicationKey || !b2.bucketName) {
    return {
      config: StoredConfigSchema.parse({}),
      authoritative: false,
      writable: false,
      source: "fallback",
      errorCode: "B2_CONFIG_NOT_CONFIGURED",
    };
  }

  try {
    const client = clientOverride || getB2Client();
    const key = buildConfigKey(b2.prefix);
    const cmd = new GetObjectCommand({
      Bucket: b2.bucketName,
      Key: key,
    });
    const resp = await client.send(cmd);
    if (!resp.Body) {
      throw new Error("B2_CONFIG_EMPTY_BODY");
    }
    const text = await resp.Body.transformToString("utf-8");
    const raw = JSON.parse(text);
    return {
      config: StoredConfigSchema.parse(raw),
      authoritative: true,
      writable: true,
      source: "b2",
      errorCode: null,
    };
  } catch (err: any) {
    if (
      err.name === "NoSuchKey" ||
      err.$metadata?.httpStatusCode === 404 ||
      err.Code === "NoSuchKey"
    ) {
      return {
        config: StoredConfigSchema.parse({}),
        authoritative: true,
        writable: true,
        source: "missing",
        errorCode: null,
      };
    }
    console.error("[B2] Error loading stored configuration; fallback is read-only:", err.message || err);
    return {
      config: StoredConfigSchema.parse({}),
      authoritative: false,
      writable: false,
      source: "fallback",
      errorCode: "B2_CONFIG_UNAVAILABLE_READ_ONLY",
    };
  }
}

export async function getStoredConfig(): Promise<StoredConfig> {
  return (await getStoredConfigState()).config;
}

export function assertStoredConfigWritable(state: StoredConfigLoadState): void {
  if (!state.authoritative || !state.writable) {
    throw new Error("B2_CONFIG_UNAVAILABLE_READ_ONLY");
  }
}

/**
 * Validate and save runtime configuration to B2 (config/current.json).
 */
export async function saveStoredConfig(
  config: Partial<StoredConfig>,
  clientOverride?: B2CommandClient,
): Promise<StoredConfig> {
  if (!clientOverride && (process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return saveStoredConfigToSupabase(config);
  }
  const b2 = getB2ConnectionConfig();
  const client = clientOverride || getB2Client();
  const loadState = await getStoredConfigState(client);
  assertStoredConfigWritable(loadState);
  const key = buildConfigKey(b2.prefix);

  // Automatically update updated_at timestamp
  const configToValidate = {
    ...config,
    updated_at: new Date().toISOString(),
  };

  const validated = StoredConfigSchema.parse(configToValidate);
  const jsonString = JSON.stringify(validated, null, 2);

  const cmd = new PutObjectCommand({
    Bucket: b2.bucketName,
    Key: key,
    Body: Buffer.from(jsonString, "utf-8"),
    ContentType: "application/json",
  });

  await client.send(cmd);
  return validated;
}

/**
 * Fetch a single run record by S3 key.
 */
export async function getRunRecordByKey(key: string): Promise<RunRecord | null> {
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();

  try {
    const cmd = new GetObjectCommand({
      Bucket: b2.bucketName,
      Key: key,
    });
    const resp = await client.send(cmd);
    if (!resp.Body) return null;
    const text = await resp.Body.transformToString("utf-8");
    const raw = JSON.parse(text);
    return RunRecordSchema.parse(raw);
  } catch (err: any) {
    if (
      err.name === "NoSuchKey" ||
      err.$metadata?.httpStatusCode === 404 ||
      err.Code === "NoSuchKey"
    ) {
      return null;
    }
    console.error(`[B2] Error loading run record at ${key}:`, err);
    return null;
  }
}

/**
 * Fetch run record for a specific date and slot.
 */
export async function getRunRecord(
  date: string,
  slot: string,
  runId?: string,
  runKind?: "test" | "manual_production" | "scheduled"
): Promise<RunRecord | null> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return getRunRecordFromSupabase(date, slot, runId, runKind);
  }
  const b2 = getB2ConnectionConfig();
  if (runId) {
    const inferredKind = runKind || (slot === "test" ? "test" : slot === "manual-production" ? "manual_production" : "scheduled");
    const exact = await getRunRecordByKey(buildRunKey(b2.prefix, date, slot, runId, inferredKind));
    return exact || await findRunRecordById(runId);
  }
  return await getRunRecordByKey(buildRunKey(b2.prefix, date, slot, runId, runKind));
}

/** Server-only exact lookup used by the isolated public-video delivery route. */
export async function getRunRecordById(runId: string): Promise<RunRecord | null> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return getRunRecordByIdFromSupabase(runId);
  }
  return findRunRecordById(runId);
}

/**
 * List recent run records from B2 in reverse chronological order.
 */
export async function listRecentRunRecords(limit = 30): Promise<RunRecord[]> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return listRecentRunRecordsFromSupabase(limit);
  }
  const b2 = getB2ConnectionConfig();
  const runsPrefix = b2.prefix.replace(/^\/+|\/+$/g, "")
    ? `${b2.prefix.replace(/^\/+|\/+$/g, "")}/runs/`
    : "runs/";

  try {
    const client = getB2Client();
    const contents: Array<{ Key?: string }> = [];
    let continuationToken: string | undefined;
    do {
      const resp = await client.send(new ListObjectsV2Command({
        Bucket: b2.bucketName,
        Prefix: runsPrefix,
        ContinuationToken: continuationToken,
      }));
      contents.push(...(resp.Contents || []));
      continuationToken = resp.IsTruncated ? resp.NextContinuationToken : undefined;
    } while (continuationToken);

    const keys = contents
      .map((item) => item.Key || "")
      .filter((k) => k.endsWith(".json") && !k.endsWith("/"));

    const records: RunRecord[] = [];
    for (const k of keys) {
      const rec = await getRunRecordByKey(k);
      if (rec) {
        records.push(rec);
      }
    }

    sortRunRecordsByStartedAt(records);
    return records.slice(0, limit);
  } catch (err: any) {
    console.error("[B2] Error listing run records:", err);
    return [];
  }
}

export function sortRunRecordsByStartedAt<T extends Pick<RunRecord, "started_at">>(records: T[]): T[] {
  return records.sort((a, b) => b.started_at.localeCompare(a.started_at));
}

export async function findRunRecordById(runId: string): Promise<RunRecord | null> {
  if (!runId || runId.length > 160) return null;
  const records = await listRecentRunRecords(100);
  return records.find((record) => record.run_id === runId) || null;
}

export async function getScheduledRunRecord(date: string, slotId: string): Promise<RunRecord | null> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return getRunRecordFromSupabase(date, slotId);
  }
  const b2 = getB2ConnectionConfig();
  for (const key of buildScheduledRunLookupKeys(b2.prefix, date, slotId)) {
    const record = await getRunRecordByKey(key);
    if (record) return record;
  }
  return null;
}

export interface SchedulerClaim {
  date: string;
  slot_id: string;
  run_id: string;
  status: "CLAIMED" | "DISPATCHED" | "FAILED";
  claimed_at: string;
  updated_at: string;
  failure_reason?: string;
}

export async function getSchedulerClaim(date: string, slotId: string): Promise<SchedulerClaim | null> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    const claim = await getSchedulerClaimFromSupabase(date, slotId);
    if (!claim) return null;
    return {
      date: claim.date,
      slot_id: claim.slot_id,
      run_id: claim.worker_run_id,
      status: claim.status,
      claimed_at: claim.claimed_at,
      updated_at: claim.updated_at,
      failure_reason: claim.failure_reason,
    };
  }
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();
  const key = buildSchedulerClaimKey(b2.prefix, date, slotId);
  try {
    const response = await client.send(new GetObjectCommand({ Bucket: b2.bucketName, Key: key }));
    if (!response.Body) return null;
    return JSON.parse(await response.Body.transformToString("utf-8")) as SchedulerClaim;
  } catch (err: any) {
    if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404 || err.Code === "NoSuchKey") {
      return null;
    }
    throw err;
  }
}

export async function claimSchedulerSlot(date: string, slotId: string, claimedAt?: string, workerRunId?: string): Promise<boolean> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return claimSchedulerSlotInSupabase(date, slotId, claimedAt, workerRunId);
  }
  const claim: SchedulerClaim = {
    date,
    slot_id: slotId,
    run_id: workerRunId || `claim_${date}_${slotId}`,
    status: "CLAIMED",
    claimed_at: claimedAt || new Date().toISOString(),
    updated_at: claimedAt || new Date().toISOString(),
  };
  return createSchedulerClaim(claim);
}

export async function createSchedulerClaim(claim: SchedulerClaim): Promise<boolean> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return claimSchedulerSlotInSupabase(claim.date, claim.slot_id, claim.claimed_at, claim.run_id);
  }
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();
  const key = buildSchedulerClaimKey(b2.prefix, claim.date, claim.slot_id);
  try {
    await client.send(new PutObjectCommand({
      Bucket: b2.bucketName,
      Key: key,
      Body: Buffer.from(JSON.stringify(claim, null, 2), "utf-8"),
      ContentType: "application/json",
      IfNoneMatch: "*",
    }));
    return true;
  } catch (err: any) {
    if (err.$metadata?.httpStatusCode === 409 || err.$metadata?.httpStatusCode === 412 || err.name === "PreconditionFailed") {
      return false;
    }
    throw err;
  }
}

export async function saveSchedulerClaim(claim: SchedulerClaim): Promise<void> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    await updateSchedulerClaimInSupabase(claim);
    return;
  }
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();
  const key = buildSchedulerClaimKey(b2.prefix, claim.date, claim.slot_id);
  await client.send(new PutObjectCommand({
    Bucket: b2.bucketName,
    Key: key,
    Body: Buffer.from(JSON.stringify(claim, null, 2), "utf-8"),
    ContentType: "application/json",
  }));
}

export async function releaseSchedulerClaim(date: string, slotId: string): Promise<void> {
  if ((process.env.STATE_AUTHORITY || "").trim().toLowerCase() === "supabase") {
    return releaseSchedulerClaimInSupabase(date, slotId);
  }
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();
  await client.send(new DeleteObjectCommand({
    Bucket: b2.bucketName,
    Key: buildSchedulerClaimKey(b2.prefix, date, slotId),
  }));
}

/**
 * Check if a media file still exists in B2 (7-day lifecycle retention).
 */
export async function checkMediaExists(mediaKey: string): Promise<boolean> {
  if (!mediaKey) return false;
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();

  try {
    const cmd = new HeadObjectCommand({
      Bucket: b2.bucketName,
      Key: mediaKey,
    });
    await client.send(cmd);
    return true;
  } catch {
    return false;
  }
}

/**
 * Generate a short-lived presigned GET URL for dashboard Reel preview.
 * Default expiration is 10 minutes (600s).
 */
export async function createPreviewPresignedUrl(
  mediaKey: string,
  expiresSeconds = 600
): Promise<string> {
  if (!mediaKey) {
    throw new Error("mediaKey is required to generate preview URL.");
  }
  const b2 = getB2ConnectionConfig();
  const client = getB2Client();

  const cmd = new GetObjectCommand({
    Bucket: b2.bucketName,
    Key: mediaKey,
  });

  return await getSignedUrl(client, cmd, { expiresIn: expiresSeconds });
}
