import "server-only";

import fs from "node:fs";
import path from "node:path";
import {
  PREVIEW_CATALOG_DIRECTORY, PREVIEW_CATALOG_MANIFEST_FILENAME,
  validatePreviewManifest,
} from "@/lib/preview-catalog";
import { getEncryptedMetaCredentials } from "@/lib/b2";
import { decryptMetaCredentials, type EncryptedMetaCredentials } from "@/lib/meta-credentials";

export interface SchedulerHealthStatus {
  status: "ACTIVE" | "DEGRADED" | "PAUSED";
  automation: "ON" | "OFF";
  trigger: string;
  lastHeartbeat: string | null;
  lastHeartbeatSource: string | null;
  lastClaim: string | null;
  lastScheduledRun: string | null;
  details?: string;
}

export interface SystemConnectionStatus {
  b2: boolean;
  github: boolean;
  quranFoundation: boolean;
  pexels: boolean;
  metaFacebook: boolean;
  metaInstagram: boolean;
  publisherBackend: "buffer" | "meta" | "disabled";
  bufferFacebook: boolean;
  bufferInstagram: boolean;
  bufferPublishingEnabled: boolean;
  reciterReuseRuntimeGate: boolean;
  englishTranslation: boolean;
  germanTranslation: boolean;
  audioYasserTiming: boolean;
  scheduler: "configured" | "not_activated" | "active" | "error";
  schedulerHealth: SchedulerHealthStatus;
  previewCatalog: "ready" | "incomplete";
}

export function getPreviewCatalogStatus(publicDirectory = path.join(process.cwd(), "public")): "ready" | "incomplete" {
  try {
    const catalogDirectory = path.join(publicDirectory, "previews", PREVIEW_CATALOG_DIRECTORY);
    const manifest = validatePreviewManifest(JSON.parse(
      fs.readFileSync(path.join(catalogDirectory, PREVIEW_CATALOG_MANIFEST_FILENAME), "utf8"),
    ));
    return Object.keys(manifest.files).every((name) => {
      if (name !== path.basename(name) || !name.endsWith(".webp")) return false;
      const stat = fs.statSync(path.join(catalogDirectory, name));
      return stat.isFile() && stat.size > 0;
    }) ? "ready" : "incomplete";
  } catch {
    return "incomplete";
  }
}

export function isPublishNowAllowed(
  storedConfirmed: boolean,
  runtimeConfirmed: boolean
): boolean {
  return storedConfirmed === true && runtimeConfirmed === true;
}

export function getPublishPlatformBlockReason(
  facebookEnabled: boolean,
  instagramEnabled: boolean,
  facebookConnected: boolean,
  instagramConnected: boolean = false,
): string | null {
  if (!facebookEnabled && !instagramEnabled) {
    return "Enable at least one production platform in Settings.";
  }
  if (facebookEnabled && !facebookConnected) {
    return "Configure and explicitly enable the verified Buffer Facebook Page server-side.";
  }
  if (instagramEnabled && !instagramConnected) {
    return "Configure and explicitly enable the verified Buffer Instagram channel server-side.";
  }
  return null;
}

export function getSystemConnectionStatus(): SystemConnectionStatus {
  const b2 = Boolean(
    process.env.B2_ENDPOINT?.trim() &&
      process.env.B2_REGION?.trim() &&
      process.env.B2_APPLICATION_KEY_ID?.trim() &&
      process.env.B2_APPLICATION_KEY?.trim() &&
      process.env.B2_BUCKET_NAME?.trim()
  );

  const github = Boolean(
    process.env.GITHUB_OWNER?.trim() &&
      process.env.GITHUB_REPO?.trim() &&
      process.env.GITHUB_ACTIONS_TOKEN?.trim()
  );

  const quranFoundation = Boolean(
    process.env.QF_CLIENT_ID?.trim() && process.env.QF_CLIENT_SECRET?.trim()
  );

  const pexels = Boolean(process.env.PEXELS_API_KEY?.trim());

  const metaFacebook = Boolean(
    process.env.META_PAGE_ID?.trim() && process.env.META_PAGE_ACCESS_TOKEN?.trim()
  );

  const metaInstagram = Boolean(
    process.env.META_IG_USER_ID?.trim() && process.env.META_PAGE_ACCESS_TOKEN?.trim()
  );
  const publisherRaw = (process.env.PUBLISHER_BACKEND || "buffer").trim().toLowerCase();
  const publisherBackend = publisherRaw === "meta" ? "meta" : publisherRaw === "buffer" ? "buffer" : "disabled";
  const bufferPublishingEnabled =
    (process.env.BUFFER_PUBLISHING_ENABLED || "").trim().toLowerCase() === "true"
    && (process.env.BUFFER_FACEBOOK_ENABLED || "").trim().toLowerCase() === "true";
  // The personal Buffer API key deliberately exists only in GitHub Actions.
  // The Dashboard reports deployment configuration, not secret presence.
  const bufferFacebook = publisherBackend === "buffer"
    && Boolean(process.env.BUFFER_ORGANIZATION_ID?.trim())
    && Boolean(process.env.BUFFER_FACEBOOK_CHANNEL_ID?.trim())
    && bufferPublishingEnabled;
  const bufferInstagram = publisherBackend === "buffer"
    && Boolean(process.env.BUFFER_ORGANIZATION_ID?.trim())
    && Boolean(process.env.BUFFER_INSTAGRAM_CHANNEL_ID?.trim())
    && (process.env.BUFFER_INSTAGRAM_ENABLED || "").trim().toLowerCase() === "true"
    && (process.env.BUFFER_PUBLISHING_ENABLED || "").trim().toLowerCase() === "true";

  const reciterReuseRaw = process.env.RECITER_REUSE_CONFIRMED?.trim().toLowerCase();
  const reciterReuseRuntimeGate = reciterReuseRaw === "true" || reciterReuseRaw === "1" || reciterReuseRaw === "yes";
  const schedulerActive = process.env.SCHEDULER_ACTIVE?.trim().toLowerCase() === "true";
  const scheduler = process.env.SCHEDULER_STATUS_ERROR?.trim()
    ? "error"
    : schedulerActive
    ? "active"
    : process.env.SCHEDULER_SECRET?.trim()
    ? "configured"
    : "not_activated";

  const schedulerHealth: SchedulerHealthStatus = {
    status: schedulerActive ? "ACTIVE" : "PAUSED",
    automation: schedulerActive ? "ON" : "OFF",
    trigger: "Supabase pg_cron + GitHub backup",
    lastHeartbeat: null,
    lastHeartbeatSource: null,
    lastClaim: null,
    lastScheduledRun: null,
  };

  return {
    b2,
    github,
    quranFoundation,
    pexels,
    metaFacebook,
    metaInstagram,
    publisherBackend,
    bufferFacebook,
    bufferInstagram,
    bufferPublishingEnabled,
    reciterReuseRuntimeGate,
    englishTranslation: true,
    germanTranslation: true,
    audioYasserTiming: true,
    scheduler,
    schedulerHealth,
    previewCatalog: getPreviewCatalogStatus(),
  };
}

/**
 * Current production publishing readiness. Enriches system status with live
 * config and scheduler heartbeat/claim/run state.
 */
export async function getSystemConnectionStatusWithPublishing(): Promise<SystemConnectionStatus> {
  const status = getSystemConnectionStatus();
  try {
    const { getStoredConfig } = await import("@/lib/b2");
    const {
      getLatestSchedulerHeartbeat,
      getLatestSchedulerClaim,
      getLatestScheduledRun,
    } = await import("@/lib/scheduler-heartbeat");

    const [config, latestHeartbeat, latestClaim, latestRun] = await Promise.all([
      Promise.resolve().then(() => getStoredConfig?.()).catch(() => null),
      Promise.resolve().then(() => getLatestSchedulerHeartbeat?.()).catch(() => null),
      Promise.resolve().then(() => getLatestSchedulerClaim?.()).catch(() => null),
      Promise.resolve().then(() => getLatestScheduledRun?.()).catch(() => null),
    ]);

    const automationOn = Boolean(config?.automation_enabled);
    let schedulerStatus: "ACTIVE" | "DEGRADED" | "PAUSED" = "PAUSED";
    let details: string | undefined = undefined;

    if (automationOn) {
      if (latestHeartbeat?.occurredAt) {
        const ageMinutes = (Date.now() - new Date(latestHeartbeat.occurredAt).getTime()) / (60 * 1000);
        if (ageMinutes <= 75) {
          schedulerStatus = "ACTIVE";
        } else {
          schedulerStatus = "DEGRADED";
          details = "DEGRADED — heartbeat overdue";
        }
      } else {
        schedulerStatus = "DEGRADED";
        details = "DEGRADED — heartbeat overdue";
      }
    }

    status.schedulerHealth = {
      status: schedulerStatus,
      automation: automationOn ? "ON" : "OFF",
      trigger: "Supabase pg_cron + GitHub backup",
      lastHeartbeat: latestHeartbeat?.occurredAt || null,
      lastHeartbeatSource: latestHeartbeat?.source || null,
      lastClaim: latestClaim ? `${latestClaim.date} (${latestClaim.slotId})` : null,
      lastScheduledRun: latestRun ? `${latestRun.berlinDate} (${latestRun.status})` : null,
      details,
    };

    if (automationOn) {
      status.scheduler = schedulerStatus === "ACTIVE" ? "active" : "error";
    } else {
      status.scheduler = "not_activated";
    }
  } catch (err) {
    console.error("[SystemStatus] Failed to enrich scheduler health:", err);
  }
  return status;
}

export async function getSystemConnectionStatusWithMeta(): Promise<SystemConnectionStatus> {
  const status = await getSystemConnectionStatusWithPublishing();
  try {
    const encryptedMeta = await getEncryptedMetaCredentials();
    if (!encryptedMeta) return status;
    const connectedMeta = decryptMetaCredentials(JSON.parse(encryptedMeta) as EncryptedMetaCredentials);
    status.metaFacebook = true;
    status.metaInstagram = Boolean(connectedMeta.instagram?.id);
  } catch {
    status.metaFacebook = false;
    status.metaInstagram = false;
  }
  return status;
}
