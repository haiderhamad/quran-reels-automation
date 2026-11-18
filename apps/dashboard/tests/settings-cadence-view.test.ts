import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { StoredConfigSchema } from "../lib/schema";
import type { SystemConnectionStatus } from "../lib/system";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("../components/SubtitlePreview", () => ({ SubtitlePreview: () => null }));

import { SettingsForm } from "../components/SettingsForm";

describe("editable Admin Dashboard publishing times", () => {
  it("renders time inputs for existing stable slots in Europe/Berlin", () => {
    const config = StoredConfigSchema.parse({
      publishing_schedule: [
        { id: "morning", time: "09:00", enabled: true },
        { id: "slot-00000000-0000-4000-8000-000000000002", time: "16:00", enabled: true },
        { id: "evening", time: "13:00", enabled: false },
      ],
    });
    const html = renderToStaticMarkup(React.createElement(SettingsForm, {
      initialConfig: config, configWritable: true,
      systemStatus: { scheduler: "active" } as SystemConnectionStatus,
    }));
    const section = html.slice(html.indexOf("Daily Publishing Times"), html.indexOf("Generation Bounds"));
    expect(section).toContain("09:00");
    expect(section).toContain("16:00");
    expect(section).not.toContain('value="13:00"');
    expect(section).toContain("Europe/Berlin");
    expect(section).toContain('type="time"');
    expect(section).toContain('value="09:00"');
    expect(section).toContain('value="16:00"');
    expect(section.match(/type="time"/g)).toHaveLength(2);
    expect(section).toContain('id="reels-per-day"');
    expect(section).toContain("Reels per day");
    expect(section).toContain('option value="1"');
    expect(section).toContain('option value="3"');
    expect(section).toContain("Choose 1–3 distinct times");
  });
});

describe("Arabic to translation spacing controls", () => {
  it("shows the new pixel values with the previously saved Wide option selected", () => {
    const config = StoredConfigSchema.parse({ arabic_translation_gap_version: 2, arabic_translation_gap_px: 60 });
    const html = renderToStaticMarkup(React.createElement(SettingsForm, {
      initialConfig: config, configWritable: true,
      systemStatus: { scheduler: "active" } as SystemConnectionStatus,
    }));
    const section = html.slice(html.indexOf("Arabic ↔ Translation Spacing"), html.indexOf("English ↔ German Gap"));
    expect(section.match(/type="radio"/g)).toHaveLength(3);
    expect(section).toContain("Standard — 80px");
    expect(section).toContain("Medium — 100px");
    expect(section).toMatch(/<input[^>]*checked=""[^>]*\/>Wide — 120px/);
  });
});

describe("English to German spacing controls", () => {
  it.each([{ languages: ["en"] }, { languages: ["de"] }, { languages: ["en", "de"] }])(
    "preserves Wide and gates the controls for $languages", ({ languages }) => {
      const config = StoredConfigSchema.parse({ translation_stack_gap_px: 30, translation_languages: languages });
      const html = renderToStaticMarkup(React.createElement(SettingsForm, {
        initialConfig: config, configWritable: true,
        systemStatus: { scheduler: "active" } as SystemConnectionStatus,
      }));
      const section = html.slice(html.indexOf("English ↔ German Gap"), html.indexOf("Used only when English"));
      expect(section.match(/type="radio"/g)).toHaveLength(3);
      expect(section).toContain("Compact — 80px");
      expect(section).toContain("Medium — 100px");
      expect(section).toMatch(/<input[^>]*checked=""[^>]*\/>Wide — 120px/);
      expect(section.match(/disabled=""/g) ?? []).toHaveLength(languages.length === 2 ? 0 : 3);
    },
  );
});
