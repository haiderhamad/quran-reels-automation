import { afterEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  isLocalBuilderEnvironmentEnabled,
  localBuilderRequestAllowed,
  publicLocalBuilderStatus,
  readLocalBuilderStatus,
  reconcileLocalBuilderStatus,
  refreshLocalPoolTotals,
  startLocalBuilder,
  stopLocalBuilder,
} from "../lib/local-background-pool-builder";

const tempRoots: string[] = [];

function makeRoot(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pool-dashboard-"));
  tempRoots.push(root);
  fs.mkdirSync(path.join(root, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(root, "scripts", "build_verified_background_pool.py"), "# fixture\n");
  fs.writeFileSync(
    path.join(root, "scripts", "refresh_background_pool_stats.py"),
    "import json,sys\np=sys.argv[sys.argv.index('--status-file')+1]\njson.dump({'status_schema_version':3,'state':'idle','stats':{'verified_current_policy':6},'b2_operations':{'list_operations':8,'metadata_reads':12,'writes':1}},open(p,'w'))\n",
  );
  return root;
}

function localEnv(): NodeJS.ProcessEnv {
  const fixtureValue = (name: string) => ["fixture", name].join("-");
  return {
    NODE_ENV: "test",
    LOCAL_BACKGROUND_POOL_BUILDER_ENABLED: "true",
    PATH: process.env.PATH,
    PEXELS_API_KEY: fixtureValue("pexels"),
    PIXABAY_API_KEY: fixtureValue("pixabay"),
    B2_ENDPOINT: "https://b2.example",
    B2_REGION: "eu-central-003",
    B2_APPLICATION_KEY_ID: fixtureValue("key-id"),
    B2_APPLICATION_KEY: fixtureValue("application-key"),
    B2_BUCKET_NAME: "bucket",
  };
}

afterEach(() => {
  for (const root of tempRoots.splice(0)) fs.rmSync(root, { recursive: true, force: true });
});

describe("local background pool builder guard", () => {
  it("requires explicit enablement and rejects every Vercel runtime", () => {
    expect(isLocalBuilderEnvironmentEnabled({ NODE_ENV: "test" })).toBe(false);
    expect(isLocalBuilderEnvironmentEnabled(localEnv())).toBe(true);
    expect(isLocalBuilderEnvironmentEnabled({ ...localEnv(), VERCEL: "1" })).toBe(false);
    expect(isLocalBuilderEnvironmentEnabled({ ...localEnv(), VERCEL_ENV: "production" })).toBe(false);
  });

  it("accepts loopback only and rejects remote hosts", () => {
    expect(localBuilderRequestAllowed("localhost", localEnv())).toBe(true);
    expect(localBuilderRequestAllowed("127.0.0.1", localEnv())).toBe(true);
    expect(localBuilderRequestAllowed("dashboard.example.com", localEnv())).toBe(false);
  });

  it("strictly filters secrets, pid, paths, and raw log fields from public status", () => {
    const privateFixture = ["private", "fixture"].join("-");
    const status = publicLocalBuilderStatus({
      state: "scanning", pid: 999, secret: privateFixture, source_url: `https://${privateFixture}`,
      status_schema_version: 2, provider: "both", target_new_safe: 20, found_safe: 6,
      discovered: 42, metadata_skipped: 20, downloads: 22, safety_scanned: 14,
      safety_rejected: 8, transient_failures: 1, elapsed_seconds: 120,
      policy_fingerprint: "abc", current_candidate: { provider: "pexels", query: "ocean waves", asset_id: "123" },
      stats: { verified_current_policy: 17, hidden: privateFixture },
      b2_operations: { list_operations: 8, metadata_reads: 12, writes: 20 },
    });
    expect(status.foundSafe).toBe(6);
    expect(status.currentCandidate?.assetId).toBe("123");
    expect(status.stats).toEqual({ verified_current_policy: 17 });
    expect(status.b2Operations).toEqual({ listOperations: 8, metadataReads: 12, writes: 20 });
    expect(status).not.toHaveProperty("pid");
    expect(JSON.stringify(status)).not.toContain(privateFixture);
  });

  it("reports missing config names without any secret values", () => {
    expect(() => startLocalBuilder(20, "both", { NODE_ENV: "test", LOCAL_BACKGROUND_POOL_BUILDER_ENABLED: "true" }, makeRoot()))
      .toThrow(/LOCAL_POOL_CONFIG_MISSING:B2_ENDPOINT/);
  });

  it("rejects duplicate start while the recorded local process is active", () => {
    const root = makeRoot();
    const directory = path.join(root, ".local", "background-pool-builder");
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, "status.json"), JSON.stringify({ state: "scanning", pid: process.pid }));
    expect(() => startLocalBuilder(20, "both", localEnv(), root, true, () => true))
      .toThrow("LOCAL_POOL_BUILDER_ALREADY_RUNNING");
  });

  it("reconciles stale active state, preserves counters, and removes stale process controls", () => {
    const root = makeRoot();
    const directory = path.join(root, ".local", "background-pool-builder");
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, "status.json"), JSON.stringify({
      status_schema_version: 3,
      state: "stopping",
      pid: 999_999_999,
      provider: "pexels",
      target_new_safe: 50,
      found_safe: 6,
      discovered: 165,
      metadata_skipped: 99,
      downloads: 50,
      safety_scanned: 50,
      safety_rejected: 41,
      transient_failures: 0,
      stats: { verified_current_policy: 6, safety_rejected: 41 },
      current_candidate: { provider: "pexels", query: "ocean waves", asset_id: "123" },
    }));
    fs.writeFileSync(path.join(directory, "builder.lock"), "999999999\n");
    fs.writeFileSync(path.join(directory, "stop.request"), "stop\n");

    const status = reconcileLocalBuilderStatus(root, () => false);

    expect(status.state).toBe("stopped");
    expect(status.errorCode).toBe("LOCAL_POOL_PROCESS_INTERRUPTED");
    expect(status.targetNewSafe).toBe(50);
    expect(status.discovered).toBe(165);
    expect(status.foundSafe).toBe(6);
    expect(status.safetyScanned).toBe(50);
    expect(status.safetyRejected).toBe(41);
    expect(status.stats).toEqual({ verified_current_policy: 6 });
    expect(status.currentCandidate).toBeNull();
    expect(fs.existsSync(path.join(directory, "builder.lock"))).toBe(false);
    expect(fs.existsSync(path.join(directory, "stop.request"))).toBe(false);
    const persisted = JSON.parse(fs.readFileSync(path.join(directory, "status.json"), "utf8"));
    expect(persisted.pid).toBeUndefined();
    expect(persisted.discovered).toBe(165);
    expect(persisted.found_safe).toBe(6);
    expect(persisted.stats).toEqual({ verified_current_policy: 6, safety_rejected: 41 });
  });

  it("keeps an active status when the PID belongs to the matching builder", () => {
    const root = makeRoot();
    const directory = path.join(root, ".local", "background-pool-builder");
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, "status.json"), JSON.stringify({
      state: "scanning", pid: 12345, discovered: 7,
    }));
    fs.writeFileSync(path.join(directory, "builder.lock"), "12345\n");

    const status = reconcileLocalBuilderStatus(root, () => true);

    expect(status.state).toBe("scanning");
    expect(status.discovered).toBe(7);
    expect(fs.existsSync(path.join(directory, "builder.lock"))).toBe(true);
  });

  it("cleans stale process state but still blocks Start when StoredConfig is not authoritative", () => {
    const root = makeRoot();
    const directory = path.join(root, ".local", "background-pool-builder");
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, "status.json"), JSON.stringify({
      state: "stopping", pid: 999_999_999, target_new_safe: 50, discovered: 165, found_safe: 6,
    }));
    fs.writeFileSync(path.join(directory, "builder.lock"), "999999999\n");

    expect(() => startLocalBuilder(5, "both", localEnv(), root, false, () => false))
      .toThrow("B2_CONFIG_UNAVAILABLE_READ_ONLY");
    const persisted = JSON.parse(fs.readFileSync(path.join(directory, "status.json"), "utf8"));
    expect(persisted.state).toBe("stopped");
    expect(persisted.discovered).toBe(165);
    expect(persisted.found_safe).toBe(6);
    expect(persisted.pid).toBeUndefined();
    expect(fs.existsSync(path.join(directory, "builder.lock"))).toBe(false);
  });
  it("automatic progress reads stay local while manual totals refresh is explicit", async () => {
    const root = makeRoot();
    const directory = path.join(root, ".local", "background-pool-builder");
    fs.mkdirSync(directory, { recursive: true });
    const statusPath = path.join(directory, "status.json");
    fs.writeFileSync(statusPath, JSON.stringify({ state: "idle", stats: { verified_current_policy: 2 } }));
    const before = fs.readFileSync(statusPath, "utf8");
    expect(readLocalBuilderStatus(root).stats.verified_current_policy).toBe(2);
    expect(fs.readFileSync(statusPath, "utf8")).toBe(before);

    const refreshed = await refreshLocalPoolTotals(localEnv(), root);
    expect(refreshed.stats.verified_current_policy).toBe(6);
    expect(refreshed.b2Operations.listOperations).toBe(8);
  });

  it("Stop writes a fail-safe request and readable stopping progress", () => {
    const root = makeRoot();
    const status = stopLocalBuilder(root);
    expect(status.state).toBe("idle");
    expect(fs.existsSync(path.join(root, ".local", "background-pool-builder", "stop.request"))).toBe(true);
    expect(status.message).toMatch(/fail closed/i);
  });
});
