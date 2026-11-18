import "server-only";

import {
  recordSchedulerHeartbeatInSupabase,
  getLatestSchedulerHeartbeatFromSupabase,
  getLatestSchedulerClaimFromSupabase,
  getLatestScheduledRunRecordFromSupabase,
} from "./supabase-state";
import { getB2Client, getB2ConnectionConfig } from "./b2";
import { GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";

export type SchedulerHeartbeatSource = "supabase_cron" | "vercel_cron" | "github_cron" | "dashboard_save";

export interface SchedulerHeartbeatEntry {
  heartbeatId: string;
  source: SchedulerHeartbeatSource;
  occurredAt: string;
  dueSlotCount: number;
  claimCount: number;
  dispatchCount: number;
  result: Record<string, any>;
}

export async function recordSchedulerHeartbeat(entry: {
  source: SchedulerHeartbeatSource;
  occurredAt?: string;
  dueSlotCount: number;
  claimCount: number;
  dispatchCount: number;
  result: Record<string, any>;
}): Promise<string> {
  const timestamp = entry.occurredAt || new Date().toISOString();
  const heartbeatId = `hb_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const record: SchedulerHeartbeatEntry = {
    heartbeatId,
    source: entry.source,
    occurredAt: timestamp,
    dueSlotCount: entry.dueSlotCount,
    claimCount: entry.claimCount,
    dispatchCount: entry.dispatchCount,
    result: entry.result,
  };

  // 1. Try Supabase
  try {
    await recordSchedulerHeartbeatInSupabase(record);
  } catch (err) {
    // Non-fatal, fallback to B2
  }

  // 2. Also write latest heartbeat to B2 as a durable fallback
  try {
    const b2 = getB2ConnectionConfig();
    const client = getB2Client();
    const key = `${b2.prefix}scheduler/latest_heartbeat.json`;
    await client.send(
      new PutObjectCommand({
        Bucket: b2.bucketName,
        Key: key,
        Body: Buffer.from(JSON.stringify(record, null, 2), "utf-8"),
        ContentType: "application/json",
      })
    );
  } catch (err) {
    // Non-fatal
  }

  return heartbeatId;
}

export async function getLatestSchedulerHeartbeat(): Promise<SchedulerHeartbeatEntry | null> {
  // 1. Try Supabase
  try {
    const fromSupabase = await getLatestSchedulerHeartbeatFromSupabase();
    if (fromSupabase) {
      return fromSupabase as SchedulerHeartbeatEntry;
    }
  } catch {
    // Fall back to B2
  }

  // 2. Fallback to B2
  try {
    const b2 = getB2ConnectionConfig();
    const client = getB2Client();
    const key = `${b2.prefix}scheduler/latest_heartbeat.json`;
    const res = await client.send(
      new GetObjectCommand({
        Bucket: b2.bucketName,
        Key: key,
      })
    );
    if (res.Body) {
      const text = await res.Body.transformToString();
      return JSON.parse(text) as SchedulerHeartbeatEntry;
    }
  } catch {
    // Not available
  }

  return null;
}

export async function getLatestSchedulerClaim(): Promise<{
  date: string;
  slotId: string;
  claimedAt: string;
  status: string;
} | null> {
  try {
    return await getLatestSchedulerClaimFromSupabase();
  } catch {
    return null;
  }
}

export async function getLatestScheduledRun(): Promise<{
  runId: string;
  berlinDate: string;
  status: string;
  completedAt?: string;
  createdAt: string;
} | null> {
  try {
    return await getLatestScheduledRunRecordFromSupabase();
  } catch {
    return null;
  }
}
