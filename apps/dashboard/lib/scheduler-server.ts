import "server-only";

import {
  createSchedulerClaim,
  getScheduledRunRecord,
  getStoredConfig,
  releaseSchedulerClaim,
  saveSchedulerClaim,
  type SchedulerClaim,
} from "./b2";
import { triggerWorkflowDispatch } from "./github";
import { getBerlinClock, getDuePublishingSlots, getProductionPublishingSchedule } from "./schedule";
import { recordSchedulerHeartbeat, type SchedulerHeartbeatSource } from "./scheduler-heartbeat";
import type { StoredConfig } from "./schema";

export interface SchedulerTickResult {
  automationEnabled: boolean;
  berlinDate: string;
  berlinTime: string;
  dispatched: string[];
  skipped: string[];
  failed: string[];
  // Stable, non-sensitive failure codes for persisted scheduler diagnostics.
  failureCodes: Record<string, string>;
}

export async function runSchedulerTick(
  now = new Date(),
  configOverride?: StoredConfig,
  source: SchedulerHeartbeatSource = "vercel_cron"
): Promise<SchedulerTickResult> {
  const config = configOverride ?? (await getStoredConfig());
  const clock = getBerlinClock(now);
  const result: SchedulerTickResult = {
    automationEnabled: config.automation_enabled,
    berlinDate: clock.date,
    berlinTime: clock.time,
    dispatched: [],
    skipped: [],
    failed: [],
    failureCodes: {},
  };

  const dueSlots = config.automation_enabled
    ? getDuePublishingSlots(getProductionPublishingSchedule(config.publishing_schedule), now)
    : [];

  if (!config.automation_enabled) {
    try {
      await recordSchedulerHeartbeat({
        source,
        occurredAt: now.toISOString(),
        dueSlotCount: 0,
        claimCount: 0,
        dispatchCount: 0,
        result,
      });
    } catch {
      // Non-fatal observability logging
    }
    return result;
  }

  for (const slot of dueSlots) {
    if (await getScheduledRunRecord(clock.date, slot.id)) {
      result.skipped.push(slot.id);
      continue;
    }
    const runId = `scheduled_${clock.date.replaceAll("-", "")}_${slot.id}`;
    const timestamp = now.toISOString();
    const claim: SchedulerClaim = {
      date: clock.date,
      slot_id: slot.id,
      run_id: runId,
      status: "CLAIMED",
      claimed_at: timestamp,
      updated_at: timestamp,
    };
    const acquired = await createSchedulerClaim(claim);
    if (!acquired) {
      result.skipped.push(slot.id);
      continue;
    }

    const dispatch = await triggerWorkflowDispatch({
      runKind: "scheduled",
      slotId: slot.id,
      runId,
      publishFacebook: config.facebook_enabled,
      publishInstagram: config.instagram_enabled,
    });
    if (dispatch.success) {
      await saveSchedulerClaim({ ...claim, status: "DISPATCHED", updated_at: new Date().toISOString() });
      result.dispatched.push(slot.id);
    } else {
      // Do not persist GitHub's raw error text: it can contain sensitive details.
      const httpStatus = /^GitHub API error \((\d{3})\):/.exec(dispatch.message)?.[1];
      const failureCode = dispatch.ambiguous
        ? "GITHUB_NETWORK_UNCERTAIN"
        : dispatch.message.startsWith("GitHub Actions dispatch is not configured.")
          ? "GITHUB_CONFIG_MISSING"
          : httpStatus
            ? `GITHUB_HTTP_${httpStatus}`
            : "GITHUB_DISPATCH_REJECTED";
      await saveSchedulerClaim({
        ...claim,
        status: "FAILED",
        updated_at: new Date().toISOString(),
        failure_reason: failureCode,
      });
      result.failed.push(slot.id);
      result.failureCodes[slot.id] = failureCode;
      if (!dispatch.ambiguous) {
        await releaseSchedulerClaim(clock.date, slot.id);
      }
    }
  }

  try {
    await recordSchedulerHeartbeat({
      source,
      occurredAt: now.toISOString(),
      dueSlotCount: dueSlots.length,
      claimCount: result.dispatched.length + result.failed.length,
      dispatchCount: result.dispatched.length,
      result,
    });
  } catch {
    // Non-fatal observability logging
  }

  return result;
}
