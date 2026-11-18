import { describe, it, expect, beforeEach, afterEach } from "vitest";
import bcrypt from "bcryptjs";
import {
  verifyAdminPassword,
  createSessionToken,
  verifySessionToken,
} from "../lib/auth";

describe("Authentication & Session Logic", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
    process.env.SESSION_SECRET = "dummy_session_secret_32_characters_long_min";
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("verifies plaintext password against valid bcrypt hash", async () => {
    const salt = await bcrypt.genSalt(10);
    const hash = await bcrypt.hash("correct-horse-battery-staple", salt);
    process.env.ADMIN_PASSWORD_HASH = hash;

    const valid = await verifyAdminPassword("correct-horse-battery-staple");
    expect(valid).toBe(true);

    const invalid = await verifyAdminPassword("wrong-password");
    expect(invalid).toBe(false);
  });

  it("returns false if ADMIN_PASSWORD_HASH is not configured", async () => {
    delete process.env.ADMIN_PASSWORD_HASH;
    const result = await verifyAdminPassword("some-password");
    expect(result).toBe(false);
  });

  it("creates and verifies stateless JWT session token", async () => {
    const token = await createSessionToken();
    expect(typeof token).toBe("string");
    expect(token.length).toBeGreaterThan(20);

    const isValid = await verifySessionToken(token);
    expect(isValid).toBe(true);
  });

  it("rejects invalid, corrupted, or tampered token", async () => {
    const isValidInvalid = await verifySessionToken("not-a-jwt");
    expect(isValidInvalid).toBe(false);

    const token = await createSessionToken();
    const tampered = token.slice(0, -5) + "abcde";
    const isValidTampered = await verifySessionToken(tampered);
    expect(isValidTampered).toBe(false);
  });
});
