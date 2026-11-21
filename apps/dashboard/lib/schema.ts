import { z } from "zod";
import reelLayout from "../reel-layout.json";
import { TOTAL_CANONICAL_AYAHS } from "./canonical-quran";

export const CURRENT_SCHEMA_VERSION = 1;
export const VALID_SLOTS = ["morning", "evening", "test"] as const;
export const VALID_RUN_KINDS = ["test", "manual_production", "scheduled"] as const;
export const VALID_STATUSES = ["STARTED", "SUCCESS", "FAILED"] as const;
export const VALID_PUBLISH_STATUSES = [
  "NOT_ATTEMPTED",
  "ACCEPTED",
  "SUCCESS",
  "FAILED",
  "BLOCKED",
  "UNKNOWN",
  "NOT_CONFIGURED",
] as const;

export const TIME_REGEX = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
export const DATE_REGEX = /^\d{4}-\d{2}-\d{2}$/;
export const SLOT_ID_REGEX = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/;

export const PublishingScheduleEntrySchema = z.object({
  id: z.string().regex(SLOT_ID_REGEX, "Schedule slot ID contains invalid characters."),
  time: z.string().regex(TIME_REGEX, "Schedule time must be in HH:MM (24-hour) format."),
  enabled: z.boolean().default(true),
});

export type PublishingScheduleEntry = z.infer<typeof PublishingScheduleEntrySchema>;

function migrateLegacyConfig(value: unknown): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const raw = value as Record<string, unknown>;
  const legacyStackGapMap: Record<number, number> = { 4: 80, 10: 80, 6: 100, 20: 100, 8: 120, 30: 120 };
  const rawStackGap = raw.translation_stack_gap_px;
  const gapVersion = raw.arabic_translation_gap_version === undefined ? 1 : raw.arabic_translation_gap_version;
  const arabicGapPresets: Record<number, number> = { 20: 80, 40: 100, 60: 120 };
  const legacyArabicGapPresets: Record<number, number> = {
    ...arabicGapPresets, 6: 80, 8: 100, 10: 80, 15: 100, 20: 120,
  };
  let arabicGap = raw.arabic_translation_gap_px;
  if ((gapVersion === 1 || gapVersion === 2) && typeof arabicGap === "number") {
    const presets = gapVersion === 1 ? legacyArabicGapPresets : arabicGapPresets;
    // Keep unsupported historical values invalid instead of inferring a preset.
    arabicGap = presets[arabicGap] ?? null;
  }
  const hasLegacyIndicators =
    raw.arabic_translation_gap_px === 6 ||
    raw.arabic_translation_gap_px === 8 ||
    raw.translation_stack_gap_px === 4 ||
    raw.translation_stack_gap_px === 6 ||
    raw.translation_stack_gap_px === 8 ||
    raw.translation_font_size === 60 ||
    raw.translation_font_size === 55 ||
    (raw.morning_time !== undefined && !Array.isArray(raw.publishing_schedule));

  const rawTranslationFontSize = typeof raw.translation_font_size === "number"
    ? raw.translation_font_size
    : undefined;
  let migratedFontSize = rawTranslationFontSize;
  if (rawTranslationFontSize === 60) {
    migratedFontSize = 50;
  } else if (rawTranslationFontSize === 55) {
    migratedFontSize = 40;
  } else if (rawTranslationFontSize === 50 && hasLegacyIndicators) {
    migratedFontSize = 30;
  }

  const migrated = {
    ...raw,
    ...(migratedFontSize !== undefined ? { translation_font_size: migratedFontSize } : {}),
    arabic_translation_gap_px: arabicGap,
    arabic_translation_gap_version: gapVersion === 1 || gapVersion === 2 ? 3 : gapVersion,
    translation_stack_gap_px: typeof rawStackGap === "number" ? legacyStackGapMap[rawStackGap] ?? rawStackGap : rawStackGap,
  };
  if (Array.isArray(raw.publishing_schedule)) return migrated;

  const schedule: PublishingScheduleEntry[] = [];
  const morningTime = typeof raw.morning_time === "string" && TIME_REGEX.test(raw.morning_time)
    ? raw.morning_time
    : "08:00";
  const eveningTime = typeof raw.evening_time === "string" && TIME_REGEX.test(raw.evening_time)
    ? raw.evening_time
    : "20:00";

  schedule.push({
    id: "morning",
    time: morningTime,
    enabled: raw.morning_time != null && raw.morning_enabled !== false,
  });
  schedule.push({
    id: "evening",
    time: eveningTime,
    enabled: raw.evening_time != null && raw.evening_enabled !== false,
  });
  return { ...migrated, publishing_schedule: schedule };
}

const StoredConfigObjectSchema = z
  .object({
    schema_version: z.number().int().refine((v) => v === CURRENT_SCHEMA_VERSION, {
      message: `Unsupported config schema_version. Expected ${CURRENT_SCHEMA_VERSION}`,
    }).default(CURRENT_SCHEMA_VERSION),
    automation_enabled: z.boolean().default(false),
    timezone: z.literal("Europe/Berlin").default("Europe/Berlin"),
    morning_enabled: z.boolean().default(true),
    morning_time: z
      .string()
      .regex(TIME_REGEX, "morning_time must be in HH:MM (24-hour) format")
      .nullable()
      .optional(),
    evening_enabled: z.boolean().default(true),
    evening_time: z
      .string()
      .regex(TIME_REGEX, "evening_time must be in HH:MM (24-hour) format")
      .nullable()
      .optional(),
    publishing_schedule: z
      .array(PublishingScheduleEntrySchema)
      .min(1, "At least one publishing time is required.")
      .max(8, "A maximum of eight publishing times is supported."),
    translation_languages: z
      .array(z.enum(["en", "de"]))
      .min(1, "translation_languages cannot be empty")
      .max(2)
      .refine((langs) => new Set(langs).size === langs.length, {
        message: "translation_languages cannot contain duplicates.",
      })
      .refine((langs) => !(langs.includes("en") && langs.includes("de")) || langs.join(",") === "en,de", {
        message: "When both translations are enabled, the order must be English then German.",
      })
      .default(["en"]),
    min_duration_seconds: z
      .number()
      .min(10.0, "min_duration_seconds must be at least 10.0")
      .max(60.0, "min_duration_seconds cannot exceed 60.0")
      .default(15.0),
    max_duration_seconds: z
      .number()
      .min(10.0, "max_duration_seconds must be at least 10.0")
      .max(60.0, "max_duration_seconds cannot exceed 60.0")
      .default(45.0),
    background_clip_strategy: z
      .enum(["auto_prefer_single", "single", "multiple"])
      .default("auto_prefer_single"),
    max_background_clips: z
      .number()
      .int()
      .min(1, "max_background_clips must be between 1 and 3")
      .max(3, "max_background_clips must be between 1 and 3")
      .default(3),
    target_background_clips: z
      .number()
      .int()
      .min(1, "target_background_clips must be between 1 and 3")
      .max(3, "target_background_clips must be between 1 and 3")
      .default(1),
    background_queries: z.array(z.string()).default([]),
    instagram_enabled: z.boolean().default(true),
    facebook_enabled: z.boolean().default(true),
    reciter_reuse_confirmed: z.boolean().default(false),
    // Subtitle & Visual Style (Premium Quran Reel preset defaults)
    subtitle_mode: z
      .enum(["progressive_phrase_sync", "static_full_ayah", "progressive_word_highlight"])
      .default("progressive_phrase_sync"),
    arabic_font_size: z.number().int().min(72).max(180).default(180),
    translation_font_size: z.union([z.literal(50), z.literal(40), z.literal(30)]).default(50),
    arabic_max_lines: z.number().int().min(1).max(5).default(2),
    // Older installations accepted 4 or 5 for this formerly shared limit.
    translation_max_lines: z.number().int().min(1).max(5)
      .transform(value => Math.min(value, reelLayout.translationMaxLines.en))
      .default(reelLayout.translationMaxLines.en),
    arabic_translation_gap_px: z.union([z.literal(80), z.literal(100), z.literal(120)]).default(80),
    arabic_translation_gap_version: z.literal(3).default(3),
    translation_stack_gap_px: z.union([z.literal(80), z.literal(100), z.literal(120)]).default(80),
    arabic_line_spacing: z.number().min(0.95).max(1.60).default(1.15),
    translation_line_spacing: z.number().min(1.0).max(1.6).default(1.25),
    text_outline: z
      .enum(["none", "minimal", "thin", "medium"])
      .default("minimal"),
    text_shadow: z
      .enum(["off", "very_soft", "soft", "medium"])
      .default("very_soft"),
    highlight_mode: z
      .enum(["none", "phrase_highlight", "word_highlight", "progressive_reveal_highlight"])
      .default("phrase_highlight"),
    highlight_intensity: z
      .enum(["subtle", "normal", "strong"])
      .default("normal"),
    highlight_style: z
      .enum(["none", "phrase_fade", "word_highlight", "phrase_word_highlight"])
      .default("phrase_fade"),
    highlight_color: z
      .enum(["warm_white", "ivory", "soft_gold", "neutral_white"])
      .default("warm_white"),
    transition_duration_ms: z.number().int().min(100).max(400).default(100),
    subtitle_lead_ms: z.number().int().min(0).max(1000).default(0),
    sura_header_top_offset: z.number().finite().default(reelLayout.suraHeaderOffset.default).transform(value => {
      const rule = reelLayout.suraHeaderOffset;
      return Math.round(Math.min(rule.max, Math.max(rule.min, value)) / rule.step) * rule.step;
    }),
    header_style: z
      .enum(["minimal_capsule", "soft_glass", "plain_text", "hidden"])
      .default("minimal_capsule"),
    header_content: z
      .enum(["surah_name_only", "arabic_translation_name", "arabic_translation_ayah_range"])
      .default("arabic_translation_ayah_range"),
    header_backing_intensity: z
      .enum(["none", "low", "medium", "high"])
      .default("low"),
    ayah_reference_position: z
      .enum(["top_header", "bottom", "hidden"])
      .default("top_header"),
    subtitle_backing: z
      .enum(["none", "shadow_only", "soft_gradient", "soft_translucent", "localized_subtle"])
      .default("shadow_only"),
    updated_at: z.string().nullable().optional(),
  })
  .superRefine((data, ctx) => {
    if (data.min_duration_seconds > data.max_duration_seconds) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "min_duration_seconds must be less than or equal to max_duration_seconds",
        path: ["max_duration_seconds"],
      });
    }
    const ids = new Set<string>();
    const activeTimes = new Set<string>();
    data.publishing_schedule.forEach((entry, index) => {
      if (ids.has(entry.id)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Schedule slot IDs must be unique.",
          path: ["publishing_schedule", index, "id"],
        });
      }
      ids.add(entry.id);
      if (entry.enabled && activeTimes.has(entry.time)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: `Duplicate active publishing time: ${entry.time}`,
          path: ["publishing_schedule", index, "time"],
        });
      }
      if (entry.enabled) activeTimes.add(entry.time);
    });
  });

export const StoredConfigSchema = z.preprocess(migrateLegacyConfig, StoredConfigObjectSchema);

export type StoredConfig = z.infer<typeof StoredConfigSchema>;

export const RunRecordSchema = z.object({
  schema_version: z
    .number()
    .int()
    .refine((v) => v === CURRENT_SCHEMA_VERSION, {
      message: `Unsupported run record schema_version. Expected ${CURRENT_SCHEMA_VERSION}`,
    })
    .default(CURRENT_SCHEMA_VERSION),
  run_id: z.string().min(1, "run_id must be a non-empty string"),
  date: z.string().regex(DATE_REGEX, "date must be in YYYY-MM-DD format"),
  slot: z.string().min(1).max(80).regex(SLOT_ID_REGEX),
  run_kind: z.enum(VALID_RUN_KINDS).optional(),
  schedule_slot_id: z.string().regex(SLOT_ID_REGEX).nullable().optional(),
  status: z.enum(VALID_STATUSES),
  started_at: z.string().min(1, "started_at must be an ISO 8601 timestamp"),
  stage: z.string().nullable().optional(),
  completed_at: z.string().nullable().optional(),
  surah_number: z.number().int().min(1).max(114).nullable().optional(),
  surah_name_ar: z.string().nullable().optional(),
  surah_name_en: z.string().nullable().optional(),
  first_ayah: z.number().int().min(1).nullable().optional(),
  last_ayah: z.number().int().min(1).nullable().optional(),
  duration_seconds: z.number().positive().nullable().optional(),
  pexels_video_ids: z.array(z.number().int()).default([]),
  media_key: z.string().nullable().optional(),
  final_safety_passed: z.boolean().nullable().optional(),
  instagram_status: z.enum(VALID_PUBLISH_STATUSES).default("NOT_ATTEMPTED"),
  facebook_status: z.enum(VALID_PUBLISH_STATUSES).default("NOT_ATTEMPTED"),
  instagram_media_id: z.string().nullable().optional(),
  facebook_media_id: z.string().nullable().optional(),
  publisher_backend: z.enum(["buffer", "meta"]).nullable().optional(),
  buffer_post_id: z.string().nullable().optional(),
  buffer_post_status: z.string().nullable().optional(),
  buffer_accepted_at: z.string().nullable().optional(),
  facebook_reel_url: z.string().url().nullable().optional(),
  facebook_published_at: z.string().nullable().optional(),
  public_media_delivery_id: z.string().min(20).max(160).nullable().optional(),
  facebook_failure_reason: z.string().nullable().optional(),
  instagram_failure_reason: z.string().nullable().optional(),
  failure_reason: z.string().nullable().optional(),
  translation_languages: z.array(z.string()).default(["en"]),
  translation_sources: z.record(z.object({
    language: z.enum(["en", "de"]).optional(),
    translation_key: z.string().min(1),
    source: z.string().min(1),
    version: z.string().min(1),
    last_update: z.union([z.string(), z.number()]).optional(),
  }).passthrough()).default({}),
  provenance: z.record(z.any()).default({}),
});

export type RunRecord = z.infer<typeof RunRecordSchema>;

export { TOTAL_CANONICAL_AYAHS } from "./canonical-quran";

export const CoverageStateSchema = z.object({
  schema_version: z
    .number()
    .int()
    .refine((v) => v === CURRENT_SCHEMA_VERSION, {
      message: `Unsupported coverage schema_version. Expected ${CURRENT_SCHEMA_VERSION}`,
    })
    .default(CURRENT_SCHEMA_VERSION),
  cycle_number: z.number().int().min(1).default(1),
  total_ayahs: z.number().int().default(TOTAL_CANONICAL_AYAHS),
  covered_count: z.number().int().min(0).max(TOTAL_CANONICAL_AYAHS).default(0),
  covered_ayahs: z.array(z.string()).default([]),
  processed_run_ids: z.array(z.string()).default([]),
  started_at: z.string().nullable().optional(),
  completed_at: z.string().nullable().optional(),
  last_updated_at: z.string().nullable().optional(),
});

export type CoverageState = z.infer<typeof CoverageStateSchema>;

export interface CoverageSummary {
  cycleNumber: number;
  totalAyahs: number;
  coveredCount: number;
  remainingCount: number;
  progressPercentage: number;
  isComplete: boolean;
  startedAt: string | null;
  lastUpdatedAt: string | null;
}
