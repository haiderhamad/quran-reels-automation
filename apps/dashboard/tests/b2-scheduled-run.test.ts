import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { send } = vi.hoisted(() => ({ send: vi.fn() }));

vi.mock("@aws-sdk/client-s3", () => ({
  S3Client: vi.fn().mockImplementation(() => ({ send })),
  GetObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
  PutObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
  ListObjectsV2Command: vi.fn().mockImplementation((args) => ({ ...args })),
  HeadObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
  DeleteObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
}));

vi.mock("@aws-sdk/s3-request-presigner", () => ({ getSignedUrl: vi.fn() }));

import { getScheduledRunRecord } from "../lib/b2";

const record = {
  schema_version: 1,
  run_id: "legacy-morning",
  date: "2026-08-24",
  slot: "morning",
  status: "SUCCESS",
  started_at: "2026-08-24T06:00:00Z",
};

describe("scheduled run record compatibility lookup", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = {
      ...originalEnv,
      B2_ENDPOINT: "https://s3.example.invalid",
      B2_REGION: "test-region",
      B2_APPLICATION_KEY_ID: "test-key-id",
      B2_APPLICATION_KEY: "test-key",
      B2_BUCKET_NAME: "test-bucket",
      B2_PREFIX: "quran-reels/",
    };
    send.mockReset();
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("falls back to the legacy morning key when the generic key is absent", async () => {
    send
      .mockRejectedValueOnce({ name: "NoSuchKey", $metadata: { httpStatusCode: 404 } })
      .mockResolvedValueOnce({
        Body: { transformToString: async () => JSON.stringify(record) },
      });

    await expect(getScheduledRunRecord("2026-08-24", "morning")).resolves.toMatchObject({
      run_id: "legacy-morning",
    });
    expect(send.mock.calls.map(([command]) => command.Key)).toEqual([
      "quran-reels/runs/2026-08-24/scheduled/morning.json",
      "quran-reels/runs/2026-08-24/morning.json",
    ]);
  });

  it("does not probe a legacy namespace for a dynamic stable slot", async () => {
    send.mockRejectedValueOnce({ name: "NoSuchKey", $metadata: { httpStatusCode: 404 } });

    await expect(getScheduledRunRecord("2026-08-24", "stable-slot")).resolves.toBeNull();
    expect(send.mock.calls.map(([command]) => command.Key)).toEqual([
      "quran-reels/runs/2026-08-24/scheduled/stable-slot.json",
    ]);
  });
});
