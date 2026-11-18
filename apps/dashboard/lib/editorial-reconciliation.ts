import snapshot from "./editorial-reconciliation.json";

export interface ReconciliationEntry {
  key: string;
  run_id: string;
  platform: string;
  editorial_verdict: string;
  classification: string;
  platform_media_id: string | null;
  public_url: string | null;
  facebook_page_post_id: string | null;
  buffer_post_id: string | null;
  verse_keys: string[];
  content_identity_sha256: string[];
}

export interface ReconciliationSnapshot {
  policy_version: string;
  editorial_policy_version: string;
  generated_at_utc: string | null;
  entries: ReconciliationEntry[];
}

const reconciliation: ReconciliationSnapshot = snapshot;
export default reconciliation;
