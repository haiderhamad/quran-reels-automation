import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LIVE_TEST_WRITE_OPT_IN_ENV,
  saveStoredConfigToSupabase,
} from "../lib/supabase-state";

describe("Supabase Dashboard test isolation", () => {
  const originalEnv = process.env;

  afterEach(() => {
    process.env = originalEnv;
    vi.unstubAllGlobals();
  });

  it("blocks a production-looking configured PostgREST write before fetch", async () => {
    process.env = {
      ...originalEnv,
      NODE_ENV: "test",
      SUPABASE_URL: "https://production-looking.supabase.co",
      SUPABASE_SERVICE_ROLE_KEY: "production-looking-secret",
      [LIVE_TEST_WRITE_OPT_IN_ENV]: "",
    };
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(saveStoredConfigToSupabase({})).rejects.toThrow(
      "Live Supabase access is blocked during Dashboard tests"
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
