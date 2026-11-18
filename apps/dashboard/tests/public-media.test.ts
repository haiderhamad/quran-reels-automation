import { describe, expect, it } from "vitest";
import { RunRecordSchema } from "@/lib/schema";
import { canServePublicMedia } from "@/lib/public-media";

const deliveryId = "deliverydeliverydeliverydelivery";
const record = RunRecordSchema.parse({
  run_id: "manual_20260902_abcdef12",
  date: "2026-09-02",
  slot: "manual-production",
  run_kind: "manual_production" as const,
  status: "SUCCESS" as const,
  started_at: "2026-09-02T10:00:00Z",
  media_key: "quran-reels/media/manual-production/manual_20260902_abcdef12.mp4",
  final_safety_passed: true,
  public_media_delivery_id: deliveryId,
});

describe("public final-safe media eligibility", () => {
  it("permits only the matching final-safe MP4 under the media prefix", () => {
    expect(canServePublicMedia(record, record.run_id, deliveryId, "quran-reels/")).toBe(true);
  });

  it("never exposes state JSON, failed media, or the wrong delivery capability", () => {
    expect(canServePublicMedia({ ...record, media_key: "quran-reels/runs/test/a.json" }, record.run_id, deliveryId, "quran-reels/")).toBe(false);
    expect(canServePublicMedia({ ...record, final_safety_passed: false }, record.run_id, deliveryId, "quran-reels/")).toBe(false);
    expect(canServePublicMedia(record, record.run_id, `${deliveryId}x`, "quran-reels/")).toBe(false);
  });
});
