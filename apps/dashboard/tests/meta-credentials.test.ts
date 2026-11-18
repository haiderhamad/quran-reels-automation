import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  decryptMetaCredentials,
  encryptMetaCredentials,
  safeMetaCredentialStatus,
  verifyOAuthState,
  type MetaCredentialPayload,
} from "../lib/meta-credentials";
import {
  buildMetaOAuthUrl,
  exchangeMetaCode,
  resolveMetaRedirectOrigin,
  testMetaConnection,
} from "../lib/meta-api";

const KEY = Buffer.alloc(32, 7).toString("base64");
const WRONG_KEY = Buffer.alloc(32, 8).toString("base64");
const TOKEN = ["page", "access", "token", "secret"].join("_");

function payload(): MetaCredentialPayload {
  return {
    schema_version: 1,
    connected_at: "2026-08-24T00:00:00.000Z",
    graph_api_version: "v26.0",
    user_id: "user-1",
    page_id: "page-1",
    page_name: "Quran Page",
    page_access_token: TOKEN,
    scopes: ["pages_manage_posts"],
    token_expires_at: null,
    instagram: null,
  };
}

describe("encrypted Meta credentials", () => {
  it("round-trips with AES-256-GCM and exposes only safe status", () => {
    const envelope = encryptMetaCredentials(payload(), KEY);
    expect(JSON.stringify(envelope)).not.toContain(TOKEN);
    expect(decryptMetaCredentials(envelope, KEY)).toEqual(payload());
    const status = safeMetaCredentialStatus(payload());
    expect(JSON.stringify(status)).not.toContain(TOKEN);
    expect(status.page).toEqual({ id: "page-1", name: "Quran Page" });
  });

  it("fails authentication with the wrong key", () => {
    const envelope = encryptMetaCredentials(payload(), KEY);
    expect(() => decryptMetaCredentials(envelope, WRONG_KEY)).toThrow();
  });

  it("compares OAuth state exactly", () => {
    const state = "state_abc123";
    expect(verifyOAuthState(state, state)).toBe(true);
    expect(verifyOAuthState(state, "state_other")).toBe(false);
    expect(verifyOAuthState("", state)).toBe(false);
  });
});

describe("Meta account discovery", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    const syntheticAppSecret = ["synthetic", "app", "fixture"].join("-");
    process.env = {
      ...originalEnv,
      META_APP_ID: "app-1",
      META_APP_SECRET: syntheticAppSecret,
      META_API_VERSION: "v26.0",
    };
    delete process.env.META_PAGE_ID;
    delete process.env.META_TARGET_PAGE_ID;
    delete process.env.META_LOGIN_CONFIG_ID;
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("stores the Page token and linked Instagram metadata without returning user tokens", async () => {
    const appSecret = process.env.META_APP_SECRET || "";
    const shortUserToken = ["short", "user", "fixture"].join("-");
    const longUserToken = ["long", "user", "fixture"].join("-");
    const fetchFn = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: shortUserToken }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: longUserToken, expires_in: 3600 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{
        id: "page-1",
        name: "Quran Page",
        access_token: TOKEN,
        instagram_business_account: { id: "ig-1", username: "quran" },
      }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "user-1" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "instagram_basic",
        "instagram_content_publish",
      ].map((permission) => ({ permission, status: "granted" })) }), { status: 200 }));

    const credentials = await exchangeMetaCode("code-1", "https://example.test/api/meta/callback", fetchFn);

    expect(credentials.page_access_token).toBe(TOKEN);
    expect(credentials.instagram?.id).toBe("ig-1");
    expect(fetchFn.mock.calls[0][0].toString()).not.toContain(appSecret);
    expect(fetchFn.mock.calls[1][0].toString()).not.toContain(shortUserToken);
    expect(JSON.stringify(credentials)).not.toContain(shortUserToken);
    expect(JSON.stringify(credentials)).not.toContain(longUserToken);
    expect(fetchFn.mock.calls[2][0].toString()).not.toContain(longUserToken);
    expect(fetchFn.mock.calls[3][0].toString()).not.toContain(longUserToken);
    expect(fetchFn.mock.calls[4][0].toString()).not.toContain(longUserToken);
  });

  it("uses Facebook Login for Business config without duplicating scope", () => {
    process.env.META_LOGIN_CONFIG_ID = "login-config-1";
    const url = new URL(buildMetaOAuthUrl("https://example.test/api/meta/callback", "csrf-state"));
    expect(url.searchParams.get("config_id")).toBe("login-config-1");
    expect(url.searchParams.get("state")).toBe("csrf-state");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.has("scope")).toBe(false);
  });

  it("allows localhost and the active Vercel host but rejects an untrusted host", () => {
    expect(resolveMetaRedirectOrigin("http://localhost:3000/api/meta/connect")).toBe("http://localhost:3000");
    process.env.VERCEL_URL = "preview-quran.vercel.app";
    expect(resolveMetaRedirectOrigin("https://preview-quran.vercel.app/api/meta/connect")).toBe(
      "https://preview-quran.vercel.app"
    );
    expect(() => resolveMetaRedirectOrigin("https://attacker.example/api/meta/connect")).toThrow();
  });

  it("keeps Facebook healthy when no linked Instagram account exists", async () => {
    const fetchFn = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "page-1" }), { status: 200 })
    );
    await expect(testMetaConnection(payload(), fetchFn)).resolves.toEqual({
      facebook: true,
      instagram: false,
    });
  });

  it("reports an invalid or expired Page credential without publishing", async () => {
    const fetchFn = vi.fn().mockResolvedValue(new Response("unauthorized", { status: 401 }));
    await expect(testMetaConnection(payload(), fetchFn)).resolves.toEqual({
      facebook: false,
      instagram: false,
    });
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
  });
});
