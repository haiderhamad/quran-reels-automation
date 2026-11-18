import "server-only";

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

export const LOCAL_POOL_ALLOWED_TARGETS = [5, 10, 20, 50] as const;
export const LOCAL_POOL_ALLOWED_PROVIDERS = ["both", "pexels", "pixabay"] as const;
export type LocalPoolProvider = typeof LOCAL_POOL_ALLOWED_PROVIDERS[number];

const ACTIVE_STATES = new Set(["starting", "scanning", "stopping"]);
const PUBLIC_STATES = new Set([
  "idle", "starting", "scanning", "stopping", "stopped", "completed",
  "candidate_exhausted", "time_budget_exhausted", "failed", "technical_error",
]);
const REQUIRED_B2_NAMES = [
  "B2_ENDPOINT",
  "B2_REGION",
  "B2_APPLICATION_KEY_ID",
  "B2_APPLICATION_KEY",
  "B2_BUCKET_NAME",
] as const;
const PUBLIC_STAT_KEYS = [
  "verified_current_policy",
  "verified_stale_policy",
  "known_safety_rejected",
  "metadata_ineligible",
  "transient_failures",
  "production_used_unique",
  "production_unused",
  "missing_or_stale",
  "pexels_verified_current_policy",
  "pixabay_verified_current_policy",
] as const;

export type LocalPoolProviderQueryMetric = {
  provider: Exclude<LocalPoolProvider, "both">;
  query: string;
  scanned: number;
  safe: number;
};

export type LocalPoolPublicStatus = {
  statusSchemaVersion: number;
  state: string;
  builderRunId: string | null;
  provider: LocalPoolProvider;
  targetNewSafe: number;
  foundSafe: number;
  discovered: number;
  metadataSkipped: number;
  downloads: number;
  safetyScanned: number;
  safetyRejected: number;
  transientFailures: number;
  currentCandidate: {
    provider: Exclude<LocalPoolProvider, "both">;
    query: string | null;
    assetId: string;
  } | null;
  elapsedSeconds: number;
  policyFingerprint: string | null;
  stats: Record<string, number>;
  providerQueryMetrics: LocalPoolProviderQueryMetric[];
  b2Operations: {
    listOperations: number;
    metadataReads: number;
    writes: number;
  };
  message: string | null;
  errorCode: string | null;
  updatedAt: string | null;
};

type InternalStatus = Record<string, unknown> & { pid?: number; state?: string };
type BuilderProcessMatcher = (pid: number | undefined, scriptPath: string) => boolean;

export function isLocalBuilderEnvironmentEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.LOCAL_BACKGROUND_POOL_BUILDER_ENABLED === "true"
    && !env.VERCEL
    && !env.VERCEL_ENV
    && !env.VERCEL_URL
    && env.GITHUB_ACTIONS !== "true";
}

export function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.trim().toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}

export function localBuilderRequestAllowed(
  hostname: string,
  env: NodeJS.ProcessEnv = process.env,
  relatedHostnames: readonly string[] = [],
): boolean {
  return isLocalBuilderEnvironmentEnabled(env)
    && [hostname, ...relatedHostnames].every(isLoopbackHostname);
}

function repositoryRoot(): string {
  let current = path.resolve(process.cwd());
  for (let i = 0; i < 5; i += 1) {
    if (fs.existsSync(path.join(current, "scripts", "build_verified_background_pool.py"))) return current;
    current = path.dirname(current);
  }
  throw new Error("LOCAL_POOL_REPOSITORY_NOT_FOUND");
}

export function localBuilderPaths(root = repositoryRoot()) {
  const directory = path.join(root, ".local", "background-pool-builder");
  return {
    root,
    directory,
    status: path.join(directory, "status.json"),
    stop: path.join(directory, "stop.request"),
    lock: path.join(directory, "builder.lock"),
    log: path.join(directory, "builder.log"),
    script: path.join(root, "scripts", "build_verified_background_pool.py"),
    refreshScript: path.join(root, "scripts", "refresh_background_pool_stats.py"),
  };
}

function ensureLocalDirectory(directory: string): void {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  try { fs.chmodSync(directory, 0o700); } catch { /* best effort on non-POSIX dev systems */ }
}

function readInternalStatus(statusPath: string): InternalStatus | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(statusPath, "utf8"));
    return parsed && typeof parsed === "object" ? parsed as InternalStatus : null;
  } catch {
    return null;
  }
}

function writeInternalStatus(statusPath: string, status: InternalStatus): void {
  const directory = path.dirname(statusPath);
  ensureLocalDirectory(directory);
  const temporary = path.join(
    directory,
    `.${path.basename(statusPath)}.${process.pid}.tmp`,
  );
  fs.writeFileSync(temporary, JSON.stringify(status, null, 2), { mode: 0o600 });
  fs.renameSync(temporary, statusPath);
}

function asCounter(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.floor(value))
    : 0;
}

function asNonNegativeNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
}

function asBoundedString(value: unknown, maxLength = 240): string | null {
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return cleaned ? cleaned.slice(0, maxLength) : null;
}

function asProvider(value: unknown, fallback: LocalPoolProvider = "both"): LocalPoolProvider {
  return LOCAL_POOL_ALLOWED_PROVIDERS.includes(value as LocalPoolProvider)
    ? value as LocalPoolProvider
    : fallback;
}

function asCandidateProvider(value: unknown): Exclude<LocalPoolProvider, "both"> | null {
  return value === "pexels" || value === "pixabay" ? value : null;
}

function publicProviderQueryMetrics(value: unknown): LocalPoolProviderQueryMetric[] {
  if (!Array.isArray(value)) return [];
  const metrics: LocalPoolProviderQueryMetric[] = [];
  for (const item of value.slice(0, 200)) {
    if (!item || typeof item !== "object") continue;
    const raw = item as Record<string, unknown>;
    const provider = asCandidateProvider(raw.provider);
    const query = asBoundedString(raw.query, 100);
    if (!provider || !query) continue;
    metrics.push({
      provider,
      query,
      scanned: asCounter(raw.scanned ?? raw.safety_scanned),
      safe: asCounter(raw.safe ?? raw.found_safe),
    });
  }
  return metrics;
}

export function publicLocalBuilderStatus(raw: InternalStatus | null): LocalPoolPublicStatus {
  const current = raw?.current_candidate;
  let currentCandidate: LocalPoolPublicStatus["currentCandidate"] = null;
  if (current && typeof current === "object") {
    const currentRaw = current as Record<string, unknown>;
    const provider = asCandidateProvider(currentRaw.provider);
    const assetId = asBoundedString(currentRaw.asset_id, 160);
    if (provider && assetId && /^[a-zA-Z0-9._-]+$/.test(assetId)) {
      currentCandidate = {
        provider,
        query: asBoundedString(currentRaw.query, 100),
        assetId,
      };
    }
  }
  const statsRaw = raw?.stats && typeof raw.stats === "object" ? raw.stats as Record<string, unknown> : {};
  const stats: Record<string, number> = {};
  for (const key of PUBLIC_STAT_KEYS) {
    const value = statsRaw[key];
    if (typeof value === "number" && Number.isFinite(value)) stats[key] = Math.max(0, value);
  }
  const rawState = asBoundedString(raw?.state, 40) || "idle";
  const rawErrorCode = asBoundedString(raw?.error_code, 120);
  const operations = raw?.b2_operations && typeof raw.b2_operations === "object"
    ? raw.b2_operations as Record<string, unknown>
    : {};
  return {
    statusSchemaVersion: asCounter(raw?.status_schema_version),
    state: PUBLIC_STATES.has(rawState) ? rawState : "unknown",
    builderRunId: asBoundedString(raw?.builder_run_id, 160),
    provider: asProvider(raw?.provider),
    targetNewSafe: asCounter(raw?.target_new_safe),
    foundSafe: asCounter(raw?.found_safe),
    discovered: asCounter(raw?.discovered),
    metadataSkipped: asCounter(raw?.metadata_skipped ?? raw?.metadata_ineligible),
    downloads: asCounter(raw?.downloads),
    safetyScanned: asCounter(raw?.safety_scanned),
    safetyRejected: asCounter(raw?.safety_rejected),
    transientFailures: asCounter(raw?.transient_failures),
    currentCandidate,
    elapsedSeconds: asNonNegativeNumber(raw?.elapsed_seconds),
    policyFingerprint: asBoundedString(raw?.policy_fingerprint, 128),
    stats,
    providerQueryMetrics: publicProviderQueryMetrics(raw?.provider_query_metrics),
    b2Operations: {
      listOperations: asCounter(operations.list_operations),
      metadataReads: asCounter(operations.metadata_reads),
      writes: asCounter(operations.writes),
    },
    message: asBoundedString(raw?.message),
    errorCode: rawErrorCode && /^[A-Z0-9_:,.-]+$/.test(rawErrorCode) ? rawErrorCode : null,
    updatedAt: asBoundedString(raw?.updated_at, 64),
  };
}

function missingConfigNames(provider: LocalPoolProvider, env: NodeJS.ProcessEnv): string[] {
  const requiredProviderNames = provider === "pexels"
    ? ["PEXELS_API_KEY"]
    : provider === "pixabay"
    ? ["PIXABAY_API_KEY"]
    : ["PEXELS_API_KEY", "PIXABAY_API_KEY"];
  return [...REQUIRED_B2_NAMES, ...requiredProviderNames].filter((name) => !env[name]?.trim());
}

function processIsAlive(pid: number | undefined): boolean {
  if (!pid || !Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function matchingBuilderProcessIsAlive(
  pid: number | undefined,
  scriptPath: string,
): boolean {
  if (!processIsAlive(pid)) return false;
  if (!pid) return false;
  let command = "";
  try {
    command = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8").replace(/\0/g, " ");
  } catch {
    const result = spawnSync("ps", ["-p", String(pid), "-o", "command="], {
      encoding: "utf8",
      timeout: 2_000,
    });
    if (result.status === 0) command = result.stdout || "";
  }
  if (!command.trim()) {
    // Fail closed against concurrent builders if the host cannot inspect a
    // known-live process. macOS and GitHub Linux both provide ps or /proc.
    return true;
  }
  return command.includes(path.resolve(scriptPath));
}

export function reconcileLocalBuilderStatus(
  root?: string,
  processMatcher: BuilderProcessMatcher = matchingBuilderProcessIsAlive,
): LocalPoolPublicStatus {
  const paths = localBuilderPaths(root);
  const existing = readInternalStatus(paths.status);
  if (!existing || !ACTIVE_STATES.has(String(existing.state))) {
    return publicLocalBuilderStatus(existing);
  }
  if (processMatcher(existing.pid, paths.script)) {
    return publicLocalBuilderStatus(existing);
  }
  const interrupted: InternalStatus = {
    ...existing,
    state: "stopped",
    current_candidate: null,
    message: "Previous local builder process ended unexpectedly; durable candidate records and historical counters were preserved.",
    error_code: "LOCAL_POOL_PROCESS_INTERRUPTED",
    updated_at: new Date().toISOString(),
    pid: undefined,
  };
  writeInternalStatus(paths.status, interrupted);
  for (const stalePath of [paths.lock, paths.stop]) {
    try { fs.unlinkSync(stalePath); } catch { /* absent is expected */ }
  }
  return publicLocalBuilderStatus(interrupted);
}

export function readLocalBuilderStatus(root?: string): LocalPoolPublicStatus {
  return reconcileLocalBuilderStatus(root);
}

export function startLocalBuilder(
  addSafe: number,
  provider: LocalPoolProvider = "both",
  env: NodeJS.ProcessEnv = process.env,
  root?: string,
  authoritativeConfigAvailable = true,
  processMatcher: BuilderProcessMatcher = matchingBuilderProcessIsAlive,
): LocalPoolPublicStatus {
  const paths = localBuilderPaths(root);
  ensureLocalDirectory(paths.directory);
  const reconciled = reconcileLocalBuilderStatus(paths.root, processMatcher);
  if (!authoritativeConfigAvailable) {
    throw new Error("B2_CONFIG_UNAVAILABLE_READ_ONLY");
  }
  if (!LOCAL_POOL_ALLOWED_TARGETS.includes(addSafe as typeof LOCAL_POOL_ALLOWED_TARGETS[number])) {
    throw new Error("LOCAL_POOL_INVALID_TARGET");
  }
  if (!LOCAL_POOL_ALLOWED_PROVIDERS.includes(provider)) {
    throw new Error("LOCAL_POOL_INVALID_PROVIDER");
  }
  const missing = missingConfigNames(provider, env);
  if (missing.length) throw new Error(`LOCAL_POOL_CONFIG_MISSING:${missing.join(",")}`);
  if (ACTIVE_STATES.has(reconciled.state)) {
    throw new Error("LOCAL_POOL_BUILDER_ALREADY_RUNNING");
  }
  try { fs.unlinkSync(paths.stop); } catch { /* absent is expected */ }
  const logFd = fs.openSync(paths.log, "a", 0o600);
  const childEnv: NodeJS.ProcessEnv = {
    NODE_ENV: env.NODE_ENV,
    PATH: env.PATH,
    TMPDIR: env.TMPDIR,
    PYTHONUNBUFFERED: "1",
    B2_ENDPOINT: env.B2_ENDPOINT,
    B2_REGION: env.B2_REGION,
    B2_APPLICATION_KEY_ID: env.B2_APPLICATION_KEY_ID,
    B2_APPLICATION_KEY: env.B2_APPLICATION_KEY,
    B2_BUCKET_NAME: env.B2_BUCKET_NAME,
    B2_PREFIX: env.B2_PREFIX || "quran-reels/",
  };
  if (provider === "both" || provider === "pexels") childEnv.PEXELS_API_KEY = env.PEXELS_API_KEY;
  if (provider === "both" || provider === "pixabay") childEnv.PIXABAY_API_KEY = env.PIXABAY_API_KEY;
  const child = spawn("python3", [
    "-u", paths.script,
    "--add-safe", String(addSafe),
    "--provider", provider,
    "--status-file", paths.status,
    "--stop-file", paths.stop,
    "--lock-file", paths.lock,
  ], {
    cwd: paths.root,
    env: childEnv,
    detached: true,
    shell: false,
    stdio: ["ignore", logFd, logFd],
  });
  const starting: InternalStatus = {
    status_schema_version: 3,
    state: "starting",
    builder_run_id: null,
    provider,
    target_new_safe: addSafe,
    found_safe: 0,
    discovered: 0,
    metadata_skipped: 0,
    downloads: 0,
    safety_scanned: 0,
    safety_rejected: 0,
    transient_failures: 0,
    elapsed_seconds: 0,
    stats: {},
    provider_query_metrics: [],
    b2_operations: { list_operations: 0, metadata_reads: 0, writes: 0 },
    message: "Local builder process started.",
    updated_at: new Date().toISOString(),
    pid: child.pid,
  };
  writeInternalStatus(paths.status, starting);
  child.unref();
  fs.closeSync(logFd);
  return publicLocalBuilderStatus(starting);
}

export async function refreshLocalPoolTotals(
  env: NodeJS.ProcessEnv = process.env,
  root?: string,
): Promise<LocalPoolPublicStatus> {
  const missing = [...REQUIRED_B2_NAMES].filter((name) => !env[name]?.trim());
  if (missing.length) throw new Error(`LOCAL_POOL_CONFIG_MISSING:${missing.join(",")}`);
  const paths = localBuilderPaths(root);
  ensureLocalDirectory(paths.directory);
  const existing = reconcileLocalBuilderStatus(paths.root);
  if (ACTIVE_STATES.has(existing.state)) {
    throw new Error("LOCAL_POOL_REFRESH_WHILE_RUNNING");
  }
  const childEnv: NodeJS.ProcessEnv = {
    NODE_ENV: env.NODE_ENV,
    PATH: env.PATH,
    TMPDIR: env.TMPDIR,
    PYTHONUNBUFFERED: "1",
    B2_ENDPOINT: env.B2_ENDPOINT,
    B2_REGION: env.B2_REGION,
    B2_APPLICATION_KEY_ID: env.B2_APPLICATION_KEY_ID,
    B2_APPLICATION_KEY: env.B2_APPLICATION_KEY,
    B2_BUCKET_NAME: env.B2_BUCKET_NAME,
    B2_PREFIX: env.B2_PREFIX || "quran-reels/",
  };
  await new Promise<void>((resolve, reject) => {
    const child = spawn("python3", [
      "-u", paths.refreshScript, "--status-file", paths.status,
    ], {
      cwd: paths.root,
      env: childEnv,
      shell: false,
      stdio: ["ignore", "ignore", "ignore"],
    });
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("LOCAL_POOL_REFRESH_TIMEOUT"));
    }, 120_000);
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error("LOCAL_POOL_REFRESH_FAILED"));
    });
  });
  return readLocalBuilderStatus(paths.root);
}

export function stopLocalBuilder(root?: string): LocalPoolPublicStatus {
  const paths = localBuilderPaths(root);
  ensureLocalDirectory(paths.directory);
  fs.writeFileSync(paths.stop, new Date().toISOString(), { mode: 0o600 });
  const status = readInternalStatus(paths.status) || {};
  const stopping = {
    ...status,
    state: ACTIVE_STATES.has(String(status.state)) ? "stopping" : String(status.state || "idle"),
    message: "Stop requested; the current candidate will fail closed and temporary media will be cleaned.",
    updated_at: new Date().toISOString(),
  };
  writeInternalStatus(paths.status, stopping);
  return publicLocalBuilderStatus(stopping);
}
