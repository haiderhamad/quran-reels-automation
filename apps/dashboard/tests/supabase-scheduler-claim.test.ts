import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  claimSchedulerSlotInSupabase,
  getSchedulerClaimFromSupabase,
  LIVE_TEST_WRITE_OPT_IN_ENV,
  releaseSchedulerClaimInSupabase,
  updateSchedulerClaimInSupabase,
} from "../lib/supabase-state";
import {
  claimSchedulerSlot,
  createSchedulerClaim,
  getSchedulerClaim,
  releaseSchedulerClaim,
  saveSchedulerClaim,
  type SchedulerClaim,
} from "../lib/b2";
import { getSchedulerClaim as getAuthoritySchedulerClaim } from "../lib/state-authority";
import { runSchedulerTick } from "../lib/scheduler-server";
import * as githubModule from "../lib/github";

describe("Supabase Scheduler Claim Lifecycle & Status Updates", () => {
  const originalEnv = process.env;
  let mockClaimsTable: Map<string, any>;

  beforeEach(() => {
    process.env = {
      ...originalEnv,
      NODE_ENV: "test",
      STATE_AUTHORITY: "supabase",
      SUPABASE_URL: "https://mock-db.supabase.co",
      SUPABASE_SERVICE_ROLE_KEY: "mock-service-role-key",
      [LIVE_TEST_WRITE_OPT_IN_ENV]: "1",
    };

    mockClaimsTable = new Map();

    // Mock global fetch to emulate Supabase PostgREST table operations on scheduler_claims
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method || "GET";
        const parsedUrl = new URL(url);
        const pathParts = parsedUrl.pathname.split("/");
        const table = pathParts[pathParts.length - 1];

        if (table === "scheduler_claims") {
          const berlinDateParam = parsedUrl.searchParams.get("berlin_date") || "";
          const slotIdParam = parsedUrl.searchParams.get("slot_id") || "";
          const date = berlinDateParam.replace(/^eq\./, "");
          const slotId = slotIdParam.replace(/^eq\./, "");
          const key = `${date}:${slotId}`;

          if (method === "POST") {
            const body = JSON.parse((init?.body as string) || "{}");
            const claimKey = `${body.berlin_date}:${body.slot_id}`;
            if (mockClaimsTable.has(claimKey)) {
              return new Response(JSON.stringify({ code: "23505", message: "duplicate key value" }), {
                status: 409,
                headers: { "Content-Type": "application/json" },
              });
            }
            mockClaimsTable.set(claimKey, { ...body, updated_at: new Date().toISOString() });
            return new Response(JSON.stringify([{ ...body }]), {
              status: 201,
              headers: { "Content-Type": "application/json" },
            });
          }

          if (method === "PATCH") {
            const body = JSON.parse((init?.body as string) || "{}");
            if (!mockClaimsTable.has(key)) {
              // Return empty array to signal no rows updated (PATCH on non-existent row)
              return new Response(JSON.stringify([]), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              });
            }
            const existing = mockClaimsTable.get(key);
            const updated = {
              ...existing,
              run_id: body.run_id ?? existing.run_id,
              status: body.status ?? existing.status,
              value: body.value ?? existing.value,
              updated_at: body.updated_at || new Date().toISOString(),
            };
            mockClaimsTable.set(key, updated);
            return new Response(JSON.stringify([updated]), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }

          if (method === "GET") {
            if (!mockClaimsTable.has(key)) {
              return new Response(JSON.stringify([]), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              });
            }
            return new Response(JSON.stringify([mockClaimsTable.get(key)]), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }

          if (method === "DELETE") {
            mockClaimsTable.delete(key);
            return new Response(null, { status: 204 });
          }
        }

        // Mock config read for scheduler-server if requested
        if (table === "app_config") {
          return new Response(
            JSON.stringify([
              {
                singleton: true,
                value: {
                  automation_enabled: true,
                  facebook_enabled: true,
                  instagram_enabled: true,
                  publishing_schedule: [{ id: "morning", time: "09:00", enabled: true }],
                },
              },
            ]),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }

        if (table === "scheduler_heartbeats") {
          return new Response(JSON.stringify([]), { status: 201 });
        }

        if (table === "runs" || table === "run_records") {
          return new Response(JSON.stringify([]), { status: 200 });
        }

        return new Response(JSON.stringify([]), { status: 200 });
      })
    );
  });

  afterEach(() => {
    process.env = originalEnv;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("acquires initial claim with status=CLAIMED and synchronized value.status", async () => {
    const date = "2026-09-06";
    const slotId = "morning";
    const runId = "scheduled_20260906_morning";

    const acquired = await claimSchedulerSlotInSupabase(date, slotId, undefined, runId);
    expect(acquired).toBe(true);

    const stored = mockClaimsTable.get(`${date}:${slotId}`);
    expect(stored).toBeDefined();
    expect(stored.status).toBe("CLAIMED");
    expect(stored.value.status).toBe("CLAIMED");
    expect(stored.run_id).toBe(runId);
    expect(stored.value.run_id).toBe(runId);
  });

  it("duplicate acquisition returns false and does NOT overwrite existing claim", async () => {
    const date = "2026-09-06";
    const slotId = "morning";
    const runId = "scheduled_20260906_morning";

    const first = await claimSchedulerSlotInSupabase(date, slotId, "2026-09-06T07:00:00Z", runId);
    expect(first).toBe(true);

    // Attempt second claim with different run ID
    const duplicate = await claimSchedulerSlotInSupabase(date, slotId, "2026-09-06T07:05:00Z", "overwrite_attempt");
    expect(duplicate).toBe(false);

    // Verify existing claim was NOT overwritten
    const stored = mockClaimsTable.get(`${date}:${slotId}`);
    expect(stored.run_id).toBe(runId);
    expect(stored.status).toBe("CLAIMED");
  });

  it("updates existing claim to DISPATCHED and keeps status column and value.status synchronized", async () => {
    const date = "2026-09-06";
    const slotId = "morning";
    const runId = "scheduled_20260906_morning";
    const claimedAt = "2026-09-06T07:00:00Z";

    await claimSchedulerSlotInSupabase(date, slotId, claimedAt, runId);

    const claimToUpdate: SchedulerClaim = {
      date,
      slot_id: slotId,
      run_id: runId,
      status: "DISPATCHED",
      claimed_at: claimedAt,
      updated_at: "2026-09-06T07:00:02Z",
    };

    await updateSchedulerClaimInSupabase(claimToUpdate);

    const stored = mockClaimsTable.get(`${date}:${slotId}`);
    expect(stored.status).toBe("DISPATCHED");
    expect(stored.value.status).toBe("DISPATCHED");
    expect(stored.run_id).toBe(runId);
    expect(stored.value.run_id).toBe(runId);

    // Read back via getSchedulerClaimFromSupabase
    const fetched = await getSchedulerClaimFromSupabase(date, slotId);
    expect(fetched).not.toBeNull();
    expect(fetched?.status).toBe("DISPATCHED");
    expect(fetched?.worker_run_id).toBe(runId);
  });

  it("updates existing claim to FAILED when dispatch fails, recording failure_reason", async () => {
    const date = "2026-09-06";
    const slotId = "morning";
    const runId = "scheduled_20260906_morning";
    const claimedAt = "2026-09-06T07:00:00Z";

    await claimSchedulerSlotInSupabase(date, slotId, claimedAt, runId);

    const failedClaim: SchedulerClaim = {
      date,
      slot_id: slotId,
      run_id: runId,
      status: "FAILED",
      claimed_at: claimedAt,
      updated_at: "2026-09-06T07:00:05Z",
      failure_reason: "GitHub Actions rejected the dispatch request.",
    };

    await updateSchedulerClaimInSupabase(failedClaim);

    const stored = mockClaimsTable.get(`${date}:${slotId}`);
    expect(stored.status).toBe("FAILED");
    expect(stored.value.status).toBe("FAILED");
    expect(stored.value.failure_reason).toBe("GitHub Actions rejected the dispatch request.");

    const fetched = await getSchedulerClaimFromSupabase(date, slotId);
    expect(fetched?.status).toBe("FAILED");
    expect(fetched?.failure_reason).toBe("GitHub Actions rejected the dispatch request.");
  });

  it("fails closed when attempting to update a claim that does not exist", async () => {
    const nonExistentClaim: SchedulerClaim = {
      date: "2026-09-06",
      slot_id: "nonexistent",
      run_id: "ghost_run",
      status: "DISPATCHED",
      claimed_at: "2026-09-06T07:00:00Z",
      updated_at: "2026-09-06T07:00:01Z",
    };

    await expect(updateSchedulerClaimInSupabase(nonExistentClaim)).rejects.toThrow(
      /Cannot update scheduler claim for 2026-09-06\/nonexistent: claim does not exist/
    );
  });

  it("saveSchedulerClaim properly updates Supabase claim instead of failing with conflict", async () => {
    const date = "2026-09-06";
    const slotId = "evening";
    const runId = "scheduled_20260906_evening";

    // 1. Initial claim creation via b2/state-authority facade
    const created = await createSchedulerClaim({
      date,
      slot_id: slotId,
      run_id: runId,
      status: "CLAIMED",
      claimed_at: "2026-09-06T11:00:00Z",
      updated_at: "2026-09-06T11:00:00Z",
    });
    expect(created).toBe(true);

    // Initial state is CLAIMED
    let claim = await getSchedulerClaim(date, slotId);
    expect(claim?.status).toBe("CLAIMED");

    // 2. saveSchedulerClaim after successful dispatch
    await saveSchedulerClaim({
      date,
      slot_id: slotId,
      run_id: runId,
      status: "DISPATCHED",
      claimed_at: "2026-09-06T11:00:00Z",
      updated_at: "2026-09-06T11:00:02Z",
    });

    // 3. Must now reflect DISPATCHED, NOT remain CLAIMED
    claim = await getSchedulerClaim(date, slotId);
    expect(claim?.status).toBe("DISPATCHED");

    // Also verify via state-authority facade
    const authorityClaim = await getAuthoritySchedulerClaim(date, slotId);
    expect(authorityClaim?.status).toBe("DISPATCHED");
    expect(authorityClaim?.run_id).toBe(runId);
  });

  it("releases claim cleanly via releaseSchedulerClaimInSupabase", async () => {
    const date = "2026-09-06";
    const slotId = "morning";
    await claimSchedulerSlotInSupabase(date, slotId, undefined, "scheduled_test");
    expect(mockClaimsTable.has(`${date}:${slotId}`)).toBe(true);

    await releaseSchedulerClaimInSupabase(date, slotId);
    expect(mockClaimsTable.has(`${date}:${slotId}`)).toBe(false);

    const claim = await getSchedulerClaimFromSupabase(date, slotId);
    expect(claim).toBeNull();
  });

  it("end-to-end runSchedulerTick transitions claim from CLAIMED to DISPATCHED on HTTP 204", async () => {
    const triggerSpy = vi.spyOn(githubModule, "triggerWorkflowDispatch").mockResolvedValue({
      success: true,
      message: "Workflow dispatch triggered.",
    });

    // Run tick at 09:05 Europe/Berlin time (07:05 UTC)
    const tickTime = new Date("2026-09-06T07:05:00Z");
    const result = await runSchedulerTick(tickTime, undefined, "supabase_cron");

    expect(result.dispatched).toEqual(["morning"]);
    expect(triggerSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        runKind: "scheduled",
        slotId: "morning",
        runId: "scheduled_20260906_morning",
      })
    );

    // Stored claim in Supabase must be DISPATCHED
    const storedClaim = await getSchedulerClaim("2026-09-06", "morning");
    expect(storedClaim?.status).toBe("DISPATCHED");
    expect(storedClaim?.run_id).toBe("scheduled_20260906_morning");

    // Second heartbeat on the same day must see existing claim and NOT re-dispatch
    const secondResult = await runSchedulerTick(new Date("2026-09-06T07:15:00Z"), undefined, "supabase_cron");
    expect(secondResult.dispatched).toEqual([]);
    expect(secondResult.skipped).toEqual(["morning"]);
  });
});
