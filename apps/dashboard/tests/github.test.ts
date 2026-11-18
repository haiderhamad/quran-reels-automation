import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { buildWorkflowDispatchPayload, triggerBackgroundDiscovery, triggerWorkflowDispatch } from "../lib/github";

describe("GitHub Workflow Dispatch Integration", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
    vi.restoreAllMocks();
  });

  it("returns error if GitHub credentials are not configured", async () => {
    delete process.env.GITHUB_OWNER;
    delete process.env.GITHUB_REPO;
    delete process.env.GITHUB_ACTIONS_TOKEN;

    const result = await triggerWorkflowDispatch("test", false);
    expect(result.success).toBe(false);
    expect(result.message).toContain("GitHub Actions dispatch is not configured");
  });

  it("sends workflow_dispatch POST request with correct payload and headers", async () => {
    process.env.GITHUB_OWNER = "dummy-owner";
    process.env.GITHUB_REPO = "dummy-repo";
    process.env.GITHUB_WORKFLOW_ID = "generate-reel.yml";
    process.env.GITHUB_WORKFLOW_REF = "main";
    process.env.GITHUB_ACTIONS_TOKEN = "dummy_github_actions_token";

    const mockFetch = vi.fn().mockResolvedValue({
      status: 204,
      ok: true,
      text: async () => "",
    });
    global.fetch = mockFetch;

    const result = await triggerWorkflowDispatch("morning", true);

    expect(result.success).toBe(true);
    expect(result.message).toContain("Generation requested successfully for 'morning'");
    expect(mockFetch).toHaveBeenCalledTimes(1);

    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe(
      "https://api.github.com/repos/dummy-owner/dummy-repo/actions/workflows/generate-reel.yml/dispatches"
    );
    expect(options.method).toBe("POST");
    expect(options.headers["Authorization"]).toBe("Bearer dummy_github_actions_token");

    const body = JSON.parse(options.body);
    expect(body.ref).toBe("main");
    expect(body.inputs.run_kind).toBe("scheduled");
    expect(body.inputs.schedule_slot_id).toBe("morning");
    expect(body.inputs.run_id).toMatch(/^legacy_morning_/);
    expect(body.inputs.force).toBe(true);
  });

  it("dispatches a unique manual production request without exposing credentials", async () => {
    process.env.GITHUB_OWNER = "dummy-owner";
    process.env.GITHUB_REPO = "dummy-repo";
    process.env.GITHUB_ACTIONS_TOKEN = "dummy_github_actions_token";
    const mockFetch = vi.fn().mockResolvedValue({ status: 204, text: async () => "" });
    global.fetch = mockFetch;
    const result = await triggerWorkflowDispatch({ runKind: "manual_production", runId: "manual_123" });
    expect(result.success).toBe(true);
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.inputs).toEqual({
      run_kind: "manual_production",
      schedule_slot_id: "",
      run_id: "manual_123",
      force: false,
      test_verse_key: "",
      publish_facebook: true,
      publish_instagram: true,
    });
    expect(JSON.stringify(body)).not.toContain("dummy_github_actions_token");
  });

  it("fails closed on unsafe IDs and invalid run-kind/slot combinations", () => {
    expect(() => buildWorkflowDispatchPayload({
      runKind: "scheduled",
      runId: "scheduled_good",
    }, "main")).toThrow("requires a path-safe schedule slot ID");
    expect(() => buildWorkflowDispatchPayload({
      runKind: "test",
      runId: "../unsafe",
    }, "main")).toThrow("path-safe non-empty identifier");
    expect(() => buildWorkflowDispatchPayload({
      runKind: "manual_production",
      runId: "manual_good",
      slotId: "morning",
    }, "main")).toThrow("Only scheduled");
    expect(() => buildWorkflowDispatchPayload({
      runKind: "manual_production",
      runId: "manual_good",
      testVerseKey: "2:21",
    }, "main")).toThrow("Only test");
  });

  it("handles GitHub API error responses safely", async () => {
    process.env.GITHUB_OWNER = "dummy-owner";
    process.env.GITHUB_REPO = "dummy-repo";
    process.env.GITHUB_ACTIONS_TOKEN = "dummy_github_actions_token";

    global.fetch = vi.fn().mockResolvedValue({
      status: 404,
      ok: false,
      text: async () => JSON.stringify({ message: "Workflow not found" }),
    });

    const result = await triggerWorkflowDispatch("evening", false);
    expect(result.success).toBe(false);
    expect(result.message).toContain("GitHub API error (404): Workflow not found");
  });

  it("dispatches only the fixed background-discovery workflow", async () => {
    process.env.GITHUB_OWNER = "dummy-owner";
    process.env.GITHUB_REPO = "dummy-repo";
    process.env.GITHUB_ACTIONS_TOKEN = "dummy_github_actions_token";
    global.fetch = vi.fn().mockResolvedValue({ status: 204, ok: true, text: async () => "" });
    const result = await triggerBackgroundDiscovery();
    expect(result.success).toBe(true);
    expect(global.fetch).toHaveBeenCalledWith(
      "https://api.github.com/repos/dummy-owner/dummy-repo/actions/workflows/background-discovery.yml/dispatches",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
