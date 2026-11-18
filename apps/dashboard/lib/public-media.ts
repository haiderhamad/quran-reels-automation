import type { RunRecord } from "@/lib/schema";

export const PUBLIC_MEDIA_RUN_ID_SAFE = /^[A-Za-z0-9_.-]{1,160}$/;
export const PUBLIC_MEDIA_DELIVERY_ID_SAFE = /^[A-Za-z0-9_-]{20,160}$/;

/** Pure eligibility gate shared by the video-only route and unit tests. */
export function canServePublicMedia(
  record: RunRecord | null,
  runId: string,
  deliveryId: string,
  b2Prefix: string,
): boolean {
  if (!PUBLIC_MEDIA_RUN_ID_SAFE.test(runId) || !PUBLIC_MEDIA_DELIVERY_ID_SAFE.test(deliveryId)) return false;
  if (
    !record
    || record.run_id !== runId
    || record.status !== "SUCCESS"
    || record.final_safety_passed !== true
    || !record.media_key
    || record.public_media_delivery_id !== deliveryId
  ) return false;
  const cleanPrefix = b2Prefix.replace(/^\/+|\/+$/g, "");
  const mediaPrefix = cleanPrefix ? `${cleanPrefix}/media/` : "media/";
  return record.media_key.startsWith(mediaPrefix) && record.media_key.endsWith(".mp4");
}
