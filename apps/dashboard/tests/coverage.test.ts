import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { buildCoverageKey, getCoverageSummary } from "../lib/b2";

vi.mock("@aws-sdk/client-s3", () => {
  const mockSend = vi.fn();
  return {
    S3Client: vi.fn().mockImplementation(() => ({
      send: mockSend,
    })),
    GetObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
    PutObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
    HeadObjectCommand: vi.fn().mockImplementation((args) => ({ ...args })),
    ListObjectsV2Command: vi.fn().mockImplementation((args) => ({ ...args })),
  };
});

describe("Quran Coverage Dashboard Integration (Backblaze B2)", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = {
      ...originalEnv,
      B2_ENDPOINT: "https://s3.eu-central-003.backblazeb2.com",
      B2_REGION: "eu-central-003",
      B2_APPLICATION_KEY_ID: "key123",
      B2_APPLICATION_KEY: "sec123",
      B2_BUCKET_NAME: "test-bucket",
      B2_PREFIX: "quran-reels/",
    };
    vi.clearAllMocks();
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it("builds canonical coverage key with prefix", () => {
    expect(buildCoverageKey("quran-reels/")).toBe("quran-reels/coverage/current.json");
    expect(buildCoverageKey("custom/prefix/")).toBe("custom/prefix/coverage/current.json");
    expect(buildCoverageKey("")).toBe("coverage/current.json");
  });

  it("returns default Cycle 1, 0/6236 summary when B2 is unconfigured", async () => {
    delete process.env.B2_ENDPOINT;
    delete process.env.B2_BUCKET_NAME;

    const { summary, error } = await getCoverageSummary();
    expect(error).toBeUndefined();
    expect(summary.cycleNumber).toBe(1);
    expect(summary.totalAyahs).toBe(6236);
    expect(summary.coveredCount).toBe(0);
    expect(summary.remainingCount).toBe(6236);
    expect(summary.progressPercentage).toBe(0.0);
    expect(summary.isComplete).toBe(false);
  });

  it("returns default Cycle 1, 0/6236 summary when coverage file does not exist (NoSuchKey)", async () => {
    const s3Instance = new S3Client({});
    (s3Instance.send as any).mockRejectedValueOnce({
      name: "NoSuchKey",
      $metadata: { httpStatusCode: 404 },
    });

    const { summary, error } = await getCoverageSummary();
    expect(error).toBeUndefined();
    expect(summary.cycleNumber).toBe(1);
    expect(summary.totalAyahs).toBe(6236);
    expect(summary.coveredCount).toBe(0);
    expect(summary.remainingCount).toBe(6236);
  });

  it("parses valid CoverageState correctly and calculates remaining count", async () => {
    const validCoverage = {
      cycle_number: 1,
      total_ayahs: 6236,
      covered_count: 50,
      covered_ayahs: ["1:1", "1:2", "1:3"],
      started_at: "2026-08-23T06:00:00Z",
      last_updated_at: "2026-08-23T18:00:00Z",
    };

    const s3Instance = new S3Client({});
    (s3Instance.send as any).mockResolvedValueOnce({
      Body: {
        transformToString: async () => JSON.stringify(validCoverage),
      },
    });

    const { summary, error } = await getCoverageSummary();
    expect(error).toBeUndefined();
    expect(summary.cycleNumber).toBe(1);
    expect(summary.totalAyahs).toBe(6236);
    expect(summary.coveredCount).toBe(50);
    expect(summary.remainingCount).toBe(6186);
    expect(summary.progressPercentage).toBe(0.8);
    expect(summary.isComplete).toBe(false);
    expect(summary.startedAt).toBe("2026-08-23T06:00:00Z");
    expect(summary.lastUpdatedAt).toBe("2026-08-23T18:00:00Z");
  });

  it("returns error message on corrupted or non-schema JSON payload", async () => {
    const s3Instance = new S3Client({});
    (s3Instance.send as any).mockResolvedValueOnce({
      Body: {
        transformToString: async () => "invalid json {",
      },
    });

    const { summary, error } = await getCoverageSummary();
    expect(error).toBeDefined();
    expect(error).toContain("Could not verify coverage state");
    expect(summary.cycleNumber).toBe(1);
    expect(summary.coveredCount).toBe(0);
  });
});
