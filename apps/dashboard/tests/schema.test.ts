import { describe, it, expect } from "vitest";
import {
  StoredConfigSchema,
  RunRecordSchema,
  CoverageStateSchema,
  CURRENT_SCHEMA_VERSION,
} from "../lib/schema";

describe("StoredConfigSchema", () => {
  it("parses valid default configuration", () => {
    const parsed = StoredConfigSchema.parse({});
    expect(parsed.schema_version).toBe(CURRENT_SCHEMA_VERSION);
    expect(parsed.automation_enabled).toBe(false);
    expect(parsed.timezone).toBe("Europe/Berlin");
    expect(parsed.morning_enabled).toBe(true);
    expect(parsed.evening_enabled).toBe(true);
    expect(parsed.translation_languages).toEqual(["en"]);
    expect(parsed.min_duration_seconds).toBe(15.0);
    expect(parsed.max_duration_seconds).toBe(45.0);
    expect(parsed.background_clip_strategy).toBe("auto_prefer_single");
    expect(parsed.max_background_clips).toBe(3);
    expect(parsed.target_background_clips).toBe(1);
    expect(parsed.facebook_enabled).toBe(true);
    expect(parsed.instagram_enabled).toBe(true);
    // Subtitle & visual style defaults
    expect(parsed.subtitle_mode).toBe("progressive_phrase_sync");
    expect(parsed.arabic_font_size).toBe(180);
    expect(parsed.translation_font_size).toBe(50);
    expect(parsed.arabic_max_lines).toBe(2);
    expect(parsed.translation_max_lines).toBe(2);
    expect(parsed.arabic_translation_gap_px).toBe(20);
    expect(parsed.translation_stack_gap_px).toBe(10);
    expect(parsed.publishing_schedule).toEqual([
      { id: "morning", time: "08:00", enabled: false },
      { id: "evening", time: "20:00", enabled: false },
    ]);
    expect(parsed.arabic_line_spacing).toBe(1.15);
    expect(parsed.translation_line_spacing).toBe(1.25);
    expect(parsed.text_outline).toBe("minimal");
    expect(parsed.text_shadow).toBe("very_soft");
    expect(parsed.highlight_mode).toBe("phrase_highlight");
    expect(parsed.highlight_intensity).toBe("normal");
    expect(parsed.highlight_color).toBe("warm_white");
    expect(parsed.transition_duration_ms).toBe(100);
    expect(parsed.subtitle_lead_ms).toBe(0);
    expect(parsed.header_style).toBe("minimal_capsule");
    expect(parsed.header_content).toBe("arabic_translation_ayah_range");
    expect(parsed.header_backing_intensity).toBe("low");
    expect(parsed.ayah_reference_position).toBe("top_header");
    expect(parsed.subtitle_backing).toBe("shadow_only");
  });

  it("parses valid custom subtitle settings", () => {
    const data = {
      subtitle_mode: "static_full_ayah",
      arabic_font_size: 104,
      translation_font_size: 40,
      arabic_max_lines: 3,
      translation_max_lines: 3,
      arabic_translation_gap_px: 10,
      text_outline: "thin",
      text_shadow: "medium",
      highlight_intensity: "strong",
      header_style: "soft_glass",
      subtitle_backing: "soft_translucent",
    };
    const parsed = StoredConfigSchema.parse(data);
    expect(parsed.subtitle_mode).toBe("static_full_ayah");
    expect(parsed.arabic_font_size).toBe(104);
    expect(parsed.text_outline).toBe("thin");
    expect(parsed.text_shadow).toBe("medium");
    expect(parsed.highlight_intensity).toBe("strong");
    expect(parsed.header_style).toBe("soft_glass");
    expect(parsed.subtitle_backing).toBe("soft_translucent");
  });

  it("rejects invalid subtitle settings out of bounds", () => {
    expect(() => StoredConfigSchema.parse({ arabic_font_size: 60 })).toThrow();
    expect(() => StoredConfigSchema.parse({ arabic_font_size: 190 })).toThrow();
    expect(() => StoredConfigSchema.parse({ translation_font_size: 20 })).toThrow();
    expect(() => StoredConfigSchema.parse({ translation_font_size: 81 })).toThrow();
    expect(() => StoredConfigSchema.parse({ text_outline: "heavy" })).toThrow();
    expect(() => StoredConfigSchema.parse({ text_shadow: "ultra" })).toThrow();
    expect(() => StoredConfigSchema.parse({ highlight_intensity: "extreme" })).toThrow();
    expect(() => StoredConfigSchema.parse({ transition_duration_ms: 50 })).toThrow();
  });

  it("parses valid custom times and duration", () => {
    const data = {
      morning_time: "07:15",
      evening_time: "19:30",
      min_duration_seconds: 12.0,
      max_duration_seconds: 45.0,
      target_background_clips: 2,
    };
    const parsed = StoredConfigSchema.parse(data);
    expect(parsed.morning_time).toBe("07:15");
    expect(parsed.evening_time).toBe("19:30");
    expect(parsed.min_duration_seconds).toBe(12.0);
    expect(parsed.max_duration_seconds).toBe(45.0);
    expect(parsed.target_background_clips).toBe(2);
  });

  it("rejects invalid time formats (not HH:MM 24h)", () => {
    expect(() => StoredConfigSchema.parse({ morning_time: "7:15" })).toThrow();
    expect(() => StoredConfigSchema.parse({ morning_time: "24:00" })).toThrow();
    expect(() => StoredConfigSchema.parse({ morning_time: "12:60" })).toThrow();
    expect(() => StoredConfigSchema.parse({ evening_time: "invalid" })).toThrow();
  });

  it("rejects min_duration > max_duration", () => {
    expect(() =>
      StoredConfigSchema.parse({
        min_duration_seconds: 25.0,
        max_duration_seconds: 15.0,
      })
    ).toThrow();
  });

  it("rejects duration out of bounds (<10s or >60s)", () => {
    expect(() => StoredConfigSchema.parse({ min_duration_seconds: 5.0 })).toThrow();
    expect(() => StoredConfigSchema.parse({ max_duration_seconds: 60.5 })).toThrow();
    expect(() => StoredConfigSchema.parse({ max_duration_seconds: 65.0 })).toThrow();
  });

  it("accepts valid max_duration values (30, 45, 50, 60 seconds)", () => {
    for (const maxSec of [30.0, 45.0, 50.0, 60.0]) {
      const parsed = StoredConfigSchema.parse({
        min_duration_seconds: 10.0,
        max_duration_seconds: maxSec,
      });
      expect(parsed.max_duration_seconds).toBe(maxSec);
    }
  });

  it("verifies SettingsForm Max Duration input HTML constraints (min=10, max=60, step=0.5)", () => {
    const fs = require("fs");
    const path = require("path");
    const content = fs.readFileSync(
      path.resolve(__dirname, "../components/SettingsForm.tsx"),
      "utf8"
    );
    expect(content).toMatch(/id="max-duration"[\s\S]*?min="10"[\s\S]*?max="60"[\s\S]*?step="0.5"/);
    expect(content).toContain("Allowed: 10.0 to 60.0s (normal range)");
    expect(content).toMatch(/id="min-duration"[\s\S]*?min="10"[\s\S]*?max="30"[\s\S]*?step="0.5"/);
    expect(content).toContain("Allowed: 10.0 to 30.0s");
  });

  it("rejects target_background_clips out of bounds (<1 or >3)", () => {
    expect(() => StoredConfigSchema.parse({ target_background_clips: 0 })).toThrow();
    expect(() => StoredConfigSchema.parse({ target_background_clips: 4 })).toThrow();
  });

  it("supports English, German, and ordered English plus German", () => {
    expect(StoredConfigSchema.parse({ translation_languages: ["en"] }).translation_languages).toEqual(["en"]);
    expect(StoredConfigSchema.parse({ translation_languages: ["de"] }).translation_languages).toEqual(["de"]);
    expect(StoredConfigSchema.parse({ translation_languages: ["en", "de"] }).translation_languages).toEqual(["en", "de"]);
    expect(() => StoredConfigSchema.parse({ translation_languages: ["de", "en"] })).toThrow();
  });

  it("accepts final stack gaps and migrates legacy values with or without a generic schedule", () => {
    for (const gap of [10, 20, 30]) {
      expect(StoredConfigSchema.parse({ translation_stack_gap_px: gap }).translation_stack_gap_px).toBe(gap);
    }
    expect(StoredConfigSchema.parse({ translation_stack_gap_px: 4 }).translation_stack_gap_px).toBe(10);
    expect(StoredConfigSchema.parse({ translation_stack_gap_px: 6 }).translation_stack_gap_px).toBe(20);
    expect(StoredConfigSchema.parse({
      translation_stack_gap_px: 8,
      publishing_schedule: [{ id: "stable", time: "08:00", enabled: true }],
    }).translation_stack_gap_px).toBe(30);
    expect(() => StoredConfigSchema.parse({ translation_stack_gap_px: 12 })).toThrow();
  });

  it("accepts final arabic gaps and migrates legacy values", () => {
    for (const gap of [20, 40, 60]) {
      expect(StoredConfigSchema.parse({ arabic_translation_gap_version: 2, arabic_translation_gap_px: gap }).arabic_translation_gap_px).toBe(gap);
    }
    expect(StoredConfigSchema.parse({ arabic_translation_gap_px: 6 }).arabic_translation_gap_px).toBe(20);
    expect(StoredConfigSchema.parse({ arabic_translation_gap_px: 8 }).arabic_translation_gap_px).toBe(40);
    expect(() => StoredConfigSchema.parse({ arabic_translation_gap_px: 7 })).toThrow();
  });

  it("accepts final translation font sizes and migrates legacy values", () => {
    for (const size of [50, 40, 30]) {
      expect(StoredConfigSchema.parse({ translation_font_size: size }).translation_font_size).toBe(size);
    }
    expect(StoredConfigSchema.parse({ translation_font_size: 60 }).translation_font_size).toBe(50);
    expect(StoredConfigSchema.parse({ translation_font_size: 55 }).translation_font_size).toBe(40);
    expect(StoredConfigSchema.parse({ translation_font_size: 50, arabic_translation_gap_px: 6 }).translation_font_size).toBe(30);
    expect(() => StoredConfigSchema.parse({ translation_font_size: 45 })).toThrow();
  });

  it("migrates legacy morning and evening settings to stable schedule IDs", () => {
    const parsed = StoredConfigSchema.parse({
      morning_enabled: true,
      morning_time: "07:30",
      evening_enabled: false,
      evening_time: "21:00",
    });
    expect(parsed.publishing_schedule).toEqual([
      { id: "morning", time: "07:30", enabled: true },
      { id: "evening", time: "21:00", enabled: false },
    ]);
  });

  it("validates stable IDs, slot count, and duplicate active times", () => {
    expect(() => StoredConfigSchema.parse({ publishing_schedule: [] })).toThrow();
    expect(() => StoredConfigSchema.parse({ publishing_schedule: [
      { id: "one", time: "08:00", enabled: true },
      { id: "two", time: "08:00", enabled: true },
    ] })).toThrow();
    expect(StoredConfigSchema.parse({ publishing_schedule: [
      { id: "one", time: "08:00", enabled: true },
      { id: "two", time: "08:00", enabled: false },
    ] }).publishing_schedule).toHaveLength(2);
  });
});

describe("RunRecordSchema", () => {
  it("parses valid full run record matching Python storage.py", () => {
    const record = {
      run_id: "run_20260823_m_123",
      date: "2026-08-23",
      slot: "morning",
      status: "SUCCESS",
      started_at: "2026-08-23T06:00:00Z",
      completed_at: "2026-08-23T06:00:45Z",
      surah_number: 1,
      surah_name_ar: "الفاتحة",
      surah_name_en: "Al-Fatihah",
      first_ayah: 1,
      last_ayah: 7,
      duration_seconds: 22.5,
      pexels_video_ids: [1001, 1002, 1003],
      media_key: "quran-reels/media/2026-08-23/morning/run_123.mp4",
      final_safety_passed: true,
      facebook_status: "SUCCESS",
      instagram_status: "NOT_CONFIGURED",
      facebook_media_id: "fb_12345",
      instagram_failure_reason: "META_IG_USER_ID is not set",
      translation_languages: ["en"],
      translation_sources: {
        en: { language: "en", translation_key: "english_rwwad", source: "Ruwwad Translation Center via QuranEnc.com", version: "1.0.19" },
      },
      provenance: {
        reciter: "Yasser Al-Dosari",
      },
    };

    const parsed = RunRecordSchema.parse(record);
    expect(parsed.run_id).toBe("run_20260823_m_123");
    expect(parsed.facebook_status).toBe("SUCCESS");
    expect(parsed.instagram_status).toBe("NOT_CONFIGURED");
    expect(parsed.final_safety_passed).toBe(true);
    expect(parsed.translation_sources.en.translation_key).toBe("english_rwwad");
  });

  it("parses translation_sources with numeric integer last_update", () => {
    const record = {
      run_id: "run_20260904_num_last_update",
      date: "2026-09-04",
      slot: "manual-production",
      status: "SUCCESS" as const,
      started_at: "2026-09-04T05:00:00Z",
      translation_sources: {
        en: {
          language: "en" as const,
          translation_key: "english_rwwad",
          source: "Ruwwad Translation Center via QuranEnc.com",
          version: "1.0.19",
          last_update: 1773348325,
        },
      },
    };
    const parsed = RunRecordSchema.parse(record);
    expect(parsed.translation_sources.en.last_update).toBe(1773348325);
  });

  it("keeps Buffer acceptance distinct from confirmed platform publication", () => {
    const parsed = RunRecordSchema.parse({
      run_id: "run_20260903_buffer_accepted",
      date: "2026-09-03",
      slot: "manual-production",
      status: "SUCCESS",
      started_at: "2026-09-03T12:00:00Z",
      facebook_status: "ACCEPTED",
      publisher_backend: "buffer",
      buffer_post_id: "buffer_post_123",
      buffer_post_status: "sending",
      buffer_accepted_at: "2026-09-03T12:00:05Z",
    });
    expect(parsed.facebook_status).toBe("ACCEPTED");
    expect(parsed.buffer_post_status).toBe("sending");
    expect(parsed.buffer_accepted_at).toBe("2026-09-03T12:00:05Z");
  });

  it("rejects invalid status", () => {
    expect(() =>
      RunRecordSchema.parse({
        run_id: "r1",
        date: "2026-08-23",
        slot: "morning",
        status: "COMPLETED", // invalid, should be SUCCESS
        started_at: "2026-08-23T06:00:00Z",
      })
    ).toThrow();
  });

  it("rejects unsafe slot names", () => {
    expect(() =>
      RunRecordSchema.parse({
        run_id: "r1",
        date: "2026-08-23",
        slot: "../../afternoon",
        status: "SUCCESS",
        started_at: "2026-08-23T06:00:00Z",
      })
    ).toThrow();
  });

  it("rejects invalid publish status", () => {
    expect(() =>
      RunRecordSchema.parse({
        run_id: "r1",
        date: "2026-08-23",
        slot: "morning",
        status: "SUCCESS",
        started_at: "2026-08-23T06:00:00Z",
        facebook_status: "PENDING", // invalid
      })
    ).toThrow();
  });
});

describe("CoverageStateSchema", () => {
  it("parses default empty coverage state", () => {
    const parsed = CoverageStateSchema.parse({});
    expect(parsed.schema_version).toBe(CURRENT_SCHEMA_VERSION);
    expect(parsed.cycle_number).toBe(1);
    expect(parsed.total_ayahs).toBe(6236);
    expect(parsed.covered_count).toBe(0);
    expect(parsed.covered_ayahs).toEqual([]);
    expect(parsed.processed_run_ids).toEqual([]);
  });

  it("parses valid partial and full coverage records", () => {
    const data = {
      cycle_number: 1,
      total_ayahs: 6236,
      covered_count: 3118,
      covered_ayahs: ["1:1", "1:2", "2:255"],
      processed_run_ids: ["run_1"],
      started_at: "2026-08-23T06:00:00Z",
      last_updated_at: "2026-08-23T06:00:45Z",
    };
    const parsed = CoverageStateSchema.parse(data);
    expect(parsed.cycle_number).toBe(1);
    expect(parsed.covered_count).toBe(3118);
    expect(parsed.covered_ayahs).toHaveLength(3);
    expect(parsed.processed_run_ids).toEqual(["run_1"]);
  });

  it("rejects invalid schema version or out of bound counts", () => {
    expect(() => CoverageStateSchema.parse({ schema_version: 99 })).toThrow();
    expect(() => CoverageStateSchema.parse({ cycle_number: 0 })).toThrow();
    expect(() => CoverageStateSchema.parse({ covered_count: 7000 })).toThrow();
  });
});
