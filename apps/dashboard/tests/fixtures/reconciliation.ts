import { createHash } from "node:crypto";
import type { ReconciliationEntry, ReconciliationSnapshot } from "../../lib/editorial-reconciliation";

function entry(run: string, platform: string, verdict: string, classification: string): ReconciliationEntry {
  return {
    key: run + ":" + platform, run_id: run, platform, editorial_verdict: verdict, classification,
    platform_media_id: null, public_url: null, facebook_page_post_id: null,
    buffer_post_id: null, verse_keys: [], content_identity_sha256: [],
  };
}

const reconciliation: ReconciliationSnapshot = {
  policy_version: "editorial-reconciliation-v1",
  editorial_policy_version: "calm-inviting-v1",
  generated_at_utc: "2000-01-01T00:00:00Z",
  entries: [
    { ...entry("fixture-keep", "facebook", "KEEP", "VALID_HISTORICAL_DELIVERY"),
      verse_keys: ["2:81", "2:82", "2:83"],
      content_identity_sha256: ["سطر تجريبي أول", "سطر تجريبي ثان", "سطر تجريبي ثالث"]
        .map(text => createHash("sha256").update(text).digest("hex")),
      public_url: "https://www.facebook.com/101_202", platform_media_id: "101_202" },
    { ...entry("fixture-retraction", "facebook", "DELETE_CONFIRMED", "RETRACTION_CONFIRMED"),
      public_url: "https://www.facebook.com/reel/303", platform_media_id: "303" },
    { ...entry("fixture-retraction", "instagram", "DELETE_CONFIRMED", "RETRACTION_REQUIRED"),
      platform_media_id: "fixture-ig-removed" },
    entry("fixture-sending", "facebook", "KEEP", "UNCERTAIN_REVIEW"),
    entry("fixture-review", "facebook", "REVIEW", "UNCERTAIN_REVIEW"),
  ],
};
export default reconciliation;
