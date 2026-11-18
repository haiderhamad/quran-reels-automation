import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getPublishingEvidenceFromSupabase } from "../lib/supabase-state";
import { computePublishingProgress } from "../lib/publishing-progress";

const run = {
  run_id: "production-run",
  date: "2026-10-09",
  slot: "morning",
  run_kind: "scheduled",
  status: "SUCCESS",
  started_at: "2026-10-09T07:00:00Z",
  completed_at: "2026-10-09T07:05:00Z",
  facebook_status: "ACCEPTED",
  instagram_status: "FAILED",
  instagram_failure_reason: "INSTAGRAM_CHANNEL_NOT_VISIBLE: No Instagram channel is visible to Buffer.",
};
const claim = {
  run_id: "production-run",
  platform: "facebook",
  publisher_backend: "buffer",
  channel_id: "facebook-channel",
  state: "SUCCESS",
  buffer_post_id: "buffer-id",
  buffer_post_status: "sending",
  external_link: null,
  accepted_at: "2026-10-09T07:00:00Z",
  last_error_code: null,
  updated_at: "2026-10-09T07:05:00Z",
};

describe("read-only Supabase publication evidence", () => {
  beforeEach(() => {
    vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "test-only-key");
    vi.stubEnv("NODE_ENV", "production");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("loads run records and Buffer claims through ordered GET requests only", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = new URL(String(input));
      return new Response(JSON.stringify(url.pathname.endsWith("/run_records") ? [{ value: run }] : [claim]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const loaded = await getPublishingEvidenceFromSupabase();
    expect(loaded.status).toBe("ready");
    if (loaded.status !== "ready") throw new Error("Expected evidence load to succeed");
    expect(loaded.records).toHaveLength(1);
    expect(loaded.claims).toHaveLength(1);
    const requests = fetchMock.mock.calls.map(([input, init]) => ({ url: new URL(String(input)), init }));
    expect(requests.map((request) => request.url.pathname)).toEqual([
      "/rest/v1/run_records",
      "/rest/v1/buffer_publication_claims",
    ]);
    expect(requests.every(({ init }) => !init?.method || init.method === "GET")).toBe(true);
    expect(requests[1].url.searchParams.get("order")).toBe("run_id.asc,platform.asc,publisher_backend.asc,channel_id.asc");
    expect(requests[1].url.searchParams.get("select")).toContain("publisher_backend,channel_id");

    const summary = computePublishingProgress(loaded.records, loaded.claims, { facebook: true, instagram: true });
    expect(summary.unverifiedDeliveryOutcomes).toBe(1);
    expect(summary.liveVerifiedPublishedAyahs).toBeNull();
    expect(summary.platforms.instagram.operationalHealth).toBe("Degraded");
  });

  it("returns unavailable rather than zero when a ledger row cannot be validated", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([{ run_id: "bad-row" }]), { status: 200 })));
    const result = await getPublishingEvidenceFromSupabase();
    expect(result.status).toBe("unavailable");
    if (result.status !== "unavailable") throw new Error("Expected unavailable evidence");
    expect(result.records).toEqual([]);
    expect(result.claims).toEqual([]);
  });

  it("keeps missing Supabase configuration unavailable instead of inventing empty results", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await getPublishingEvidenceFromSupabase()).toMatchObject({ status: "unavailable" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
