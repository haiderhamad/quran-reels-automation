import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { StoredConfigSchema } from "../lib/schema";
import {
  PREVIEW_CASES, DEFAULT_PREVIEW_CASE, DEFAULT_PREVIEW_STYLE, PREVIEW_ARABIC_GAPS,
  PREVIEW_LAYOUT_COUNT, PREVIEW_CATALOG_MANIFEST_URL, selectPreview,
  loadPreviewCatalog, validatePreviewManifest,
} from "../lib/preview-catalog";

const directory = path.join(process.cwd(), "public/previews/v3");
const manifest = validatePreviewManifest(JSON.parse(fs.readFileSync(path.join(directory, "workspace.json"), "utf8")));
const defaults = StoredConfigSchema.parse({});

describe("static renderer workspace", () => {
  it("ships all native glyph assets and 19,620 layouts with integrity hashes", () => {
    expect(Object.keys(manifest.layouts)).toHaveLength(PREVIEW_LAYOUT_COUNT);
    for (const [name, hash] of Object.entries(manifest.files)) {
      expect(crypto.createHash("sha256").update(fs.readFileSync(path.join(directory, name))).digest("hex")).toBe(hash);
    }
    expect(manifest.canvas).toEqual([1080, 1920]);
    expect(manifest.header_top).toBe(130);
  });

  it("defaults to 2 Lines and provides genuine natural 1/2/3-line glyphs", () => {
    expect(DEFAULT_PREVIEW_CASE).toBe("2-line");
    expect(PREVIEW_CASES).toEqual(["1-line", "2-line", "3-line"]);
    PREVIEW_CASES.forEach((previewCase, index) => {
      const preview = selectPreview(manifest, defaults, previewCase);
      expect(preview.glyphs[0].visual_line_ranges.length).toBe(index + 1);
      expect(preview.sample.arabic).not.toMatch(/[\r\n]/);
      if (index > 0) expect(preview.glyphs[1].visual_line_ranges.length).toBeGreaterThan(1);
    });
  });

  it("changes actual layout immediately for each 80/100/120 pixel setting", () => {
    expect(PREVIEW_ARABIC_GAPS).toEqual([80, 100, 120]);
    const previews = PREVIEW_ARABIC_GAPS.map(gap => selectPreview(manifest, { ...defaults, arabic_translation_gap_px: gap }, "2-line"));
    previews.forEach((preview, i) => {
      const ar = preview.glyphs[0];
      expect(preview.layout.tops[1] - preview.layout.tops[0] - (ar.bottom_offset - ar.top_offset)).toBe(PREVIEW_ARABIC_GAPS[i]);
    });
    expect(new Set(previews.map(p => p.layout.tops[0])).size).toBe(3);
    expect(new Set(previews.map(p => p.layout.tops[1])).size).toBe(3);
  });

  it("previews every supported font size and language combination without changing settings", () => {
    const config = structuredClone(defaults);
    const before = JSON.stringify(config);
    for (const previewCase of PREVIEW_CASES) selectPreview(manifest, config, previewCase);
    const dense = selectPreview(manifest, config, "2-line");
    const comparison = selectPreview(manifest, config, "2-line-comparison");
    expect(dense.sample.label).toBe("Sketchbook");
    expect(dense.sample.source).toBe("Original typography sample");
    expect(comparison.sample.label).toBe("Studio window");
    expect(comparison.glyphs[0].visual_line_ranges.length).toBe(2);
    expect(JSON.stringify(config)).toBe(before);
    expect(StoredConfigSchema.parse(JSON.parse(before))).toEqual(config);
    for (let size = 72; size <= 180; size++) {
      const preview = selectPreview(manifest, { ...defaults, arabic_font_size: size, translation_languages: ["en", "de"] }, "3-line");
      expect(preview.glyphs).toHaveLength(3);
    }
  });

  it("reports overflow without truncating or changing production max lines", () => {
    const preview = selectPreview(manifest, defaults, "3-line");
    expect(preview.exceedsLines).toBe(true);
    expect(preview.glyphs[0].visual_line_ranges.length).toBe(3);
    expect(defaults.arabic_max_lines).toBe(2);
  });

  it("fetches only the static catalog; missing data fails visibly", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(manifest))) as typeof fetch;
    expect((await loadPreviewCatalog(fetcher)).version).toBe(3);
    expect(fetcher).toHaveBeenCalledWith(PREVIEW_CATALOG_MANIFEST_URL, { cache: "no-cache" });
    await expect(loadPreviewCatalog(vi.fn(async () => new Response("{}")) as typeof fetch)).rejects.toThrow("incomplete");
    expect(() => selectPreview({ ...manifest, headers: {} }, defaults, "2-line")).toThrow("missing");
  });

  it("migrates historical values once and preserves new settings through serialization", () => {
    for (const [old, current] of [[6, 80], [8, 100], [10, 80], [15, 100], [20, 120]]) {
      const config = StoredConfigSchema.parse({ arabic_translation_gap_px: old });
      expect(config.arabic_translation_gap_px).toBe(current);
      expect(config.arabic_translation_gap_version).toBe(3);
      expect(StoredConfigSchema.parse(JSON.parse(JSON.stringify(config)))).toEqual(config);
    }
    for (const version of [0, 4, "2", true, null]) expect(() => StoredConfigSchema.parse({ arabic_translation_gap_version: version })).toThrow();
    expect(DEFAULT_PREVIEW_STYLE.arabic_translation_gap_px).toBe(80);
    expect(DEFAULT_PREVIEW_STYLE.arabic_translation_gap_version).toBe(3);
  });
  it("shares hard safe margins and normalizes persisted header offsets", () => {
    expect(manifest.safe_zone).toEqual({ x: 80, y: 80, right: 1000, bottom: 1800, width: 920, height: 1720 });
    expect(defaults.sura_header_top_offset).toBe(50);
    for (const [value, expected] of [[-10, 0], [0, 0], [13, 15], [40, 40], [50, 50], [65, 60]]) {
      const config = StoredConfigSchema.parse({ sura_header_top_offset: value });
      expect(config.sura_header_top_offset).toBe(expected);
      expect(StoredConfigSchema.parse(JSON.parse(JSON.stringify(config)))).toEqual(config);
    }
    for (const value of [null, true, "10", NaN]) {
      expect(() => StoredConfigSchema.parse({ sura_header_top_offset: value })).toThrow();
    }
  });

});
