import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  getStateAuthority,
  getConfigLoadState,
  getCoverageSummary,
  getRunRecord,
  claimSchedulerSlot,
} from "../lib/state-authority";

describe("State Authority Selector (Next.js Dashboard)", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    vi.resetModules();
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("defaults to 'b2' when STATE_AUTHORITY is unset or empty", () => {
    delete process.env.STATE_AUTHORITY;
    expect(getStateAuthority()).toBe("b2");

    process.env.STATE_AUTHORITY = "   ";
    expect(getStateAuthority()).toBe("b2");
  });

  it("returns 'supabase' when STATE_AUTHORITY=supabase", () => {
    process.env.STATE_AUTHORITY = "supabase";
    expect(getStateAuthority()).toBe("supabase");

    process.env.STATE_AUTHORITY = " SUPABASE  ";
    expect(getStateAuthority()).toBe("supabase");
  });

  it("fails closed on invalid STATE_AUTHORITY values", () => {
    process.env.STATE_AUTHORITY = "redis";
    expect(() => getStateAuthority()).toThrow(/Invalid STATE_AUTHORITY/);

    process.env.STATE_AUTHORITY = "postgres";
    expect(() => getStateAuthority()).toThrow(/Invalid STATE_AUTHORITY/);
  });
});
