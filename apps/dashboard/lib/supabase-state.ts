import "server-only";

import {
  StoredConfig,
  StoredConfigSchema,
  RunRecord,
  RunRecordSchema,
  CoverageStateSchema,
  CoverageSummary,
  TOTAL_CANONICAL_AYAHS,
} from "./schema";
import type { StoredConfigLoadState } from "./b2";
import { computeEditorialCoverage } from "./editorial-coverage";
import type { EditorialCoverageSummary } from "./editorial-coverage";
import { isApprovedDeliveredReel, editorialRangeKey, type AuthorizedEditorialRanges } from "./approved-reels";
import { BufferPublicationClaimSchema, type BufferPublicationClaim } from "./publishing-progress";
import { isCanonicalAyahKey } from "./canonical-quran";
import { getCalmReelsShortlistKeys } from "./calm-reels-shortlist";
import { calculateCalmReelsCycleProgress, type CalmReelsCycleProgress } from "./calm-reels-fresh-cycle";
import { auditEditorialEvidencePage } from "./editorial-evidence-audit";
import { validateSurahProgress, type EditorialSurahProgress, type EditorialFilter } from "./editorial-review";

export async function getEditorialSurahProgressFromSupabase(): Promise<{ rows: EditorialSurahProgress[]; error: string | null }> {
  try {
    const rows = validateSurahProgress(await postgrestRequest("editorial_screening_surah_summary", "GET", {
      params: { select: "*", order: "surah_number.asc", limit: "114" },
    }));
    return { rows, error: null };
  } catch {
    return { rows: [], error: "Surah progress could not be verified from Supabase." };
  }
}

export interface EditorialAyahDetail {
  ayah_number: number;
  screening_status: string;
  semantic_assessment: string | null;
  productionApproved: boolean;
  source: Record<string, any> | null;
  assessments: Record<string, any>[];
  evidenceIssues?: readonly string[];
}

/** Admin-only server read: at most 25 ayahs, 25 source records, and 50 pass records. */
export async function getEditorialAyahsFromSupabase(surah: number, filter: EditorialFilter, page: number,
  approvedKeys: ReadonlySet<string>): Promise<{ rows: EditorialAyahDetail[]; hasNext: boolean; error: string | null }> {
  try {
    if (!isCanonicalAyahKey(`${surah}:1`) || !Number.isSafeInteger(page) || page < 1 || page > 12) throw new Error("Invalid editorial page.");
    const params: Record<string, string> = { policy_version: "eq.calm-inviting-v1", surah_number: `eq.${surah}`,
      select: "*", order: "ayah_number.asc", limit: "26", offset: String((page - 1) * 25) };
    if (filter === "NOT_SCREENED") params.screening_status = "eq.NOT_SCREENED";
    else if (filter !== "ALL") params.semantic_assessment = `eq.${filter}`;
    const reviews = await postgrestRequest("editorial_ayah_reviews", "GET", { params });
    if (!Array.isArray(reviews) || reviews.some((row) => row.surah_number !== surah
      || !isCanonicalAyahKey(`${surah}:${row.ayah_number}`))) throw new Error("Invalid editorial identities.");
    const selected = reviews.slice(0, 25);
    if (!selected.length) return { rows: [], hasNext: false, error: null };
    const keys = selected.map((row) => row.ayah_number).join(",");
    const runs = [...new Set(selected.map((row) => row.screening_run_id).filter(Boolean))];
    const sourceHashes = [...new Set(selected.map((row) => row.source_hash).filter(Boolean))];
    const translationHashes = [...new Set(selected.map((row) => row.translation_hash).filter(Boolean))];
    if ([...sourceHashes, ...translationHashes].some((hash) => typeof hash !== "string" || !/^[0-9a-f]{64}$/u.test(hash))) throw new Error("Malformed source identity.");
    if (runs.some((run) => typeof run !== "string" || !/^[\w:.-]+$/u.test(run))) throw new Error("Malformed screening run identity.");
    const [sources, assessments] = await Promise.all([
      sourceHashes.length && translationHashes.length ? postgrestRequest("editorial_source_evidence", "GET", { params: { policy_version: "eq.calm-inviting-v1", surah_number: `eq.${surah}`,
        ayah_number: `in.(${keys})`, source_hash: `in.(${sourceHashes.join(",")})`, translation_hash: `in.(${translationHashes.join(",")})`,
        select: "*", order: "retrieved_at.desc", limit: "51" } }) : [],
      runs.length ? postgrestRequest("editorial_ayah_assessments", "GET", { params: { policy_version: "eq.calm-inviting-v1", surah_number: `eq.${surah}`,
        ayah_number: `in.(${keys})`, run_id: `in.(${runs.join(",")})`, select: "*", order: "ayah_number.asc,pass_number.asc", limit: "51" } }) : [],
    ]);
    if (!Array.isArray(sources) || sources.length > 50 || !Array.isArray(assessments)
      || assessments.length > 50) throw new Error("Truncated or malformed editorial evidence.");
    const audit = auditEditorialEvidencePage(selected, sources, assessments, "calm-inviting-v1");
    return { rows: selected.map((row) => ({ ayah_number: row.ayah_number, screening_status: row.screening_status,
      evidenceIssues: audit.issues.find((issue) => issue.key === `${surah}:${row.ayah_number}`)?.reasons ?? [],
      semantic_assessment: row.semantic_assessment, productionApproved: approvedKeys.has(`${surah}:${row.ayah_number}`),
      source: sources.find((source) => source.ayah_number === row.ayah_number && source.source_hash === row.source_hash
        && source.translation_hash === row.translation_hash) ?? null,
      assessments: assessments.filter((assessment) => assessment.ayah_number === row.ayah_number && assessment.run_id === row.screening_run_id),
    })), hasNext: reviews.length > 25, error: null };
  } catch {
    return { rows: [], hasNext: false, error: "Ayah evidence could not be verified from Supabase." };
  }
}

export interface SupabaseServerConfig {
  url: string;
  serviceRoleKey: string;
}

export const LIVE_TEST_WRITE_OPT_IN_ENV = "QURAN_REELS_ALLOW_LIVE_TEST_WRITES";

function assertSupabaseTransportAllowedForTests(): void {
  // Vitest runs with NODE_ENV=test.  This check prevents an inherited
  // production service-role environment from turning ordinary Dashboard tests
  // into real PostgREST calls. Production/server execution is unchanged.
  if (
    process.env.NODE_ENV === "test" &&
    process.env[LIVE_TEST_WRITE_OPT_IN_ENV] !== "1"
  ) {
    throw new Error(
      "Live Supabase access is blocked during Dashboard tests. " +
        `Set ${LIVE_TEST_WRITE_OPT_IN_ENV}=1 only for an explicitly reviewed live integration test.`
    );
  }
}

export function getSupabaseServerConfig(): { url: string; serviceRoleKey: string } {
  const url = (process.env.SUPABASE_URL || "")
    .trim()
    .replace(/^["']|["']$/g, "")
    .replace(/\/+$/, "");
  const serviceRoleKey = (process.env.SUPABASE_SERVICE_ROLE_KEY || "")
    .trim()
    .replace(/^["']|["']$/g, "");
  return { url, serviceRoleKey };
}

async function postgrestRequest(
  table: string,
  method: string = "GET",
  options: {
    params?: Record<string, string>;
    body?: any;
    prefer?: string;
  } = {}
): Promise<any> {
  assertSupabaseTransportAllowedForTests();
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    throw new Error(
      "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required server-side."
    );
  }

  const searchParams = new URLSearchParams(options.params || {});
  const endpoint = `${url}/rest/v1/${table}${searchParams.toString() ? `?${searchParams.toString()}` : ""}`;

  const headers: Record<string, string> = {
    apikey: serviceRoleKey,
    Authorization: `Bearer ${serviceRoleKey}`,
    "Content-Type": "application/json",
  };
  if (options.prefer) {
    headers["Prefer"] = options.prefer;
  }

  const response = await fetch(endpoint, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
  });

  if (!response.ok) {
    if (response.status === 409) {
      return { _conflict: true, status: 409 };
    }
    const errText = await response.text().catch(() => "");
    throw new Error(
      `Supabase PostgREST ${method} ${table} failed with HTTP ${response.status}: ${errText}`
    );
  }

  const text = await response.text();
  if (!text) return [];
  return JSON.parse(text);
}

/** Server-only, cheap operational queue summary for the dashboard. */
export async function getBackgroundCandidateSummaryFromSupabase(): Promise<{
  available: boolean; total: number; pending: number; processing: number; verified: number;
  safetyRejected: number; metadataIneligible: number; transientFailure: number;
  lastActivity: string | null; lastDiscovery: string | null;
}> {
  const empty = { available: false, total: 0, pending: 0, processing: 0, verified: 0, safetyRejected: 0, metadataIneligible: 0, transientFailure: 0, lastActivity: null, lastDiscovery: null };
  try {
    const rows = await allBackgroundRows("background_candidates", { select: "status,updated_at,discovered_at", order: "candidate_id" });
    for (const row of rows || []) {
      empty.total += 1;
      if (row.status === "PENDING") empty.pending += 1;
      else if (row.status === "PROCESSING") empty.processing += 1;
      else if (row.status === "VERIFIED") empty.verified += 1;
      else if (row.status === "SAFETY_REJECTED") empty.safetyRejected += 1;
      else if (row.status === "METADATA_INELIGIBLE") empty.metadataIneligible += 1;
      else if (row.status === "TRANSIENT_FAILURE") empty.transientFailure += 1;
      if (typeof row.updated_at === "string" && (!empty.lastActivity || row.updated_at > empty.lastActivity)) empty.lastActivity = row.updated_at;
      if (typeof row.discovered_at === "string" && (!empty.lastDiscovery || row.discovered_at > empty.lastDiscovery)) empty.lastDiscovery = row.discovered_at;
    }
    return { ...empty, available: true };
  } catch {
    return empty;
  }
}

// Keep aligned with engine/background_pool.py; this is reporting, never selection authority.
export const BACKGROUND_SAFETY_POLICY_FINGERPRINT = "414bdef0079708e46f947b50bfb2de7a74d6e31d1f35217f637f4ecc7908462d";

async function allBackgroundRows(table: string, params: Record<string, string>): Promise<any[]> {
  const rows: any[] = [];
  // Supabase limits each response. Stable ordering avoids silently truncated totals.
  for (let offset = 0; ; offset += 500) {
    const page = await postgrestRequest(table, "GET", { params: { ...params, limit: "500", offset: String(offset) } });
    if (!Array.isArray(page)) throw new Error("Invalid background summary response");
    rows.push(...page);
    if (page.length < 500) return rows;
  }
}

export async function getBackgroundPoolOperationsSummaryFromSupabase() {
  const empty = { available: false, total: 0, verified: 0, stale: 0, selectable: 0, highRisk: 0, unused: 0, used: 0 };
  try {
    const [rows, events] = await Promise.all([
      allBackgroundRows("background_assets", { select: "*", order: "provider,provider_asset_id,provider_variant_id,policy_fingerprint,content_sha256" }),
      allBackgroundRows("background_events", { select: "event_type,provider,provider_asset_id,value", event_type: "in.(usage,stale)", order: "event_id" }),
    ]);
    const used = new Set(events.filter(row => row.event_type === "usage").map(row => `${row.provider}:${row.provider_asset_id}`));
    const staleIds = new Set(events.filter(row => row.event_type === "stale").map(row => row.value?.verified_record_id).filter(Boolean));
    const selectable = new Set<string>();
    for (const row of rows) {
      empty.total += 1;
      const value = row.value || {};
      const safety = value.safety || {};
      const id = `${row.provider}:${row.provider_asset_id}:${row.provider_variant_id}:${row.content_sha256}:${row.policy_fingerprint}`;
      const stale = row.status === "stale" || value.active === false || row.policy_fingerprint !== BACKGROUND_SAFETY_POLICY_FINGERPRINT || staleIds.has(id) || staleIds.has(value.record_id);
      if (stale) empty.stale += 1;
      if (row.status === "verified") empty.verified += 1;
      const robustness = value.render_robustness || {};
      const highRisk = robustness.risk_tier === "HIGH_RISK" || value.risk_tier === "HIGH_RISK" || (typeof robustness.failed_evaluation_rate === "number" && robustness.failed_evaluation_rate > 0.15);
      if (highRisk) empty.highRisk += 1;
      const safe = safety.verdict === "SAFE" && safety.person_result === "CLEAR" && safety.face_result === "CLEAR" && safety.animal_verdict === "CLEAR" && safety.full_source_scan === true && safety.policy_fingerprint === BACKGROUND_SAFETY_POLICY_FINGERPRINT;
      if (row.status === "verified" && !stale && !highRisk && safe) selectable.add(`${row.provider}:${row.provider_asset_id}`);
    }
    empty.selectable = selectable.size;
    empty.used = [...selectable].filter(id => used.has(id)).length;
    empty.unused = empty.selectable - empty.used;
    return { ...empty, available: true };
  } catch {
    return empty;
  }
}

export async function getBackgroundDiscoveryStateFromSupabase(): Promise<{
  category: string; nextCategory: string; completedAt: string; queryCursor: number; pageCursor: number;
} | null> {
  try {
    const rows = await postgrestRequest("background_builder_runs", "GET", { params: {
      builder_run_id: "eq.background-discovery-rotation-v1", select: "value", limit: "1",
    } });
    const value = rows?.[0]?.value;
    if (!value || typeof value.category !== "string" || typeof value.next_category !== "string" || typeof value.completed_at !== "string" || !Number.isInteger(value.query_cursor) || !Number.isInteger(value.page_cursor)) return null;
    return { category: value.category, nextCategory: value.next_category, completedAt: value.completed_at, queryCursor: value.query_cursor, pageCursor: value.page_cursor };
  } catch { return null; }
}

export async function getRecentBackgroundReuseFromSupabase(): Promise<{ available: boolean; count: number; lastAt: string | null }> {
  try {
    const rows = await postgrestRequest("run_records", "GET", { params: { run_kind: "in.(manual_production,scheduled)", select: "value,completed_at", order: "completed_at.desc.nullslast", limit: "30" } });
    let count = 0;
    let lastAt: string | null = null;
    for (const row of rows) {
      const warnings = row.value?.provenance?.background_reuse_warnings;
      if (!Array.isArray(warnings)) continue;
      const matched = warnings.filter((warning: any) => warning?.event === "BACKGROUND_REUSE_FALLBACK").length;
      count += matched;
      if (matched && !lastAt) lastAt = row.completed_at || null;
    }
    return { available: true, count, lastAt };
  } catch { return { available: false, count: 0, lastAt: null }; }
}

/**
 * Fetch and parse runtime configuration from Supabase (app_config table).
 */
export async function getStoredConfigStateFromSupabase(): Promise<StoredConfigLoadState> {
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    return {
      config: StoredConfigSchema.parse({}),
      authoritative: false,
      writable: false,
      source: "fallback",
      errorCode: "SUPABASE_NOT_CONFIGURED",
    };
  }

  try {
    const rows = await postgrestRequest("app_config", "GET", {
      params: { singleton: "eq.true", select: "value" },
    });

    if (!rows || rows.length === 0) {
      return {
        config: StoredConfigSchema.parse({}),
        authoritative: true,
        writable: true,
        source: "missing",
        errorCode: null,
      };
    }

    const parsed = StoredConfigSchema.parse(rows[0].value);
    return {
      config: parsed,
      authoritative: true,
      writable: true,
      source: "b2", // indicates valid authoritative source
      errorCode: null,
    };
  } catch (err: any) {
    console.error("[Supabase] Error loading stored configuration:", err);
    return {
      config: StoredConfigSchema.parse({}),
      authoritative: false,
      writable: false,
      source: "fallback",
      errorCode: "SUPABASE_CONFIG_UNAVAILABLE",
    };
  }
}

export async function getStoredConfigFromSupabase(): Promise<StoredConfig> {
  return (await getStoredConfigStateFromSupabase()).config;
}

export async function saveStoredConfigToSupabase(
  config: Partial<StoredConfig>
): Promise<StoredConfig> {
  const configToValidate = {
    ...config,
    updated_at: new Date().toISOString(),
  };
  const validated = StoredConfigSchema.parse(configToValidate);

  await postgrestRequest("app_config", "POST", {
    body: {
      singleton: true,
      schema_version: validated.schema_version,
      value: validated,
    },
    prefer: "resolution=merge-duplicates,return=minimal",
  });

  return validated;
}

/**
 * Fetch Quran Coverage summary from Supabase coverage_state table.
 */
export async function getCoverageSummaryFromSupabase(): Promise<{
  summary: CoverageSummary;
  error?: string;
}> {
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

  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    return { summary: defaultSummary };
  }

  try {
    const rows = await postgrestRequest("coverage_state", "GET", {
      params: { singleton: "eq.true", select: "value" },
    });

    if (!rows || rows.length === 0) {
      return { summary: defaultSummary };
    }

    const parsed = CoverageStateSchema.parse(rows[0].value);
    const remaining = Math.max(0, parsed.total_ayahs - parsed.covered_count);
    const progress =
      parsed.total_ayahs > 0
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
    console.error("[Supabase] Error loading coverage state:", err);
    return {
      summary: defaultSummary,
      error: `Could not verify coverage state from Supabase: ${err.message || "Malformed record"}`,
    };
  }
}

export type EditorialCoverageLoadResult =
  | { status: "ready"; summary: EditorialCoverageSummary; coveredAyahKeys?: readonly string[]; error: null }
  | { status: "missing"; summary: null; error: null }
  | { status: "error"; summary: null; error: string };

export interface EditorialScreeningSummary {
  policy_version: string;
  canonical_ayahs: number;
  screened_ayahs: number;
  not_screened_ayahs: number;
  eligible_candidates: number;
  ineligible_candidates: number;
  ambiguous_candidates: number;
  production_approved_ayahs: number;
  automatic_production_approved_ayahs: number;
  legacy_production_approved_ayahs: number;
  production_approved_passages: number;
  last_successful_screening_at: string | null;
}

export interface EditorialWorkerStatus {
  status: "STARTING" | "RUNNING" | "PAUSED" | "FAILED" | "COMPLETED" | "STALE";
  heartbeat_at: string;
  last_error: string | null;
  committed_ayahs: number;
  failed_ayahs: number;
}

export interface EditorialDraftSummary {
  total: number;
  awaiting_second_pass: number;
  source_unverified: number;
}

export type EditorialScreeningLoadResult =
  | {
    status: "ready";
    summary: EditorialScreeningSummary;
    worker: EditorialWorkerStatus | null;
    preliminaryDrafts?: EditorialDraftSummary | null;
    productionAuthorizedCandidateKeys: readonly string[];
    productionAuthorizedRanges?: readonly string[];
  }
  | { status: "unavailable"; summary: null; worker: null; productionAuthorizedCandidateKeys: readonly []; error: string };

interface ProductionAuthorizedPassageRow {
  policy_version: string;
  surah_number: number;
  passage_first_ayah: number;
  passage_last_ayah: number;
  candidate_ayah_keys: string[];
}

function isValidProductionAuthorizedPassageRow(value: unknown): value is ProductionAuthorizedPassageRow {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return row.policy_version === "calm-inviting-v1"
    && Number.isInteger(row.surah_number)
    && Number.isInteger(row.passage_first_ayah)
    && Number.isInteger(row.passage_last_ayah)
    && Array.isArray(row.candidate_ayah_keys)
    && row.candidate_ayah_keys.every((key) => typeof key === "string");
}

/**
 * Read only identity/range metadata from the service-only authorization RPC.
 * The result is bounded by the canonical Quran and contains no screening prose
 * or source evidence that could reach the browser.
 */
async function getProductionAuthorizedCandidateKeysFromSupabase(): Promise<readonly string[]> {
  const rows: unknown[] = [];
  for (let offset = 0; ; offset += 500) {
    if (offset > TOTAL_CANONICAL_AYAHS * 2) throw new Error("Authorization response exceeds the canonical identity bound.");
    const page = await postgrestRequest("editorial_production_authorized_ayahs", "GET", { params: {
      policy_version: "eq.calm-inviting-v1",
      select: "policy_version,surah_number,ayah_number,passage_first_ayah,passage_last_ayah",
      order: "authorization_id.asc", limit: "500", offset: String(offset),
    } });
    if (!Array.isArray(page)) throw new Error("Malformed production authorization response.");
    rows.push(...page.map((row) => ({ ...row, candidate_ayah_keys: [`${row.surah_number}:${row.ayah_number}`] })));
    if (page.length < 500) break;
  }
  if (!Array.isArray(rows) || rows.length > TOTAL_CANONICAL_AYAHS * 2) {
    throw new Error("Malformed production authorization response.");
  }
  const keys = new Set<string>();
  for (const row of rows) {
    if (!isValidProductionAuthorizedPassageRow(row)
      || row.passage_first_ayah < 1
      || row.passage_first_ayah > row.passage_last_ayah) {
      throw new Error("Malformed production authorization passage.");
    }
    for (const key of row.candidate_ayah_keys) {
      if (!isCanonicalAyahKey(key)) {
        throw new Error("Production authorization contains a non-canonical ayah.");
      }
      const [surah, ayah] = key.split(":").map(Number);
      if (surah !== row.surah_number
        || ayah < row.passage_first_ayah
        || ayah > row.passage_last_ayah) {
        throw new Error("Production authorization candidate is outside its passage.");
      }
      keys.add(key);
    }
  }
  return [...keys].sort((left, right) => {
    const [leftSurah, leftAyah] = left.split(":").map(Number);
    const [rightSurah, rightAyah] = right.split(":").map(Number);
    return leftSurah - rightSurah || leftAyah - rightAyah;
  });
}

/** Server-side bounded exact ranges, without privileged assessment/source prose. */
async function getProductionAuthorizedRangesFromSupabase(): Promise<readonly string[]> {
  const ranges = new Set<string>();
  for (let offset = 0; offset <= TOTAL_CANONICAL_AYAHS * 2; offset += 500) {
    const rows = await postgrestRequest("editorial_production_authorized_passages", "GET", { params: {
      policy_version: "eq.calm-inviting-v1",
      select: "surah_number,passage_first_ayah,passage_last_ayah",
      order: "surah_number.asc,passage_first_ayah.asc,passage_last_ayah.asc", limit: "500", offset: String(offset),
    } });
    if (!Array.isArray(rows)) throw new Error("Malformed production passage response.");
    for (const row of rows) {
      if (!Number.isInteger(row.surah_number) || !Number.isInteger(row.passage_first_ayah)
        || !Number.isInteger(row.passage_last_ayah) || row.passage_first_ayah > row.passage_last_ayah
        || !isCanonicalAyahKey(`${row.surah_number}:${row.passage_first_ayah}`)
        || !isCanonicalAyahKey(`${row.surah_number}:${row.passage_last_ayah}`)) throw new Error("Non-canonical production passage.");
      const key = editorialRangeKey(row.surah_number, row.passage_first_ayah, row.passage_last_ayah);
      if (ranges.has(key)) throw new Error("Duplicate production passage.");
      ranges.add(key);
    }
    if (rows.length < 500) return [...ranges];
  }
  throw new Error("Production passage response exceeds its bounded size.");
}

/**
 * Preliminary ChatGPT notes are deliberately separate from the screening
 * ledger. They are bounded informational metadata and never participate in
 * production authorization or official screening totals.
 */
async function getEditorialDraftSummaryFromSupabase(): Promise<EditorialDraftSummary> {
  const rows: any[] = [];
  for (let offset = 0; ; offset += 500) {
    if (offset > TOTAL_CANONICAL_AYAHS) throw new Error("Draft response exceeds the canonical identity bound.");
    const page = await postgrestRequest("editorial_chat_review_drafts", "GET", { params: {
      policy_version: "eq.calm-inviting-v1",
      select: "preliminary_assessment,source_integrity_state,second_pass_state",
      order: "surah_number.asc,ayah_number.asc", limit: "500", offset: String(offset),
    } });
    if (!Array.isArray(page)) throw new Error("Malformed preliminary draft response.");
    rows.push(...page);
    if (page.length < 500) break;
  }
  if (rows.length > TOTAL_CANONICAL_AYAHS) throw new Error("Preliminary draft count exceeds canonical identities.");
  for (const row of rows) {
    if (!row || !["ELIGIBLE_CANDIDATE", "INELIGIBLE_CANDIDATE", "AMBIGUOUS_REVIEW_REQUIRED"].includes(row.preliminary_assessment)
      || row.source_integrity_state !== "UNVERIFIED" || row.second_pass_state !== "NOT_PERFORMED") {
      throw new Error("Malformed preliminary draft state.");
    }
  }
  return {
    total: rows.length,
    awaiting_second_pass: rows.filter((row) => row.second_pass_state === "NOT_PERFORMED").length,
    source_unverified: rows.filter((row) => row.source_integrity_state === "UNVERIFIED").length,
  };
}

/** Read the live durable screening ledger; this is distinct from coverage_state. */
export async function getEditorialScreeningFromSupabase(): Promise<EditorialScreeningLoadResult> {
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    return { status: "unavailable", summary: null, worker: null, productionAuthorizedCandidateKeys: [], error: "Live editorial screening is not configured." };
  }
  try {
    const [summaryRows, workerRows, productionAuthorizedCandidateKeys, productionAuthorizedRanges] = await Promise.all([
      postgrestRequest("editorial_screening_worker_summary", "GET", {
        params: { policy_version: "eq.calm-inviting-v1", select: "*", limit: "1" },
      }),
      postgrestRequest("editorial_screening_worker_jobs", "GET", {
        params: { policy_version: "eq.calm-inviting-v1", select: "status,heartbeat_at,last_error,committed_ayahs,failed_ayahs", order: "updated_at.desc", limit: "1" },
      }),
      getProductionAuthorizedCandidateKeysFromSupabase(),
      getProductionAuthorizedRangesFromSupabase(),
    ]);
    const value = summaryRows?.[0];
    const fields = [
      "canonical_ayahs", "screened_ayahs", "not_screened_ayahs",
      "eligible_candidates", "ineligible_candidates", "ambiguous_candidates",
      "production_approved_ayahs", "automatic_production_approved_ayahs",
      "legacy_production_approved_ayahs", "production_approved_passages",
    ];
    if (!value || value.policy_version !== "calm-inviting-v1" || fields.some((field) => !Number.isInteger(value[field]) || value[field] < 0)
      || value.canonical_ayahs !== TOTAL_CANONICAL_AYAHS
      || value.screened_ayahs + value.not_screened_ayahs !== TOTAL_CANONICAL_AYAHS
      || value.eligible_candidates + value.ineligible_candidates + value.ambiguous_candidates !== value.screened_ayahs
      || value.automatic_production_approved_ayahs > value.eligible_candidates
      || value.production_approved_ayahs > TOTAL_CANONICAL_AYAHS) {
      throw new Error("Malformed editorial screening summary.");
    }
    if (productionAuthorizedCandidateKeys.length !== value.production_approved_ayahs) {
      throw new Error("Production authorization identities do not reconcile with the live summary.");
    }
    if (productionAuthorizedRanges.length !== value.production_approved_passages) throw new Error("Production passage totals do not reconcile.");
    const worker = workerRows?.[0];
    if (worker && (!["STARTING", "RUNNING", "PAUSED", "FAILED", "COMPLETED"].includes(worker.status)
      || !Number.isFinite(Date.parse(worker.heartbeat_at))
      || !Number.isInteger(worker.committed_ayahs) || worker.committed_ayahs < 0
      || !Number.isInteger(worker.failed_ayahs) || worker.failed_ayahs < 0)) {
      throw new Error("Malformed editorial worker status.");
    }
    let preliminaryDrafts: EditorialDraftSummary | null = null;
    try {
      preliminaryDrafts = await getEditorialDraftSummaryFromSupabase();
    } catch {
      // Draft reporting is informational and must not hide authoritative
      // screening totals when the private table is unavailable.
      preliminaryDrafts = null;
    }
    return {
      status: "ready",
      summary: value as EditorialScreeningSummary,
      worker: worker ? { ...worker, status: ["STARTING", "RUNNING"].includes(worker.status)
        && Date.now() - Date.parse(worker.heartbeat_at) > 15 * 60 * 1000 ? "STALE" : worker.status } : null,
      preliminaryDrafts,
      productionAuthorizedCandidateKeys,
      productionAuthorizedRanges,
    };
  } catch {
    console.error("[Supabase] Editorial screening summary could not be verified.");
    return { status: "unavailable", summary: null, worker: null, productionAuthorizedCandidateKeys: [], error: "Live editorial screening could not be verified from Supabase." };
  }
}

/** Read the current cycle from Supabase and count only keys in the versioned catalog. */
export async function getEditorialCoverageFromSupabase(): Promise<EditorialCoverageLoadResult> {
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    return {
      status: "error",
      summary: null,
      error: "Editorial coverage is unavailable because the Supabase connection is not configured.",
    };
  }

  try {
    const rows = await postgrestRequest("coverage_state", "GET", {
      params: { singleton: "eq.true", select: "value" },
    });
    if (!Array.isArray(rows)) {
      throw new Error("Coverage state response is malformed.");
    }
    if (rows.length === 0) {
      return { status: "missing", summary: null, error: null };
    }
    if (rows.length !== 1) {
      throw new Error("Coverage state response did not contain one singleton record.");
    }

    const value = rows[0]?.value;
    if (
      !value ||
      typeof value !== "object" ||
      Array.isArray(value) ||
      !Array.isArray(value.covered_ayahs) ||
      !Number.isInteger(value.cycle_number) ||
      value.cycle_number < 1
    ) {
      throw new Error("Coverage state is missing its authoritative cycle keys.");
    }

    const parsed = CoverageStateSchema.parse(value);
    return {
      status: "ready",
      summary: computeEditorialCoverage(
        parsed.covered_ayahs,
        parsed.cycle_number,
        parsed.last_updated_at || null,
      ),
      coveredAyahKeys: parsed.covered_ayahs,
      error: null,
    };
  } catch {
    console.error("[Supabase] Editorial coverage could not be verified.");
    return {
      status: "error",
      summary: null,
      error: "Editorial coverage could not be verified from Supabase. Retry after the data source is available.",
    };
  }
}

/**
 * Meta Credentials from Supabase
 */
export async function getEncryptedMetaCredentialsFromSupabase(): Promise<string | null> {
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) return null;

  try {
    const rows = await postgrestRequest("meta_credentials", "GET", {
      params: { singleton: "eq.true", select: "encrypted_envelope" },
    });
    return rows && rows.length > 0 ? rows[0].encrypted_envelope : null;
  } catch (err) {
    console.error("[Supabase] Error loading meta credentials:", err);
    return null;
  }
}

export async function saveEncryptedMetaCredentialsToSupabase(
  envelope: string
): Promise<void> {
  await postgrestRequest("meta_credentials", "POST", {
    body: { singleton: true, encrypted_envelope: envelope },
    prefer: "resolution=merge-duplicates,return=minimal",
  });
}

export async function deleteEncryptedMetaCredentialsFromSupabase(): Promise<void> {
  await postgrestRequest("meta_credentials", "DELETE", {
    params: { singleton: "eq.true" },
    prefer: "return=minimal",
  });
}

/**
 * Run Records from Supabase
 */
export async function getRunRecordFromSupabase(
  date: string,
  slot: string,
  runId?: string,
  runKind?: "test" | "manual_production" | "scheduled"
): Promise<RunRecord | null> {
  const params: Record<string, string> = { select: "value", limit: "1" };
  if (runId) {
    params["run_id"] = `eq.${runId}`;
  } else {
    params["berlin_date"] = `eq.${date}`;
    if (slot !== "test" && slot !== "manual-production") {
      params["schedule_slot_id"] = `eq.${slot}`;
    }
  }

  try {
    const rows = await postgrestRequest("run_records", "GET", { params });
    if (!rows || rows.length === 0) return null;
    return RunRecordSchema.parse(rows[0].value);
  } catch (err) {
    console.error("[Supabase] Error fetching run record:", err);
    return null;
  }
}

/** Fetch one RunRecord by its durable logical run ID. */
export async function getRunRecordByIdFromSupabase(runId: string): Promise<RunRecord | null> {
  if (!/^[A-Za-z0-9_.-]{1,160}$/.test(runId)) return null;
  try {
    const rows = await postgrestRequest("run_records", "GET", {
      params: { run_id: `eq.${runId}`, select: "value", limit: "1" },
    });
    if (!rows || rows.length === 0) return null;
    return RunRecordSchema.parse(rows[0].value);
  } catch (err) {
    console.error("[Supabase] Error fetching run record by ID:", err);
    return null;
  }
}

export async function listRecentRunRecordsFromSupabase(
  limit = 30
): Promise<RunRecord[]> {
  try {
    const rows = await postgrestRequest("run_records", "GET", {
      params: {
        select: "value",
        order: "completed_at.desc.nullslast",
        limit: String(limit),
      },
    });
    if (!rows) return [];
    return rows.map((r: any) => RunRecordSchema.parse(r.value));
  } catch (err) {
    console.error("[Supabase] Error listing run records:", err);
    return [];
  }
}

/**
 * The only count for the OWNER'S FRESH 2,211-ayah publication cycle.
 * Old run_records, Buffer claims, coverage_state and historic videos are
 * deliberately ignored. Missing/invalid state is UNKNOWN, never 0.
 */
export type CalmReelsFreshCycleLoad =
  | { status: "ready"; progress: CalmReelsCycleProgress }
  | { status: "unavailable"; progress: null; error: string };

export async function getCalmReelsFreshCycleFromSupabase(): Promise<CalmReelsFreshCycleLoad> {
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    return { status: "unavailable", progress: null, error: "Fresh cycle Supabase connection unavailable." };
  }
  try {
    const stateRows = await postgrestRequest("calm_reels_v1_cycle_state", "GET", {
      params: { select: "catalog_id,catalog_sha256,total_ayahs,cycle_number,started_at", singleton: "eq.true", limit: "2" },
    });
    if (!Array.isArray(stateRows) || stateRows.length !== 1) throw new Error("Missing/duplicate cycle");
    const row = stateRows[0];
    if (!Number.isSafeInteger(row.cycle_number) || row.cycle_number < 1) throw new Error("Invalid cycle");
    const keys: string[] = [];
    for (let offset = 0; offset <= 2211; offset += 500) {
      const page = await postgrestRequest("calm_reels_v1_confirmed_posts", "GET", {
        params: {
          select: "ayah_key,run_id,media_key,external_post_id,evidence_sha256,verified_at,cycle_number",
          cycle_number: `eq.${row.cycle_number}`, order: "ayah_key.asc",
          limit: "500", offset: String(offset),
        },
      });
      if (!Array.isArray(page) || keys.length + page.length > 2211) throw new Error("Malformed confirmed posts");
      for (const item of page) {
        if (item?.cycle_number !== row.cycle_number
          || typeof item.run_id !== "string" || !item.run_id
          || typeof item.media_key !== "string" || !item.media_key
          || typeof item.external_post_id !== "string" || !item.external_post_id
          || typeof item.evidence_sha256 !== "string" || !/^[0-9a-f]{64}$/u.test(item.evidence_sha256)
          || typeof item.verified_at !== "string" || !Number.isFinite(Date.parse(item.verified_at))) {
          throw new Error("Unverifiable fresh confirmed post");
        }
        keys.push(item.ayah_key);
      }
      if (page.length < 500) break;
    }
    return { status: "ready",
      progress: calculateCalmReelsCycleProgress(row, keys, getCalmReelsShortlistKeys()) };
  } catch {
    console.error("[Supabase] Fresh Calm Reels v1 publication cycle unavailable or invalid.");
    return { status: "unavailable", progress: null,
      error: "Current Calm Reels v1 confirmed publication count could not be verified." };
  }
}

export type PublishingEvidenceLoadResult =
  | { status: "ready"; records: RunRecord[]; claims: BufferPublicationClaim[] }
  | { status: "unavailable"; records: []; claims: []; error: string };

/** Read the bounded publication ledger and run evidence; this path never writes. */
export async function getPublishingEvidenceFromSupabase(): Promise<PublishingEvidenceLoadResult> {
  const { url, serviceRoleKey } = getSupabaseServerConfig();
  if (!url || !serviceRoleKey) {
    return { status: "unavailable", records: [], claims: [], error: "Publication evidence is not available." };
  }

  async function readAll(table: string, select: string, order: string): Promise<any[]> {
    const result: any[] = [];
    const pageSize = 500;
    for (let offset = 0; ; offset += pageSize) {
      const rows = await postgrestRequest(table, "GET", {
        params: { select, order, limit: String(pageSize), offset: String(offset) },
      });
      if (!Array.isArray(rows)) throw new Error("Publication ledger response is malformed.");
      result.push(...rows);
      if (rows.length < pageSize) return result;
    }
  }

  try {
    const [runRows, claimRows] = await Promise.all([
      readAll("run_records", "value", "run_id.asc"),
      readAll("buffer_publication_claims", "run_id,platform,publisher_backend,channel_id,state,buffer_post_id,buffer_post_status,external_link,accepted_at,last_error_code,updated_at", "run_id.asc,platform.asc,publisher_backend.asc,channel_id.asc"),
    ]);
    const records = runRows.map((row) => RunRecordSchema.parse(row?.value));
    const claims = claimRows.map((row) => BufferPublicationClaimSchema.parse(row));
    return { status: "ready", records, claims };
  } catch {
    console.error("[Supabase] Publication evidence could not be verified.");
    return {
      status: "unavailable",
      records: [],
      claims: [],
      error: "Publication evidence could not be verified from Supabase.",
    };
  }
}

export type ApprovedReelsLoadResult =
  | { status: "ready"; records: RunRecord[] }
  | { status: "error"; records: []; error: string };

/** Filter production candidates before limiting the final approved list. */
export async function listRecentApprovedReelsFromSupabase(limit = 5, authorizedRanges?: AuthorizedEditorialRanges): Promise<ApprovedReelsLoadResult> {
  const target = Math.max(1, Math.min(50, Math.trunc(limit) || 5));
  const pageSize = Math.max(50, target * 3);
  const records: RunRecord[] = [];
  const seen = new Set<string>();
  let offset = 0;
  try {
    while (records.length < target && offset < 5000) {
      const rows = await postgrestRequest("run_records", "GET", {
        params: {
          select: "value",
          run_kind: "in.(scheduled,manual_production)",
          status: "eq.SUCCESS",
          final_safety_passed: "eq.true",
          media_key: "not.is.null",
          order: "completed_at.desc,run_id.desc",
          limit: String(pageSize),
          offset: String(offset),
        },
      });
      if (!Array.isArray(rows)) throw new Error("Malformed run history response");
      for (const row of rows) {
        const parsed = RunRecordSchema.safeParse(row?.value);
        if (!parsed.success || seen.has(parsed.data.run_id)) continue;
        seen.add(parsed.data.run_id);
        if (isApprovedDeliveredReel(parsed.data, authorizedRanges)) records.push(parsed.data);
        if (records.length === target) break;
      }
      if (rows.length < pageSize) break;
      offset += pageSize;
    }
    return { status: "ready", records };
  } catch {
    console.error("[Supabase] Approved Reel history could not be verified.");
    return { status: "error", records: [], error: "Approved Reel history is unavailable. Technical Run History remains accessible." };
  }
}

/**
 * Scheduler Claims from Supabase
 */
export async function claimSchedulerSlotInSupabase(
  date: string,
  slotId: string,
  claimedAt?: string,
  workerRunId?: string
): Promise<boolean> {
  const iso = claimedAt || new Date().toISOString();
  const runId = workerRunId || `claim_${date}_${slotId}`;

  const res = await postgrestRequest("scheduler_claims", "POST", {
    body: {
      berlin_date: date,
      slot_id: slotId,
      run_id: runId,
      status: "CLAIMED",
      value: {
        date,
        slot_id: slotId,
        run_id: runId,
        status: "CLAIMED",
        claimed_at: iso,
        updated_at: iso,
      },
    },
    prefer: "return=minimal",
  });

  if (res && res._conflict) {
    return false;
  }
  return true;
}

export async function updateSchedulerClaimInSupabase(
  claim: {
    date: string;
    slot_id: string;
    run_id: string;
    status: "CLAIMED" | "DISPATCHED" | "FAILED";
    claimed_at: string;
    updated_at: string;
    failure_reason?: string;
  }
): Promise<void> {
  const updatedAt = claim.updated_at || new Date().toISOString();
  const valuePayload: Record<string, any> = {
    date: claim.date,
    slot_id: claim.slot_id,
    run_id: claim.run_id,
    status: claim.status,
    claimed_at: claim.claimed_at,
    updated_at: updatedAt,
  };
  if (claim.failure_reason) {
    valuePayload.failure_reason = claim.failure_reason;
  }

  const rows = await postgrestRequest("scheduler_claims", "PATCH", {
    params: {
      berlin_date: `eq.${claim.date}`,
      slot_id: `eq.${claim.slot_id}`,
    },
    body: {
      run_id: claim.run_id,
      status: claim.status,
      value: valuePayload,
      updated_at: updatedAt,
    },
    prefer: "return=representation",
  });

  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error(
      `Cannot update scheduler claim for ${claim.date}/${claim.slot_id}: claim does not exist.`
    );
  }
}

function parseSchedulerClaimStatus(status: unknown): "CLAIMED" | "DISPATCHED" | "FAILED" {
  if (status === "DISPATCHED") return "DISPATCHED";
  if (status === "FAILED") return "FAILED";
  return "CLAIMED";
}

export async function getSchedulerClaimFromSupabase(
  date: string,
  slotId: string
): Promise<{
  date: string;
  slot_id: string;
  claimed_at: string;
  worker_run_id: string;
  status: "CLAIMED" | "DISPATCHED" | "FAILED";
  updated_at: string;
  failure_reason?: string;
} | null> {
  try {
    const rows = await postgrestRequest("scheduler_claims", "GET", {
      params: {
        berlin_date: `eq.${date}`,
        slot_id: `eq.${slotId}`,
        select: "*",
        limit: "1",
      },
    });
    if (!rows || rows.length === 0) return null;
    const row = rows[0];
    return {
      date: row.berlin_date,
      slot_id: row.slot_id,
      claimed_at: row.value?.claimed_at || row.updated_at,
      worker_run_id: row.run_id,
      status: parseSchedulerClaimStatus(row.status || row.value?.status),
      updated_at: row.updated_at || row.value?.updated_at || row.value?.claimed_at,
      failure_reason: row.value?.failure_reason,
    };
  } catch (err) {
    console.error("[Supabase] Error getting scheduler claim:", err);
    return null;
  }
}

export async function releaseSchedulerClaimInSupabase(
  date: string,
  slotId: string
): Promise<void> {
  await postgrestRequest("scheduler_claims", "DELETE", {
    params: {
      berlin_date: `eq.${date}`,
      slot_id: `eq.${slotId}`,
    },
  });
}

export async function recordSchedulerHeartbeatInSupabase(entry: {
  heartbeatId: string;
  source: string;
  occurredAt: string;
  dueSlotCount: number;
  claimCount: number;
  dispatchCount: number;
  result: Record<string, any>;
}): Promise<boolean> {
  try {
    await postgrestRequest("scheduler_heartbeats", "POST", {
      body: {
        heartbeat_id: entry.heartbeatId,
        source: entry.source,
        occurred_at: entry.occurredAt,
        due_slot_count: entry.dueSlotCount,
        claim_count: entry.claimCount,
        dispatch_count: entry.dispatchCount,
        result: entry.result,
      },
      prefer: "return=minimal",
    });
    return true;
  } catch (err) {
    // Graceful: Table might not yet exist or Supabase write failed
    return false;
  }
}

export async function getLatestSchedulerHeartbeatFromSupabase(): Promise<{
  heartbeatId: string;
  source: string;
  occurredAt: string;
  dueSlotCount: number;
  claimCount: number;
  dispatchCount: number;
  result: Record<string, any>;
} | null> {
  try {
    const rows = await postgrestRequest("scheduler_heartbeats", "GET", {
      params: {
        select: "*",
        order: "occurred_at.desc",
        limit: "1",
      },
    });
    if (!rows || rows.length === 0) return null;
    return {
      heartbeatId: rows[0].heartbeat_id,
      source: rows[0].source,
      occurredAt: rows[0].occurred_at,
      dueSlotCount: rows[0].due_slot_count ?? 0,
      claimCount: rows[0].claim_count ?? 0,
      dispatchCount: rows[0].dispatch_count ?? 0,
      result: rows[0].result ?? {},
    };
  } catch {
    return null;
  }
}

export async function getLatestSchedulerClaimFromSupabase(): Promise<{
  date: string;
  slotId: string;
  claimedAt: string;
  status: string;
} | null> {
  try {
    const rows = await postgrestRequest("scheduler_claims", "GET", {
      params: {
        select: "*",
        order: "updated_at.desc",
        limit: "1",
      },
    });
    if (!rows || rows.length === 0) return null;
    return {
      date: rows[0].berlin_date,
      slotId: rows[0].slot_id,
      claimedAt: rows[0].value?.claimed_at || rows[0].updated_at,
      status: rows[0].status,
    };
  } catch {
    return null;
  }
}

export async function getLatestScheduledRunRecordFromSupabase(): Promise<{
  runId: string;
  berlinDate: string;
  status: string;
  completedAt?: string;
  createdAt: string;
} | null> {
  try {
    const rows = await postgrestRequest("run_records", "GET", {
      params: {
        run_kind: "eq.scheduled",
        select: "*",
        order: "created_at.desc",
        limit: "1",
      },
    });
    if (!rows || rows.length === 0) return null;
    return {
      runId: rows[0].run_id,
      berlinDate: rows[0].berlin_date,
      status: rows[0].status,
      completedAt: rows[0].completed_at,
      createdAt: rows[0].created_at,
    };
  } catch {
    return null;
  }
}
