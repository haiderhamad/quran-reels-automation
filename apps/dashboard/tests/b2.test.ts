import { describe, it, expect, vi } from "vitest";
import {
  buildConfigKey,
  buildCoverageKey,
  buildRunKey,
  buildSchedulerClaimKey,
  buildScheduledRunKey,
  buildScheduledRunLookupKeys,
  getB2ConnectionConfig,
  getStoredConfigState,
  saveStoredConfig,
  sortRunRecordsByStartedAt,
} from "../lib/b2";

describe("Backblaze B2 Key Building & Connection Helpers", () => {
  it("builds canonical config key with prefix", () => {
    expect(buildConfigKey("quran-reels/")).toBe("quran-reels/config/current.json");
    expect(buildConfigKey("custom/prefix/")).toBe("custom/prefix/config/current.json");
    expect(buildConfigKey("")).toBe("config/current.json");
  });

  it("builds canonical coverage key with prefix", () => {
    expect(buildCoverageKey("quran-reels/")).toBe("quran-reels/coverage/current.json");
    expect(buildCoverageKey("custom/prefix/")).toBe("custom/prefix/coverage/current.json");
    expect(buildCoverageKey("")).toBe("coverage/current.json");
  });

  it("builds deterministic run keys for morning, evening, and test slots", () => {
    expect(buildRunKey("quran-reels/", "2026-08-23", "morning")).toBe(
      "quran-reels/runs/2026-08-23/morning.json"
    );
    expect(buildRunKey("quran-reels/", "2026-08-23", "evening")).toBe(
      "quran-reels/runs/2026-08-23/evening.json"
    );
    expect(buildRunKey("quran-reels/", "2026-08-23", "test", "run_test_123")).toBe(
      "quran-reels/runs/test/run_test_123.json"
    );
  });

  it("throws if runId is omitted for test slot", () => {
    expect(() => buildRunKey("quran-reels/", "2026-08-23", "test")).toThrow(
      "runId is required for test run records"
    );
  });

  it("mirrors dynamic scheduled and manual-production Python run keys", () => {
    expect(buildRunKey("quran-reels/", "2026-08-24", "stable-slot", undefined, "scheduled")).toBe(
      "quran-reels/runs/2026-08-24/scheduled/stable-slot.json"
    );
    expect(buildRunKey("quran-reels/", "2026-08-24", "manual-production", "manual_1", "manual_production")).toBe(
      "quran-reels/runs/manual-production/manual_1.json"
    );
    expect(() => buildRunKey("quran-reels/", "2026-08-24", "manual-production")).toThrow();
  });

  it("builds path-safe stable scheduler claim keys", () => {
    expect(buildSchedulerClaimKey("quran-reels/", "2026-08-24", "slot-a1")).toBe(
      "quran-reels/scheduler/claims/2026-08-24/slot-a1.json"
    );
    expect(() => buildSchedulerClaimKey("quran-reels/", "2026-08-24", "../bad")).toThrow();
    expect(buildScheduledRunKey("quran-reels/", "2026-08-24", "slot-a1")).toBe(
      "quran-reels/runs/2026-08-24/scheduled/slot-a1.json"
    );
  });

  it("probes legacy morning/evening run keys after the generic scheduled key", () => {
    expect(buildScheduledRunLookupKeys("quran-reels/", "2026-08-24", "morning")).toEqual([
      "quran-reels/runs/2026-08-24/scheduled/morning.json",
      "quran-reels/runs/2026-08-24/morning.json",
    ]);
    expect(buildScheduledRunLookupKeys("quran-reels/", "2026-08-24", "evening")).toEqual([
      "quran-reels/runs/2026-08-24/scheduled/evening.json",
      "quran-reels/runs/2026-08-24/evening.json",
    ]);
    expect(buildScheduledRunLookupKeys("quran-reels/", "2026-08-24", "stable-slot")).toEqual([
      "quran-reels/runs/2026-08-24/scheduled/stable-slot.json",
    ]);
  });

  it("sorts mixed test, scheduled, and manual records by start time rather than object namespace", () => {
    const records = [
      { started_at: "2026-08-24T08:00:00Z", kind: "test" },
      { started_at: "2026-08-24T20:00:00Z", kind: "manual" },
      { started_at: "2026-08-24T13:00:00Z", kind: "scheduled" },
    ];
    expect(sortRunRecordsByStartedAt(records).map((record) => record.kind)).toEqual(["manual", "scheduled", "test"]);
  });

  it("reads B2 connection configuration from environment", () => {
    const originalEnv = process.env;
    process.env = {
      ...originalEnv,
      B2_ENDPOINT: "https://s3.eu-central-003.backblazeb2.com",
      B2_REGION: "eu-central-003",
      B2_APPLICATION_KEY_ID: "dummy_app_key_id",
      B2_APPLICATION_KEY: "dummy_app_key",
      B2_BUCKET_NAME: "dummy_bucket",
      B2_PREFIX: "custom/",
    };

    const cfg = getB2ConnectionConfig();
    expect(cfg.endpoint).toBe("https://s3.eu-central-003.backblazeb2.com");
    expect(cfg.region).toBe("eu-central-003");
    expect(cfg.applicationKeyId).toBe("dummy_app_key_id");
    expect(cfg.applicationKey).toBe("dummy_app_key");
    expect(cfg.prefix).toBe("custom/");
    expect(cfg.bucketName).toBe("dummy_bucket");

    process.env = originalEnv;
  });

  it("treats a Class-B config read failure as read-only fallback", async () => {
    const originalEnv = process.env;
    process.env = {
      ...originalEnv,
      B2_ENDPOINT: "https://b2.example",
      B2_REGION: "eu-central-003",
      B2_APPLICATION_KEY_ID: "fixture-id",
      B2_APPLICATION_KEY: "fixture-key",
      B2_BUCKET_NAME: "fixture-bucket",
    };
    const client = {
      send: vi.fn(async () => {
        throw new Error("Class B cap exceeded");
      }),
    };
    const state = await getStoredConfigState(client);
    expect(state.authoritative).toBe(false);
    expect(state.writable).toBe(false);
    expect(state.source).toBe("fallback");
    expect(state.errorCode).toBe("B2_CONFIG_UNAVAILABLE_READ_ONLY");
    process.env = originalEnv;
  });

  it("cannot persist fallback defaults over remote config when B2 reads fail", async () => {
    const originalEnv = process.env;
    process.env = {
      ...originalEnv,
      B2_ENDPOINT: "https://b2.example",
      B2_REGION: "eu-central-003",
      B2_APPLICATION_KEY_ID: "fixture-id",
      B2_APPLICATION_KEY: "fixture-key",
      B2_BUCKET_NAME: "fixture-bucket",
    };
    const client = {
      send: vi.fn(async () => {
        throw new Error("Class B cap exceeded");
      }),
    };
    await expect(saveStoredConfig({}, client)).rejects.toThrow(
      "B2_CONFIG_UNAVAILABLE_READ_ONLY",
    );
    expect(client.send).toHaveBeenCalledTimes(1);
    process.env = originalEnv;
  });
});
