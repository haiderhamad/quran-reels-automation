import { describe, it, expect, beforeEach, vi } from "vitest";
import { NextRequest } from "next/server";

const {
  getAdminSession,
  saveStoredConfig,
  getStoredConfig,
  getStoredConfigState,
  getScheduledRunRecord,
  createSchedulerClaim,
  saveSchedulerClaim,
  releaseSchedulerClaim,
  triggerWorkflowDispatch,
} = vi.hoisted(() => ({
  getAdminSession: vi.fn(),
  saveStoredConfig: vi.fn(),
  getStoredConfig: vi.fn(),
  getStoredConfigState: vi.fn(),
  getScheduledRunRecord: vi.fn(),
  createSchedulerClaim: vi.fn(),
  saveSchedulerClaim: vi.fn(),
  releaseSchedulerClaim: vi.fn(),
  triggerWorkflowDispatch: vi.fn(),
}));

vi.mock("@/lib/auth", () => ({ getAdminSession }));
vi.mock("@/lib/b2", () => ({
  saveStoredConfig,
  getStoredConfig,
  getStoredConfigState,
  getScheduledRunRecord,
  createSchedulerClaim,
  saveSchedulerClaim,
  releaseSchedulerClaim,
}));
vi.mock("@/lib/github", () => ({ triggerWorkflowDispatch }));

import { POST } from "../app/api/settings/route";
import { runSchedulerTick } from "../lib/scheduler-server";
import { StoredConfigSchema, type StoredConfig } from "../lib/schema";

const baseValidConfig = StoredConfigSchema.parse({
  automation_enabled: true,
  publishing_schedule: [
    { id: "morning", time: "09:00", enabled: true },
    { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
    { id: "evening", time: "20:00", enabled: false },
  ],
});

function createRequest(body: Record<string, any>): NextRequest {
  return new NextRequest("http://localhost:3000/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("Phase 8F.1 — Immediate Same-Day Schedule Save Trigger", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getAdminSession.mockResolvedValue(true);
    saveStoredConfig.mockImplementation(async (cfg) => ({ ...cfg }));
    getStoredConfig.mockResolvedValue(baseValidConfig);
    getScheduledRunRecord.mockResolvedValue(null);
    createSchedulerClaim.mockResolvedValue(true);
    saveSchedulerClaim.mockResolvedValue(undefined);
    releaseSchedulerClaim.mockResolvedValue(undefined);
    triggerWorkflowDispatch.mockResolvedValue({ success: true, message: "ok" });
  });

  // Test A: save at 09:17 after changing unclaimed morning to 09:00 triggers exactly one due run
  it("Test A: save at 09:17 after changing unclaimed morning to 09:00 triggers exactly one due run", async () => {
    // Berlin 09:17 CEST is 08:17 UTC
    const clock0917 = new Date("2026-09-05T07:17:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
        { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
      ],
    };

    const result = await runSchedulerTick(clock0917, testConfig);

    expect(result.automationEnabled).toBe(true);
    expect(result.berlinTime).toBe("09:17");
    expect(result.dispatched).toEqual(["morning"]);
    expect(createSchedulerClaim).toHaveBeenCalledTimes(1);
    expect(createSchedulerClaim).toHaveBeenCalledWith(expect.objectContaining({
      date: "2026-09-05",
      slot_id: "morning",
      run_id: "scheduled_20260905_morning",
      status: "CLAIMED",
    }));
    expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(1);
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(expect.objectContaining({
      runKind: "scheduled",
      slotId: "morning",
      runId: "scheduled_20260905_morning",
    }));
    expect(saveSchedulerClaim).toHaveBeenCalledWith(expect.objectContaining({
      status: "DISPATCHED",
    }));
  });

  // Test B: save at 09:35 after changing to 09:00 does not publish early
  it("Test B: save at 09:35 after changing to 09:00 does not publish early", async () => {
    // Berlin 09:35 CEST is 07:35 UTC
    const clock0935 = new Date("2026-09-05T06:35:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
      ],
    };

    const result = await runSchedulerTick(clock0935, testConfig);

    expect(result.berlinTime).toBe("08:35");
    expect(result.dispatched).toEqual([]);
    expect(result.skipped).toEqual([]);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test C: subsequent hourly heartbeat after save cannot duplicate the run
  it("Test C: subsequent hourly heartbeat after save cannot duplicate the run", async () => {
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
      ],
    };

    // Heartbeat at 11:03 CEST (09:03 UTC): slot is outside 45m grace window
    const clock1103 = new Date("2026-09-05T09:03:00Z");
    const result1103 = await runSchedulerTick(clock1103, testConfig);
    expect(result1103.dispatched).toEqual([]);
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();

    // Even if an earlier heartbeat at 09:35 CEST checks, the existing claim prevents duplication
    const clock0935 = new Date("2026-09-05T07:35:00Z");
    createSchedulerClaim.mockResolvedValue(false); // claim already exists from 09:17 save
    const result0935 = await runSchedulerTick(clock0935, testConfig);
    expect(result0935.skipped).toEqual(["morning"]);
    expect(result0935.dispatched).toEqual([]);
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test D: already-claimed stable slot does not run again after time edit
  it("Test D: already-claimed stable slot does not run again after time edit", async () => {
    // Morning was claimed at 08:00 today; user changes time to 09:00 at 09:30
    // Slot record already exists
    getScheduledRunRecord.mockResolvedValue({
      run_id: "scheduled_20260905_morning",
      schedule_slot_id: "morning",
    });

    const clock0917 = new Date("2026-09-05T07:17:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
      ],
    };

    const result = await runSchedulerTick(clock0917, testConfig);

    expect(result.skipped).toEqual(["morning"]);
    expect(result.dispatched).toEqual([]);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test E: outside 45-minute grace does not catch up
  it("Test E: outside 45-minute grace does not catch up", async () => {
    // Slot is 09:00, saved at 09:50 CEST (08:50 UTC) -> 50 minutes elapsed (> 45m grace)
    const clock0950 = new Date("2026-09-05T07:50:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
      ],
    };

    const result = await runSchedulerTick(clock0950, testConfig);

    expect(result.dispatched).toEqual([]);
    expect(result.skipped).toEqual([]);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test F: automation_enabled=false causes zero claim/run
  it("Test F: automation_enabled=false causes zero claim/run", async () => {
    const clock0917 = new Date("2026-09-05T07:17:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      automation_enabled: false,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
      ],
    };

    const result = await runSchedulerTick(clock0917, testConfig);

    expect(result.automationEnabled).toBe(false);
    expect(result.dispatched).toEqual([]);
    expect(result.skipped).toEqual([]);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test G: disabled slot causes zero claim/run
  it("Test G: disabled slot causes zero claim/run", async () => {
    const clock0917 = new Date("2026-09-05T07:17:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: false },
      ],
    };

    const result = await runSchedulerTick(clock0917, testConfig);

    expect(result.dispatched).toEqual([]);
    expect(result.skipped).toEqual([]);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test H: Dashboard save must persist Supabase config before scheduler evaluation
  it("Test H: Dashboard save must persist Supabase config before scheduler evaluation", async () => {
    const executionOrder: string[] = [];

    saveStoredConfig.mockImplementation(async (cfg) => {
      executionOrder.push("saveStoredConfig");
      return { ...cfg };
    });

    createSchedulerClaim.mockImplementation(async () => {
      executionOrder.push("createSchedulerClaim");
      return true;
    });

    triggerWorkflowDispatch.mockImplementation(async () => {
      executionOrder.push("triggerWorkflowDispatch");
      return { success: true, message: "ok" };
    });

    const requestConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
        { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
      ],
    };

    const req = createRequest(requestConfig);
    const res = await POST(req);

    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.success).toBe(true);

    // saveStoredConfig MUST be called before scheduler claim or dispatch
    expect(executionOrder[0]).toBe("saveStoredConfig");
    expect(saveStoredConfig).toHaveBeenCalledTimes(1);

    // Verify fail-closed: if saveStoredConfig fails, scheduler is NEVER evaluated
    vi.clearAllMocks();
    saveStoredConfig.mockRejectedValue(new Error("Supabase write failure"));
    const failReq = createRequest(requestConfig);
    const failRes = await POST(failReq);
    expect(failRes.status).toBe(400);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // Test I: simultaneous save-trigger and heartbeat remains exactly-once
  it("Test I: simultaneous save-trigger and heartbeat remains exactly-once", async () => {
    const clock0917 = new Date("2026-09-05T07:17:00Z");
    const testConfig: StoredConfig = {
      ...baseValidConfig,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
      ],
    };

    // Atomic claim: first concurrent call acquires (returns true), second gets conflict (returns false)
    let claimCount = 0;
    createSchedulerClaim.mockImplementation(async () => {
      claimCount += 1;
      return claimCount === 1;
    });

    // Execute save-trigger and heartbeat simultaneously
    const [resultSave, resultHeartbeat] = await Promise.all([
      runSchedulerTick(clock0917, testConfig),
      runSchedulerTick(clock0917, testConfig),
    ]);

    const totalDispatched = [...resultSave.dispatched, ...resultHeartbeat.dispatched];
    const totalSkipped = [...resultSave.skipped, ...resultHeartbeat.skipped];

    // Exactly one call dispatches, the other skips
    expect(totalDispatched).toEqual(["morning"]);
    expect(totalSkipped).toEqual(["morning"]);
    expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(1);
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(expect.objectContaining({
      runKind: "scheduled",
      slotId: "morning",
    }));
  });
  it("rejects unknown active slots, four enabled times, or duplicated times", async () => {
    const approved = [
      { id: "morning", time: "09:00", enabled: true },
      { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
    ];
    for (const schedule of [
      [...approved, { id: "evening", time: "13:00", enabled: true }],
      [...approved, { id: "slot-third", time: "20:00", enabled: true }, { id: "extra", time: "21:00", enabled: true }],
      [approved[0], { ...approved[1], time: "09:00" }],
    ]) {
      const response = await POST(createRequest({ ...baseValidConfig, publishing_schedule: schedule }));
      expect(response.status).toBe(400);
      expect((await response.json()).error).toMatch(/1 to 3 different approved publishing times|Duplicate active publishing time/);
    }
    expect(saveStoredConfig).not.toHaveBeenCalled();
    expect(createSchedulerClaim).not.toHaveBeenCalled();
  });

  it("saves one or three daily Reels and blocks unapproved active slot identities", async () => {
    for (const schedule of [
      [{ id: "morning", time: "10:00", enabled: true }],
      [
        { id: "morning", time: "10:00", enabled: true },
        { id: "slot-00000000-0000-4000-8000-000000000002", time: "18:00", enabled: true },
        { id: "slot-third", time: "21:15", enabled: true },
      ],
    ]) {
      const response = await POST(createRequest({
        ...baseValidConfig,
        automation_enabled: false,
        publishing_schedule: schedule,
      }));
      expect(response.status).toBe(200);
      expect(saveStoredConfig).toHaveBeenCalledWith(
        expect.objectContaining({ publishing_schedule: schedule }),
      );
    }
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  it("preserves Admin-selected times and disabled historical slots in an approved save", async () => {
    const publishingSchedule = [
      { id: "morning", time: "10:25", enabled: true },
      { id: "slot-00000000-0000-4000-8000-000000000002", time: "18:40", enabled: true },
      { id: "evening", time: "13:00", enabled: false },
    ];
    const response = await POST(createRequest({ ...baseValidConfig, publishing_schedule: publishingSchedule, arabic_font_size: 160, instagram_enabled: false }));
    expect(response.status).toBe(200);
    expect(saveStoredConfig).toHaveBeenCalledWith(expect.objectContaining({
      publishing_schedule: publishingSchedule, arabic_font_size: 160, instagram_enabled: false, timezone: "Europe/Berlin",
    }));
  });

});
