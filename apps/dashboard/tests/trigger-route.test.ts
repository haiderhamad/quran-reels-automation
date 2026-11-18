import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { NextRequest } from "next/server";
import { POST } from "../app/api/actions/trigger/route";

const release = vi.hoisted(() => ({ ready: false }));
vi.mock("@/lib/calm-reels-shortlist", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/calm-reels-shortlist")>()),
  get CALM_REELS_V1_MANUAL_PUBLISH_READY() { return release.ready; },
}));

vi.mock("@/lib/auth", () => ({
  getAdminSession: vi.fn(),
}));

vi.mock("@/lib/github", () => ({
  triggerBackgroundDiscovery: vi.fn(),
  triggerWorkflowDispatch: vi.fn(),
}));

vi.mock("@/lib/b2", () => ({
  getStoredConfig: vi.fn(),
}));

vi.mock("@/lib/system", () => ({
  getSystemConnectionStatusWithPublishing: vi.fn(),
  isPublishNowAllowed: vi.fn(),
  getPublishPlatformBlockReason: vi.fn(),
}));

import { getAdminSession } from "@/lib/auth";
import { triggerBackgroundDiscovery, triggerWorkflowDispatch } from "@/lib/github";
import { getStoredConfig } from "@/lib/b2";
import {
  getPublishPlatformBlockReason,
  getSystemConnectionStatusWithPublishing,
  isPublishNowAllowed,
} from "@/lib/system";

function createRequest(body: Record<string, any>): NextRequest {
  return new NextRequest("http://localhost:3000/api/actions/trigger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("POST /api/actions/trigger (Dashboard Operations Control Center)", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
    release.ready = false;
    vi.clearAllMocks();
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  // 1. Auth check
  it("rejects unauthorized requests with 401", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(false);
    const req = createRequest({ action: "generate_test" });
    const res = await POST(req);
    expect(res.status).toBe(401);
    const data = await res.json();
    expect(data.error).toBe("Unauthorized");
  });

  // 2. Workflow allowlist - reject arbitrary actions
  it("rejects arbitrary unallowlisted actions with 400", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    const req = createRequest({ action: "arbitrary_custom_command", command: "rm -rf /" });
    const res = await POST(req);
    expect(res.status).toBe(400);
    const data = await res.json();
    expect(data.error).toBe("Unsupported operations action.");
  });

  // 3. Find Backgrounds Now
  it("dispatches background_discovery with fixed workflow", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(triggerBackgroundDiscovery).mockResolvedValue({
      success: true,
      message: "Background candidate discovery requested.",
    });

    const req = createRequest({ action: "background_discovery" });
    const res = await POST(req);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.success).toBe(true);
    expect(triggerBackgroundDiscovery).toHaveBeenCalledTimes(1);
  });

  // 4. Generate Test
  it("dispatches generate_test action with test runKind", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(triggerWorkflowDispatch).mockResolvedValue({
      success: true,
      message: "Generation requested successfully for test",
    });

    const req = createRequest({ action: "generate_test" });
    const res = await POST(req);
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.runKind).toBe("test");
    expect(data.action).toBe("generate_test");
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        runKind: "test",
        publishFacebook: false,
        publishInstagram: false,
      })
    );
  });

  // 5. Generate Test rejects invalid test_verse_key
  it("rejects generate_test with invalid test_verse_key", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    const req = createRequest({ action: "generate_test", test_verse_key: "112:1" });
    const res = await POST(req);
    expect(res.status).toBe(400);
    const data = await res.json();
    expect(data.error).toContain("2:21 fixture");
  });

  // 6. Generate Test with 2:21 checks English translations
  it("rejects 2:21 test when multi-language translation configured", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(getStoredConfig).mockResolvedValue({
      translation_languages: ["en", "de"],
    } as any);

    const req = createRequest({ action: "generate_test", test_verse_key: "2:21" });
    const res = await POST(req);
    expect(res.status).toBe(409);
    const data = await res.json();
    expect(data.error).toContain("requires English-only translation display");
  });

  // 7. Publish Now requires at least one target
  it("rejects publish_now when neither Facebook nor Instagram is selected", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    const req = createRequest({ action: "publish_now", publish_facebook: false, publish_instagram: false });
    const res = await POST(req);
    expect(res.status).toBe(400);
    const data = await res.json();
    expect(data.error).toBe("Select Facebook, Instagram, or both publishing targets.");
  });

  // 8. Server-side rights lock: PUBLICATION_BLOCKED_RECITER_RIGHTS
  it("blocks publish_now with 403 when reciter rights are not confirmed", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(getStoredConfig).mockResolvedValue({
      reciter_reuse_confirmed: false,
      facebook_enabled: true,
      instagram_enabled: true,
    } as any);
    vi.mocked(getSystemConnectionStatusWithPublishing).mockResolvedValue({
      reciterReuseRuntimeGate: false,
    } as any);
    vi.mocked(isPublishNowAllowed).mockReturnValue(false);

    const req = createRequest({ action: "publish_now", publish_facebook: true });
    const res = await POST(req);
    expect(res.status).toBe(403);
    const data = await res.json();
    expect(data.code).toBe("PUBLICATION_BLOCKED_RECITER_RIGHTS");
    expect(data.error).toBe("PUBLICATION_BLOCKED_RECITER_RIGHTS");
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // 9. Platform connection missing
  it("blocks publish_now with 409 when target is enabled but connection is missing", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(getStoredConfig).mockResolvedValue({
      reciter_reuse_confirmed: true,
      facebook_enabled: true,
      instagram_enabled: false,
    } as any);
    vi.mocked(getSystemConnectionStatusWithPublishing).mockResolvedValue({
      reciterReuseRuntimeGate: true,
      bufferFacebook: { connected: false, channelConfigured: false },
      bufferInstagram: { connected: false, channelConfigured: false },
    } as any);
    vi.mocked(isPublishNowAllowed).mockReturnValue(true);
    vi.mocked(getPublishPlatformBlockReason).mockReturnValue("Facebook channel is disconnected.");

    const req = createRequest({ action: "publish_now", publish_facebook: true });
    const res = await POST(req);
    expect(res.status).toBe(409);
    const data = await res.json();
    expect(data.error).toContain("Publish Now is blocked: Facebook channel is disconnected.");
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // 10. Publish Now with Facebook only
  it("rejects Facebook-only posting when the two-channel release is enabled", async () => {
    release.ready = true;
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(getStoredConfig).mockResolvedValue({
      reciter_reuse_confirmed: true,
      facebook_enabled: true,
      instagram_enabled: false,
    } as any);
    vi.mocked(getSystemConnectionStatusWithPublishing).mockResolvedValue({
      reciterReuseRuntimeGate: true,
      bufferFacebook: { connected: true, channelConfigured: true },
    } as any);
    vi.mocked(isPublishNowAllowed).mockReturnValue(true);
    vi.mocked(getPublishPlatformBlockReason).mockReturnValue(null);
    vi.mocked(triggerWorkflowDispatch).mockResolvedValue({
      success: true,
      message: "Dispatched manual production.",
    });

    const req = createRequest({ action: "publish_now", publish_facebook: true, publish_instagram: false });
    const res = await POST(req);
    expect(res.status).toBe(400);
    expect((await res.json()).error).toMatch(/both Facebook and Instagram/);
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // 11. Publish Now with Instagram only
  it("rejects Instagram-only posting when the two-channel release is enabled", async () => {
    release.ready = true;
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(getStoredConfig).mockResolvedValue({
      reciter_reuse_confirmed: true,
      facebook_enabled: false,
      instagram_enabled: true,
    } as any);
    vi.mocked(getSystemConnectionStatusWithPublishing).mockResolvedValue({
      reciterReuseRuntimeGate: true,
      bufferInstagram: { connected: true, channelConfigured: true },
    } as any);
    vi.mocked(isPublishNowAllowed).mockReturnValue(true);
    vi.mocked(getPublishPlatformBlockReason).mockReturnValue(null);
    vi.mocked(triggerWorkflowDispatch).mockResolvedValue({
      success: true,
      message: "Dispatched manual production.",
    });

    const req = createRequest({ action: "publish_now", publish_facebook: false, publish_instagram: true });
    const res = await POST(req);
    expect(res.status).toBe(400);
    expect((await res.json()).error).toMatch(/both Facebook and Instagram/);
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();
  });

  // 12. Publish Now with both targets
  it("rejects when unreleased and dispatches both targets once when enabled", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    vi.mocked(getStoredConfig).mockResolvedValue({
      reciter_reuse_confirmed: true,
      facebook_enabled: true,
      instagram_enabled: true,
    } as any);
    vi.mocked(getSystemConnectionStatusWithPublishing).mockResolvedValue({
      reciterReuseRuntimeGate: true,
      bufferFacebook: { connected: true, channelConfigured: true },
      bufferInstagram: { connected: true, channelConfigured: true },
    } as any);
    vi.mocked(isPublishNowAllowed).mockReturnValue(true);
    vi.mocked(getPublishPlatformBlockReason).mockReturnValue(null);
    vi.mocked(triggerWorkflowDispatch).mockResolvedValue({
      success: true,
      message: "Dispatched manual production.",
    });

    const body = { action: "publish_now", publish_facebook: true, publish_instagram: true };
    const blocked = await POST(createRequest(body));
    expect(blocked.status).toBe(409);
    expect((await blocked.json()).code).toBe("OWNER_V1_PUBLISH_NOT_READY");
    expect(triggerWorkflowDispatch).not.toHaveBeenCalled();

    release.ready = true;
    const res = await POST(createRequest(body));
    expect(triggerWorkflowDispatch).toHaveBeenCalledTimes(1);
    expect(res.status).toBe(200);
    expect(triggerWorkflowDispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        runKind: "manual_production",
        publishFacebook: true,
        publishInstagram: true,
      })
    );
  });

  // 13. No secret exposure
  it("never exposes tokens, secrets, or internal keys in responses", async () => {
    vi.mocked(getAdminSession).mockResolvedValue(true);
    process.env.GITHUB_ACTIONS_TOKEN = "dummy_gh_token_for_test";
    process.env.BUFFER_API_KEY = "dummy_buffer_token_for_test";
    process.env.SUPABASE_SERVICE_ROLE_KEY = "dummy_supabase_token_for_test";

    vi.mocked(triggerBackgroundDiscovery).mockResolvedValue({
      success: true,
      message: "Background discovery triggered successfully.",
    });

    const req = createRequest({ action: "background_discovery" });
    const res = await POST(req);
    const text = await res.text();
    expect(text).not.toContain("dummy_gh_token_for_test");
    expect(text).not.toContain("dummy_buffer_token_for_test");
    expect(text).not.toContain("dummy_supabase_token_for_test");
  });
});
