import "server-only";

import {
  StoredConfig,
  RunRecord,
  CoverageSummary,
} from "./schema";
import {
  StoredConfigLoadState,
  getStoredConfigState as getB2StoredConfigState,
  getStoredConfig as getB2StoredConfig,
  saveStoredConfig as saveB2StoredConfig,
  getCoverageSummary as getB2CoverageSummary,
  getEncryptedMetaCredentials as getB2EncryptedMetaCredentials,
  saveEncryptedMetaCredentials as saveB2EncryptedMetaCredentials,
  deleteEncryptedMetaCredentials as deleteB2EncryptedMetaCredentials,
  getRunRecord as getB2RunRecord,
  listRecentRunRecords as listB2RecentRunRecords,
  claimSchedulerSlot as claimB2SchedulerSlot,
  getSchedulerClaim as getB2SchedulerClaim,
  createPreviewPresignedUrl as getB2PresignedMediaUrl,
  SchedulerClaim,
} from "./b2";
import {
  getStoredConfigStateFromSupabase,
  getStoredConfigFromSupabase,
  saveStoredConfigToSupabase,
  getCoverageSummaryFromSupabase,
  getEncryptedMetaCredentialsFromSupabase,
  saveEncryptedMetaCredentialsToSupabase,
  deleteEncryptedMetaCredentialsFromSupabase,
  getRunRecordFromSupabase,
  listRecentRunRecordsFromSupabase,
  claimSchedulerSlotInSupabase,
  getSchedulerClaimFromSupabase,
} from "./supabase-state";

export type StateAuthority = "b2" | "supabase";

export function getStateAuthority(): StateAuthority {
  const raw = process.env.STATE_AUTHORITY;
  if (!raw || !raw.trim()) {
    return "b2";
  }
  const cleaned = raw.trim().toLowerCase();
  if (cleaned !== "b2" && cleaned !== "supabase") {
    throw new Error(
      `Invalid STATE_AUTHORITY '${raw}'. Expected 'b2' or 'supabase'.`
    );
  }
  return cleaned as StateAuthority;
}

export async function getConfigLoadState(): Promise<StoredConfigLoadState> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return getStoredConfigStateFromSupabase();
  }
  return getB2StoredConfigState();
}

export async function getConfig(): Promise<StoredConfig> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return getStoredConfigFromSupabase();
  }
  return getB2StoredConfig();
}

export async function saveConfig(
  config: Partial<StoredConfig>
): Promise<StoredConfig> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return saveStoredConfigToSupabase(config);
  }
  return saveB2StoredConfig(config);
}

export async function getCoverageSummary(): Promise<{
  summary: CoverageSummary;
  error?: string;
}> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return getCoverageSummaryFromSupabase();
  }
  return getB2CoverageSummary();
}

export async function getEncryptedMetaCredentials(): Promise<string | null> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return getEncryptedMetaCredentialsFromSupabase();
  }
  return getB2EncryptedMetaCredentials();
}

export async function saveEncryptedMetaCredentials(
  envelope: string
): Promise<void> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return saveEncryptedMetaCredentialsToSupabase(envelope);
  }
  return saveB2EncryptedMetaCredentials(envelope);
}

export async function deleteEncryptedMetaCredentials(): Promise<void> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return deleteEncryptedMetaCredentialsFromSupabase();
  }
  return deleteB2EncryptedMetaCredentials();
}

export async function getRunRecord(
  date: string,
  slot: string,
  runId?: string,
  runKind?: "test" | "manual_production" | "scheduled"
): Promise<RunRecord | null> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return getRunRecordFromSupabase(date, slot, runId, runKind);
  }
  return getB2RunRecord(date, slot, runId, runKind);
}

export async function listRecentRunRecords(limit = 30): Promise<RunRecord[]> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return listRecentRunRecordsFromSupabase(limit);
  }
  return listB2RecentRunRecords(limit);
}

export async function claimSchedulerSlot(
  date: string,
  slotId: string,
  claimedAt?: string,
  workerRunId?: string
): Promise<boolean> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
    return claimSchedulerSlotInSupabase(date, slotId, claimedAt, workerRunId);
  }
  return claimB2SchedulerSlot(date, slotId, claimedAt, workerRunId);
}

export async function getSchedulerClaim(
  date: string,
  slotId: string
): Promise<SchedulerClaim | null> {
  const authority = getStateAuthority();
  if (authority === "supabase") {
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
  return getB2SchedulerClaim(date, slotId);
}

/**
 * Media is ALWAYS fetched from Backblaze B2 (final Reel MP4 store).
 */
export async function getPresignedMediaUrl(
  mediaKey: string,
  expiresInSeconds?: number
): Promise<string> {
  return getB2PresignedMediaUrl(mediaKey, expiresInSeconds);
}
