import { beforeEach, describe, expect, it, vi } from "vitest";

const { getStoredConfig, getScheduledRunRecord, createSchedulerClaim, saveSchedulerClaim, releaseSchedulerClaim, triggerWorkflowDispatch } = vi.hoisted(() => ({
  getStoredConfig: vi.fn(),
  getScheduledRunRecord: vi.fn(),
  createSchedulerClaim: vi.fn(),
  saveSchedulerClaim: vi.fn(),
  releaseSchedulerClaim: vi.fn(),
  triggerWorkflowDispatch: vi.fn(),
}));

vi.mock("../lib/b2", () => ({
  getStoredConfig,
  getScheduledRunRecord,
  createSchedulerClaim,
  saveSchedulerClaim,
  releaseSchedulerClaim,
}));
vi.mock("../lib/github", () => ({ triggerWorkflowDispatch }));

import { runSchedulerTick } from "../lib/scheduler-server";

describe("provider-independent scheduler tick", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getStoredConfig.mockResolvedValue({
      automation_enabled: true,
      publishing_schedule: [{ id: "morning", time: "09:00", enabled: true }],
    });
    createSchedulerClaim.mockResolvedValue(true);
    getScheduledRunRecord.mockResolvedValue(null);
    saveSchedulerClaim.mockResolvedValue(undefined);
    releaseSchedulerClaim.mockResolvedValue(undefined);
    triggerWorkflowDispatch.mockResolvedValue({ success: true, message: "ok" });
  });

  it("does nothing while automation is paused", async () => {
    getStoredConfig.mockResolvedValue({ automation_enabled: false, publishing_schedule: [] });
    const result = await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(result.automationEnabled).toBe(false);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
  });

  it("claims and dispatches a due stable slot once", async () => {
    const result = await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(result.dispatched).toEqual(["morning"]);
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(expect.objectContaining({
      runKind: "scheduled",
      slotId: "morning",
      runId: "scheduled_20260824_morning",
    }));
    expect(saveSchedulerClaim).toHaveBeenCalledWith(expect.objectContaining({ status: "DISPATCHED" }));
  });

  it("honors Admin-selected Berlin times while retaining stable GitHub run IDs", async () => {
    const afternoonId = "slot-00000000-0000-4000-8000-000000000002";
    getStoredConfig.mockResolvedValue({
      automation_enabled: true,
      publishing_schedule: [
        { id: "morning", time: "10:25", enabled: true },
        { id: afternoonId, time: "18:40", enabled: true },
      ],
      facebook_enabled: true,
      instagram_enabled: true,
    });
    const before = await runSchedulerTick(new Date("2026-08-24T08:23:00Z"));
    expect(before.dispatched).toEqual([]);
    const morning = await runSchedulerTick(new Date("2026-08-24T08:33:00Z"));
    expect(morning.dispatched).toEqual(["morning"]);
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(expect.objectContaining({
      runKind: "scheduled", slotId: "morning", runId: "scheduled_20260824_morning",
      publishFacebook: true, publishInstagram: true,
    }));
    const afternoon = await runSchedulerTick(new Date("2026-08-24T16:43:00Z"));
    expect(afternoon.dispatched).toEqual([afternoonId]);
  });

  it("dispatches three approved daily Berlin slots without double-claiming", async () => {
    const entries = [
      { id: "morning", time: "10:00", enabled: true },
      { id: "slot-00000000-0000-4000-8000-000000000002", time: "18:00", enabled: true },
      { id: "slot-third", time: "21:00", enabled: true },
    ];
    getStoredConfig.mockResolvedValue({
      automation_enabled: true, publishing_schedule: entries,
      facebook_enabled: true, instagram_enabled: true,
    });
    const claims = new Set<string>();
    createSchedulerClaim.mockImplementation(async (claim) => {
      const key = claim.date + "/" + claim.slot_id;
      if (claims.has(key)) return false;
      claims.add(key);
      return true;
    });
    const dispatched: string[] = [];
    for (const utc of ["2026-08-24T08:05:00Z", "2026-08-24T16:05:00Z", "2026-08-24T19:05:00Z"]) {
      dispatched.push(...(await runSchedulerTick(new Date(utc))).dispatched);
      dispatched.push(...(await runSchedulerTick(new Date(utc))).dispatched);
    }
    expect(dispatched).toEqual(entries.map((entry) => entry.id));
    expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(3);
  });

  it("never double-dispatches when the same-day claim already exists", async () => {
    createSchedulerClaim.mockResolvedValue(false);
    const result = await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(result.skipped).toEqual(["morning"]);
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  it("skips a slot that already has a durable scheduled run", async () => {
    getScheduledRunRecord.mockResolvedValue({ run_id: "existing" });
    const result = await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(result.skipped).toEqual(["morning"]);
    expect(createSchedulerClaim).not.toHaveBeenCalled();
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  it("releases a definitively rejected dispatch but retains an ambiguous claim", async () => {
    triggerWorkflowDispatch.mockResolvedValueOnce({ success: false, message: "rejected" });
    await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(releaseSchedulerClaim).toHaveBeenCalledWith("2026-08-24", "morning");

    vi.clearAllMocks();
    getStoredConfig.mockResolvedValue({ automation_enabled: true, publishing_schedule: [
      { id: "morning", time: "09:00", enabled: true },
      { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
    ] });
    getScheduledRunRecord.mockResolvedValue(null);
    createSchedulerClaim.mockResolvedValue(true);
    triggerWorkflowDispatch.mockResolvedValue({ success: false, ambiguous: true, message: "timeout" });
    await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(releaseSchedulerClaim).not.toHaveBeenCalled();
  });
  it("persists only safe GitHub dispatch error codes, not raw response text", async () => {
    triggerWorkflowDispatch.mockResolvedValue({
      success: false,
      message: "GitHub API error (403): potentially sensitive upstream response",
    });
    const result = await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(result.failed).toEqual(["morning"]);
    expect(result.failureCodes).toEqual({ morning: "GITHUB_HTTP_403" });
    expect(saveSchedulerClaim).toHaveBeenCalledWith(
      expect.objectContaining({ status: "FAILED", failure_reason: "GITHUB_HTTP_403" }),
    );
    expect(JSON.stringify(result)).not.toContain("potentially sensitive");
    expect(releaseSchedulerClaim).toHaveBeenCalledWith("2026-08-24", "morning");
  });

  it("separates missing GitHub dispatch configuration from network uncertainty", async () => {
    triggerWorkflowDispatch.mockResolvedValueOnce({
      success: false,
      message: "GitHub Actions dispatch is not configured. Missing GITHUB_OWNER.",
    });
    const missing = await runSchedulerTick(new Date("2026-08-24T07:15:00Z"));
    expect(missing.failureCodes.morning).toBe("GITHUB_CONFIG_MISSING");

    triggerWorkflowDispatch.mockResolvedValueOnce({
      success: false,
      ambiguous: true,
      message: "Network error triggering GitHub Actions: connection closed",
    });
    const uncertain = await runSchedulerTick(new Date("2026-08-25T07:15:00Z"));
    expect(uncertain.failureCodes.morning).toBe("GITHUB_NETWORK_UNCERTAIN");
    expect(releaseSchedulerClaim).toHaveBeenCalledTimes(1);
  });

  it("never dispatches invalid schedules containing three enabled times", async () => {
    getStoredConfig.mockResolvedValue({
      automation_enabled: true,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
        { id: "evening", time: "13:00", enabled: true },
        { id: "new", time: "16:00", enabled: true },
      ],
    });
    for (const stamp of ["2026-08-24T11:03:00Z", "2026-08-24T14:03:00Z"]) {
      const result = await runSchedulerTick(new Date(stamp));
      expect(result.dispatched).toEqual([]);
    }
    expect(createSchedulerClaim).not.toHaveBeenCalled();
  });

  it("claims two Admin-configured daily slots across both DST transition dates", async () => {
    const afternoonId = "slot-00000000-0000-4000-8000-000000000002";
    getStoredConfig.mockResolvedValue({
      automation_enabled: true,
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
        { id: afternoonId, time: "16:00", enabled: true },
        { id: "evening", time: "13:00", enabled: false },
      ],
    });
    const claims = new Set<string>();
    createSchedulerClaim.mockImplementation(async (claim) => {
      const key = `${claim.date}/${claim.slot_id}`;
      if (claims.has(key)) return false;
      claims.add(key);
      return true;
    });
    for (const date of ["2026-03-29", "2026-10-25"]) {
      const dispatched: string[] = [];
      for (let hour = 0; hour < 24; hour += 1) {
        const now = new Date(`${date}T${String(hour).padStart(2, "0")}:03:00Z`);
        dispatched.push(...(await runSchedulerTick(now)).dispatched);
        dispatched.push(...(await runSchedulerTick(now)).dispatched);
      }
      expect(dispatched).toEqual(["morning", afternoonId]);
    }
    expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(4);
  });

});
