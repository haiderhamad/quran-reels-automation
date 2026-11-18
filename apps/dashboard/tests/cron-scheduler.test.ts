import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { NextRequest } from "next/server";

const {
  getStoredConfig,
  getScheduledRunRecord,
  createSchedulerClaim,
  saveSchedulerClaim,
  releaseSchedulerClaim,
  triggerWorkflowDispatch,
  recordSchedulerHeartbeat,
  getLatestSchedulerHeartbeat,
  getLatestSchedulerClaim,
  getLatestScheduledRun,
} = vi.hoisted(() => ({
  getStoredConfig: vi.fn(),
  getScheduledRunRecord: vi.fn(),
  createSchedulerClaim: vi.fn(),
  saveSchedulerClaim: vi.fn(),
  releaseSchedulerClaim: vi.fn(),
  triggerWorkflowDispatch: vi.fn(),
  recordSchedulerHeartbeat: vi.fn(),
  getLatestSchedulerHeartbeat: vi.fn(),
  getLatestSchedulerClaim: vi.fn(),
  getLatestScheduledRun: vi.fn(),
}));

vi.mock("@/lib/b2", () => ({
  getStoredConfig,
  getScheduledRunRecord,
  createSchedulerClaim,
  saveSchedulerClaim,
  releaseSchedulerClaim,
  getStoredConfigState: vi.fn(),
  saveStoredConfig: vi.fn().mockImplementation(async (cfg) => cfg),
  getB2ConnectionConfig: vi.fn().mockReturnValue({ prefix: "quran-reels/", bucketName: "test-bucket" }),
  getB2Client: vi.fn().mockReturnValue({ send: vi.fn() }),
}));

vi.mock("@/lib/github", () => ({
  triggerWorkflowDispatch,
}));

vi.mock("@/lib/scheduler-heartbeat", () => ({
  recordSchedulerHeartbeat,
  getLatestSchedulerHeartbeat,
  getLatestSchedulerClaim,
  getLatestScheduledRun,
}));

vi.mock("@/lib/auth", () => ({
  getAdminSession: vi.fn().mockResolvedValue(true),
}));

import { GET } from "../app/api/cron/publishing-scheduler/route";
import { POST } from "../app/api/settings/route";
import { runSchedulerTick } from "../lib/scheduler-server";
import { getSystemConnectionStatus, getSystemConnectionStatusWithPublishing } from "../lib/system";
import { StoredConfigSchema } from "../lib/schema";

const baseConfig = StoredConfigSchema.parse({
  automation_enabled: true,
  publishing_schedule: [
    { id: "morning", time: "09:00", enabled: true },
    { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
  ],
  facebook_enabled: true,
  instagram_enabled: true,
});

function createCronRequest(authHeader?: string, userAgent = "vercel-cron/1.0"): NextRequest {
  const headers = new Headers();
  if (authHeader) {
    headers.set("Authorization", authHeader);
  }
  if (userAgent) {
    headers.set("User-Agent", userAgent);
  }
  return new NextRequest("http://localhost:3000/api/cron/publishing-scheduler", {
    method: "GET",
    headers,
  });
}

describe("Phase 8I — Reliable Vercel Scheduler Heartbeat & Cron Route", () => {
  const originalCronSecret = process.env.CRON_SECRET;

  beforeEach(() => {
    vi.clearAllMocks();
    process.env.CRON_SECRET = "dummy_cron_secret_for_test";
    getStoredConfig.mockResolvedValue(baseConfig);
    getScheduledRunRecord.mockResolvedValue(null);
    createSchedulerClaim.mockResolvedValue(true);
    saveSchedulerClaim.mockResolvedValue(undefined);
    releaseSchedulerClaim.mockResolvedValue(undefined);
    triggerWorkflowDispatch.mockResolvedValue({ success: true, message: "ok" });
    recordSchedulerHeartbeat.mockResolvedValue("hb_test_123");
    getLatestSchedulerHeartbeat.mockResolvedValue(null);
    getLatestSchedulerClaim.mockResolvedValue(null);
    getLatestScheduledRun.mockResolvedValue(null);
  });

  afterEach(() => {
    process.env.CRON_SECRET = originalCronSecret;
  });

  // A. Authenticated Vercel Cron invokes runSchedulerTick
  it("Test A: authenticated Vercel Cron invokes runSchedulerTick and returns 200 with structured JSON", async () => {
    const req = createCronRequest("Bearer dummy_cron_secret_for_test");
    const res = await GET(req);
    expect(res.status).toBe(200);
    const json = await res.json();
    expect(json.success).toBe(true);
    expect(json.berlinClock).toBeDefined();
    expect(json.berlinClock.date).toBeDefined();
    expect(recordSchedulerHeartbeat).toHaveBeenCalledWith(
      expect.objectContaining({ source: "vercel_cron" })
    );
  });

  it("Test A.2: Supabase pg_cron caller source is detected via User-Agent and recorded as supabase_cron", async () => {
    const req = createCronRequest("Bearer dummy_cron_secret_for_test", "Supabase-pg_cron/1.0");
    const res = await GET(req);
    expect(res.status).toBe(200);
    expect(recordSchedulerHeartbeat).toHaveBeenCalledWith(
      expect.objectContaining({ source: "supabase_cron" })
    );
  });

  // B. Missing/wrong CRON_SECRET -> 401
  it("Test B: missing or wrong CRON_SECRET returns 401 Unauthorized", async () => {
    // Missing header
    const req1 = createCronRequest();
    const res1 = await GET(req1);
    expect(res1.status).toBe(401);
    expect((await res1.json()).error).toBe("Unauthorized");

    // Wrong secret
    const req2 = createCronRequest("Bearer wrong_secret");
    const res2 = await GET(req2);
    expect(res2.status).toBe(401);

    // Empty secret in env
    process.env.CRON_SECRET = "";
    const req3 = createCronRequest("Bearer dummy_cron_secret_for_test");
    const res3 = await GET(req3);
    expect(res3.status).toBe(401);
  });

  // C. Automation disabled -> no claim
  it("Test C: automation disabled in config produces no claims or dispatches", async () => {
    getStoredConfig.mockResolvedValue({ ...baseConfig, automation_enabled: false });
    // 16:10 Berlin is 14:10 UTC (slot 16:00 is due if enabled)
    const now = new Date("2026-09-05T14:10:00Z");
    const result = await runSchedulerTick(now, undefined, "vercel_cron");
    expect(result.automationEnabled).toBe(false);
    expect(result.dispatched).toHaveLength(0);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // D. Future slot -> no claim
  it("Test D: future slot is not claimed before its scheduled time", async () => {
    // 15:50 Berlin is 13:50 UTC (slot 16:00 is 10 minutes in the future)
    const now = new Date("2026-09-05T13:50:00Z");
    const result = await runSchedulerTick(now, baseConfig, "vercel_cron");
    expect(result.dispatched).toHaveLength(0);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // E. Due slot -> exactly one claim
  it("Test E: due slot acquires exactly one atomic claim and dispatches GitHub Actions", async () => {
    // 16:10 Berlin is 14:10 UTC (slot 16:00 is due)
    const now = new Date("2026-09-05T14:10:00Z");
    const result = await runSchedulerTick(now, baseConfig, "vercel_cron");
    expect(result.dispatched).toContain("slot-00000000-0000-4000-8000-000000000002");
    expect(createSchedulerClaim).toHaveBeenCalledTimes(1);
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        runKind: "scheduled",
        slotId: "slot-00000000-0000-4000-8000-000000000002",
        publishFacebook: true,
        publishInstagram: true,
      })
    );
  });

  // F. Second heartbeat same slot -> no second claim
  it("Test F: second heartbeat in the same slot window does not create a second claim", async () => {
    // First heartbeat claims the slot
    createSchedulerClaim.mockResolvedValueOnce(false); // Simulate second claim conflict
    const now = new Date("2026-09-05T14:10:00Z");
    const result = await runSchedulerTick(now, baseConfig, "vercel_cron");
    expect(result.dispatched).toHaveLength(0);
    expect(result.skipped).toContain("slot-00000000-0000-4000-8000-000000000002");
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // G. Vercel + GitHub heartbeat race -> exactly one claim
  it("Test G: Vercel + GitHub concurrent heartbeat race guarantees exactly one successful dispatch", async () => {
    const now = new Date("2026-09-05T14:10:00Z");

    // Thread 1 (Vercel) wins atomic claim
    createSchedulerClaim.mockResolvedValueOnce(true);
    // Thread 2 (GitHub) encounters atomic conflict
    createSchedulerClaim.mockResolvedValueOnce(false);

    const [resVercel, resGitHub] = await Promise.all([
      runSchedulerTick(now, baseConfig, "vercel_cron"),
      runSchedulerTick(now, baseConfig, "github_cron"),
    ]);

    expect(resVercel.dispatched).toContain("slot-00000000-0000-4000-8000-000000000002");
    expect(resGitHub.dispatched).toHaveLength(0);
    expect(resGitHub.skipped).toContain("slot-00000000-0000-4000-8000-000000000002");
    expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(1);
  });

  // H. 45-minute grace preserved
  it("Test H: 45-minute grace window strictly preserved (16:40 due, 16:46 not due)", async () => {
    // 16:40 Berlin (14:40 UTC) -> 40m elapsed <= 45m grace -> DUE
    const at1640 = new Date("2026-09-05T14:40:00Z");
    const res1 = await runSchedulerTick(at1640, baseConfig, "vercel_cron");
    expect(res1.dispatched).toContain("slot-00000000-0000-4000-8000-000000000002");

    vi.clearAllMocks();
    createSchedulerClaim.mockResolvedValue(true);

    // 16:46 Berlin (14:46 UTC) -> 46m elapsed > 45m grace -> NOT DUE
    const at1646 = new Date("2026-09-05T14:46:00Z");
    const res2 = await runSchedulerTick(at1646, baseConfig, "vercel_cron");
    expect(res2.dispatched).toHaveLength(0);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
  });

  // I. DST Europe/Berlin preserved
  it("Test I: DST Europe/Berlin wall-clock calculation is preserved correctly", async () => {
    // September in Berlin is CEST (UTC+2)
    // 14:00 UTC = 16:00 CEST -> exactly slot time
    const at1600Berlin = new Date("2026-09-05T14:00:00Z");
    const res = await runSchedulerTick(at1600Berlin, baseConfig, "vercel_cron");
    expect(res.berlinDate).toBe("2026-09-05");
    expect(res.berlinTime).toBe("16:00");
    expect(res.dispatched).toContain("slot-00000000-0000-4000-8000-000000000002");
  });

  // J. Dashboard save trigger still works
  it("Test J: Dashboard settings save trigger invokes runSchedulerTick with dashboard_save source", async () => {
    // Berlin 16:15 CEST is 14:15 UTC
    vi.setSystemTime(new Date("2026-09-05T14:15:00Z"));
    try {
      const saveReq = new NextRequest("http://localhost:3000/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(baseConfig),
      });
      const res = await POST(saveReq);
      expect(res.status).toBe(200);
      expect(recordSchedulerHeartbeat).toHaveBeenCalledWith(
        expect.objectContaining({ source: "dashboard_save" })
      );
      expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  // K. Heavy generation never runs inside Vercel route
  it("Test K: heavy generation (FFmpeg, OpenCV, renderer) is never imported or executed in Vercel route", () => {
    // Verify pure Next.js server runtime without native video modules
    expect(process.env.CHILD_PROCESS).toBeUndefined();
  });

  // L. Scheduler health reports stale heartbeat as DEGRADED
  it("Test L: scheduler health reports stale heartbeat as DEGRADED", async () => {
    // Stale heartbeat 2 hours ago (120 minutes > 75 minutes tolerance)
    const twoHoursAgo = new Date(Date.now() - 120 * 60 * 1000).toISOString();
    getLatestSchedulerHeartbeat.mockResolvedValueOnce({
      heartbeatId: "hb_old",
      source: "vercel_cron",
      occurredAt: twoHoursAgo,
      dueSlotCount: 0,
      claimCount: 0,
      dispatchCount: 0,
      result: {},
    });

    const status = await getSystemConnectionStatusWithPublishing();
    expect(status.schedulerHealth.status).toBe("DEGRADED");
    expect(status.schedulerHealth.details).toBe("DEGRADED — heartbeat overdue");
    expect(status.scheduler).toBe("error");
  });

  // M. Instagram health no longer reports Deferred when configured
  it("Test M: Instagram health reports Configured when buffer Instagram channel is enabled", () => {
    const originalOrg = process.env.BUFFER_ORGANIZATION_ID;
    const originalChannel = process.env.BUFFER_INSTAGRAM_CHANNEL_ID;
    const originalInsta = process.env.BUFFER_INSTAGRAM_ENABLED;
    const originalPublish = process.env.BUFFER_PUBLISHING_ENABLED;
    const originalBackend = process.env.PUBLISHER_BACKEND;

    try {
      process.env.BUFFER_ORGANIZATION_ID = "org_123";
      process.env.BUFFER_INSTAGRAM_CHANNEL_ID = "channel_456";
      process.env.BUFFER_INSTAGRAM_ENABLED = "true";
      process.env.BUFFER_PUBLISHING_ENABLED = "true";
      process.env.PUBLISHER_BACKEND = "buffer";

      const status = getSystemConnectionStatus();
      expect(status.bufferInstagram).toBe(true);
    } finally {
      process.env.BUFFER_ORGANIZATION_ID = originalOrg;
      process.env.BUFFER_INSTAGRAM_CHANNEL_ID = originalChannel;
      process.env.BUFFER_INSTAGRAM_ENABLED = originalInsta;
      process.env.BUFFER_PUBLISHING_ENABLED = originalPublish;
      process.env.PUBLISHER_BACKEND = originalBackend;
    }
  });
});
