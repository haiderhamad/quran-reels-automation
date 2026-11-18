import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { getPreviewCatalogStatus, getPublishPlatformBlockReason, getSystemConnectionStatus, isPublishNowAllowed } from "../lib/system";

describe("System Health & Status Derivation (Backblaze B2)", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("derives connection status booleans correctly from environment", () => {
    process.env.B2_ENDPOINT = "https://s3.eu-central-003.backblazeb2.com";
    process.env.B2_REGION = "eu-central-003";
    process.env.B2_APPLICATION_KEY_ID = "key";
    process.env.B2_APPLICATION_KEY = "secret";
    process.env.B2_BUCKET_NAME = "bucket";

    process.env.META_PAGE_ID = "page_123";
    process.env.META_PAGE_ACCESS_TOKEN = "tok_123";
    delete process.env.META_IG_USER_ID;

    process.env.RECITER_REUSE_CONFIRMED = "false";

    const status = getSystemConnectionStatus();

    expect(status.b2).toBe(true);
    expect(status.metaFacebook).toBe(true);
    expect(status.metaInstagram).toBe(false); // Optional secondary not configured
    expect(status.reciterReuseRuntimeGate).toBe(false); // Fail-closed
    expect(status.englishTranslation).toBe(true);
    expect(status.germanTranslation).toBe(true);
    expect(status.audioYasserTiming).toBe(true);
  });

  it("reports scheduler readiness without exposing its secret", () => {
    const placeholderValue = ["scheduler", "test", "placeholder"].join("-");
    process.env.SCHEDULER_SECRET = placeholderValue;
    delete process.env.SCHEDULER_ACTIVE;
    const configured = getSystemConnectionStatus();
    expect(configured.scheduler).toBe("configured");
    expect(JSON.stringify(configured)).not.toContain(placeholderValue);
    process.env.SCHEDULER_ACTIVE = "true";
    expect(getSystemConnectionStatus().scheduler).toBe("active");
  });

  it("fails Publish Now closed unless both reciter rights gates are true", () => {
    expect(isPublishNowAllowed(false, false)).toBe(false);
    expect(isPublishNowAllowed(true, false)).toBe(false);
    expect(isPublishNowAllowed(false, true)).toBe(false);
    expect(isPublishNowAllowed(true, true)).toBe(true);
  });

  it("blocks manual production before dispatch when enabled Facebook OAuth is unavailable", () => {
    expect(getPublishPlatformBlockReason(true, true, false)).toContain("Facebook Page");
    expect(getPublishPlatformBlockReason(false, false, false)).toContain("at least one");
    expect(getPublishPlatformBlockReason(true, false, true)).toBeNull();
    expect(getPublishPlatformBlockReason(false, true, false)).toContain("Instagram");
  });

  it("reports the committed static preview catalog as 45/45 ready", () => {
    expect(getPreviewCatalogStatus()).toBe("ready");
    expect(getSystemConnectionStatus().previewCatalog).toBe("ready");
  });

  it("evaluates reciter rights gate true when set to 'true', '1', or 'yes'", () => {
    process.env.RECITER_REUSE_CONFIRMED = "true";
    expect(getSystemConnectionStatus().reciterReuseRuntimeGate).toBe(true);

    process.env.RECITER_REUSE_CONFIRMED = "1";
    expect(getSystemConnectionStatus().reciterReuseRuntimeGate).toBe(true);

    process.env.RECITER_REUSE_CONFIRMED = "yes";
    expect(getSystemConnectionStatus().reciterReuseRuntimeGate).toBe(true);

    process.env.RECITER_REUSE_CONFIRMED = "false";
    expect(getSystemConnectionStatus().reciterReuseRuntimeGate).toBe(false);
  });
});
