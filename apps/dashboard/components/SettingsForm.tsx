"use client";

import React, { useState } from "react";
import reelLayout from "../reel-layout.json";
import { StoredConfig } from "@/lib/schema";
import { SystemConnectionStatus } from "@/lib/system";
import { SubtitlePreview } from "@/components/SubtitlePreview";
import { DEFAULT_PREVIEW_STYLE } from "@/lib/preview-catalog";
import { assertProductionPublishingSchedule, getProductionPublishingSchedule, getUpcomingPublishingTimes, PRODUCTION_PUBLISHING_SLOTS } from "@/lib/schedule";
import {
  Save,
  CheckCircle2,
  AlertCircle,
  Clock,
  Shield,
  Layers,
  Facebook,
  Instagram,
  Check,
  X,
  RefreshCw,
  Type,
} from "lucide-react";
import { useRouter } from "next/navigation";

export function SettingsForm({
  initialConfig,
  systemStatus,
  configWritable,
}: {
  initialConfig: StoredConfig;
  systemStatus: SystemConnectionStatus;
  configWritable: boolean;
}) {
  const router = useRouter();
  const [config, setConfig] = useState<StoredConfig>(initialConfig);
  const [loading, setLoading] = useState(false);
  const [statusMessage, setStatusMessage] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const upcoming = getUpcomingPublishingTimes(getProductionPublishingSchedule(config.publishing_schedule), new Date(), 4);
  const managedSchedule = PRODUCTION_PUBLISHING_SLOTS.map((slot) =>
    config.publishing_schedule.find((entry) => entry.id === slot.id) ??
    { ...slot, enabled: false }
  );
  const activeSchedule = managedSchedule.filter((entry) => entry.enabled);

  const setPostingFrequency = (count: number) => {
    const currentIds = managedSchedule.filter((entry) => entry.enabled).map((entry) => entry.id);
    const selected = [...currentIds];
    for (const slot of PRODUCTION_PUBLISHING_SLOTS) {
      if (selected.length >= count) break;
      if (!selected.includes(slot.id)) selected.push(slot.id);
    }
    const activeIds = new Set(selected.slice(0, count));
    const updated = config.publishing_schedule.map((entry) =>
      PRODUCTION_PUBLISHING_SLOTS.some((slot) => slot.id === entry.id)
        ? { ...entry, enabled: activeIds.has(entry.id) }
        : entry
    );
    const newEntries = PRODUCTION_PUBLISHING_SLOTS
      .filter((slot) => !config.publishing_schedule.some((entry) => entry.id === slot.id))
      .map((slot) => ({ ...slot, enabled: activeIds.has(slot.id) }));
    setConfig({ ...config, publishing_schedule: [...updated, ...newEntries] });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!configWritable) {
      setStatusMessage({
        type: "error",
        text: "Backblaze B2 configuration is unavailable. Settings are read-only and cannot be saved.",
      });
      return;
    }
    setLoading(true);
    setStatusMessage(null);

    // Client-side validations
    if (config.min_duration_seconds > config.max_duration_seconds) {
      setStatusMessage({
        type: "error",
        text: "Minimum duration cannot exceed maximum duration.",
      });
      setLoading(false);
      return;
    }
    try {
      assertProductionPublishingSchedule(config.publishing_schedule);
    } catch (error) {
      setStatusMessage({ type: "error", text: (error as Error).message });
      setLoading(false);
      return;
    }

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });

      const data = await res.json();
      if (res.ok && data.success) {
        setConfig(data.config);
        const dispatched = data.scheduler?.dispatched ?? [];
        if (dispatched.length > 0) {
          setStatusMessage({
            type: "success",
            text: `Saved — triggered scheduled run for ${dispatched.join(", ")}.`,
          });
        } else {
          setStatusMessage({
            type: "success",
            text: "Saved — applies to future Reels.",
          });
        }
        router.refresh();
      } else {
        setStatusMessage({
          type: "error",
          text: data.error || "Failed to save configuration.",
        });
      }
    } catch {
      setStatusMessage({
        type: "error",
        text: "Network error saving configuration to B2.",
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
      {!configWritable && (
        <div className="card" role="alert" style={{ color: "var(--status-blocked-text)" }}>
          Backblaze B2 configuration could not be loaded authoritatively. Fallback values are read-only; Save is blocked to protect the stored configuration.
        </div>
      )}
      {/* 1. Global Automation Control */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Automation State</h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          Master kill-switch for automated scheduled runs.
        </p>

        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer", fontWeight: 500 }}>
            <input
              type="checkbox"
              checked={config.automation_enabled}
              onChange={(e) => setConfig({ ...config, automation_enabled: e.target.checked })}
              style={{ width: "16px", height: "16px" }}
            />
            <span>Enable Automated Generation ({config.automation_enabled ? "Active" : "Disabled"})</span>
          </label>
        </div>
      </div>

      {/* 2. Daily publishing cadence */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Daily Publishing Times</h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          Choose 1, 2, or 3 Reels per day in <strong>Europe/Berlin</strong> (24-hour clock). Supabase checks every ten minutes and requests the existing GitHub Reel Action only when a configured time is due.
        </p>

        <div
          style={{
            padding: "10px 14px",
            background: "var(--status-blocked-bg)",
            border: "1px solid var(--status-blocked-border)",
            borderRadius: "var(--radius-md)",
            color: "var(--status-blocked-text)",
            fontSize: "12px",
            marginBottom: "16px",
            display: "flex",
            alignItems: "center",
            gap: "8px",
          }}
        >
          <Clock size={15} style={{ flexShrink: 0 }} />
          <span>
            <strong>Scheduler {systemStatus.schedulerHealth?.status || (systemStatus.scheduler === "active" ? "Active" : "Not activated")}:</strong>{" "}
            {systemStatus.schedulerHealth?.status === "ACTIVE"
              ? "Primary scheduler heartbeat is active. Automatic slot generation is operating normally."
              : systemStatus.schedulerHealth?.details || "The schedule model and protected tick endpoint are ready."}
          </span>
        </div>
        <div className="form-group" style={{ marginBottom: "12px" }}>
          <label className="form-label" htmlFor="reels-per-day">Reels per day</label>
          <select
            id="reels-per-day"
            aria-label="Reels per day"
            className="form-input"
            value={activeSchedule.length}
            disabled={!configWritable}
            onChange={(event) => setPostingFrequency(Number(event.target.value))}
          >
            <option value="1">1 Reel per day</option>
            <option value="2">2 Reels per day</option>
            <option value="3">3 Reels per day</option>
          </select>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          {activeSchedule.map((entry, index) => (
            <div key={entry.id} className="schedule-row">
              <label htmlFor={"publishing-time-" + entry.id} style={{ fontSize: "13px" }}>
                {"Reel " + (index + 1)}
              </label>
              <input
                id={"publishing-time-" + entry.id}
                type="time"
                step="60"
                value={entry.time}
                disabled={!configWritable}
                aria-label={"Publishing time for Reel " + (index + 1) + " (Europe/Berlin)"}
                onChange={(event) => setConfig({
                  ...config,
                  publishing_schedule: config.publishing_schedule.map((slot) =>
                    slot.id === entry.id ? { ...slot, time: event.target.value } : slot
                  ),
                })}
                className="form-input"
              />
            </div>
          ))}
        </div>
        <p className="form-hint">Choose 1–3 distinct times. Changing the daily count preserves existing slot identities and prevents duplicate claims.</p>
        {config.automation_enabled && (
          <p role="alert" className="form-hint" style={{ color: "var(--status-blocked-text)" }}>
            Saving with automation enabled may immediately dispatch an already-due Reel.
            Pause automation before editing if you do not want a run.
          </p>
        )}
        <div style={{ marginTop: "14px", fontSize: "12px", color: "var(--text-muted)" }}>
          <strong>{config.automation_enabled ? "Next publishing" : "Automation paused"}</strong>
          {upcoming.length > 0 ? upcoming.map((item, index) => <div key={`${item.date}-${item.slotId}`}>{index === 0 ? "Next: " : "Upcoming: "}{item.dayLabel} {item.time}</div>) : <div>No enabled publishing times.</div>}
        </div>
      </div>

      {/* 3. Duration & Video Generation Parameters */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Generation Bounds & Constraints</h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          Target duration interval and background video composition parameters.
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px", marginBottom: "16px" }} className="grid-2">
          <div className="form-group">
            <label className="form-label" htmlFor="min-duration">
              Min Duration (seconds)
            </label>
            <input
              id="min-duration"
              type="number"
              min="10"
              max="30"
              step="0.5"
              value={config.min_duration_seconds}
              onChange={(e) =>
                setConfig({ ...config, min_duration_seconds: parseFloat(e.target.value) || 10 })
              }
              className="form-input"
            />
            <p className="form-hint">Allowed: 10.0 to 30.0s</p>
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="max-duration">
              Max Duration (seconds)
            </label>
            <input
              id="max-duration"
              type="number"
              min="10"
              max="60"
              step="0.5"
              value={config.max_duration_seconds}
              onChange={(e) =>
                setConfig({ ...config, max_duration_seconds: parseFloat(e.target.value) || 30 })
              }
              className="form-input"
            />
            <p className="form-hint">Allowed: 10.0 to 60.0s (normal range)</p>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }} className="grid-2">
          <div className="form-group">
            <label className="form-label" htmlFor="bg-strategy">
              Background Clip Strategy
            </label>
            <select
              id="bg-strategy"
              value={config.background_clip_strategy || "auto_prefer_single"}
              onChange={(e) =>
                setConfig({
                  ...config,
                  background_clip_strategy: e.target.value as "auto_prefer_single" | "single" | "multiple",
                })
              }
              className="form-input"
            >
              <option value="auto_prefer_single">Auto — Prefer One Continuous Video</option>
              <option value="single">Single Video Only</option>
              <option value="multiple">Multiple Videos</option>
            </select>
            <p className="form-hint">Single continuous clip preserves natural camera motion</p>
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="bg-clips">
              Maximum Background Clips
            </label>
            <input
              id="bg-clips"
              type="number"
              min="1"
              max="3"
              value={config.max_background_clips || config.target_background_clips || 3}
              onChange={(e) => {
                const val = parseInt(e.target.value, 10) || 3;
                setConfig({ ...config, max_background_clips: val, target_background_clips: val });
              }}
              className="form-input"
            />
            <p className="form-hint">Allowed: 1 to 3 clips (preferred 1)</p>
          </div>
        </div>
      </div>

      {/* 5. Social Publishing Targets */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Social Media Destinations</h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          V1 Publishing hierarchy: Facebook Page is Primary; Instagram is Optional.
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }} className="grid-2">
          {/* Facebook */}
          <div
            style={{
              padding: "16px",
              background: "var(--bg-app)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <Facebook size={16} color="var(--primary)" />
                <span style={{ fontWeight: 600 }}>Facebook Page</span>
                <span className="badge badge-success" style={{ fontSize: "10px" }}>PRIMARY</span>
              </div>
              <input
                type="checkbox"
                checked={config.facebook_enabled}
                onChange={(e) => setConfig({ ...config, facebook_enabled: e.target.checked })}
              />
            </div>
            <p style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "8px" }}>
              Publishes one approved Facebook Reel through Buffer when its explicit server-side flags and both rights gates are enabled.
            </p>
          </div>

          {/* Instagram */}
          <div
            style={{
              padding: "16px",
              background: "var(--bg-app)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <Instagram size={16} color="var(--primary)" />
                <span style={{ fontWeight: 600 }}>Instagram Account</span>
                <span className="badge badge-neutral" style={{ fontSize: "10px" }}>OPTIONAL</span>
              </div>
              <input
                type="checkbox"
                checked={config.instagram_enabled}
                onChange={(e) => setConfig({ ...config, instagram_enabled: e.target.checked })}
              />
            </div>
            <p style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "8px" }}>
              Publish verified Reels to the connected Instagram account via Buffer.
            </p>
          </div>
        </div>
      </div>

      {/* 6. Reciter Publishing Rights Status */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Audio Redistribution Rights Gate</h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          Fail-closed legal and rights safety gating. Both gates must be confirmed before public broadcasting.
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }} className="grid-2">
          <div
            style={{
              padding: "14px",
              background: "var(--bg-app)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md)",
            }}
          >
            <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Stored Config Gate</span>
            <div style={{ marginTop: "6px" }}>
              <span className={`badge ${config.reciter_reuse_confirmed ? "badge-success" : "badge-blocked"}`}>
                {config.reciter_reuse_confirmed ? <Check size={12} /> : <X size={12} />}
                <span>{config.reciter_reuse_confirmed ? "Confirmed" : "Not Confirmed"}</span>
              </span>
            </div>
          </div>

          <div
            style={{
              padding: "14px",
              background: "var(--bg-app)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md)",
            }}
          >
            <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Runtime Environment Gate</span>
            <div style={{ marginTop: "6px" }}>
              <span className={`badge ${systemStatus.reciterReuseRuntimeGate ? "badge-success" : "badge-blocked"}`}>
                {systemStatus.reciterReuseRuntimeGate ? <Check size={12} /> : <X size={12} />}
                <span>{systemStatus.reciterReuseRuntimeGate ? "RECITER_REUSE_CONFIRMED=true" : "RECITER_REUSE_CONFIRMED=false"}</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* 7. Subtitle & Visual Style */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }} id="reel-visual-style">
          <Type size={16} style={{ verticalAlign: "text-bottom", marginRight: "6px" }} />
          Subtitle & Visual Style
        </h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          Configure Progressive Phrase Sync typography, header design, highlight effects, and subtitle backing.
        </p>

        <a className="preview-mobile-jump" href="#reel-preview">Jump to Reel preview</a>
        <div className="subtitle-style-layout">
          {/* Controls column */}
          <div className="subtitle-style-controls">
            <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>Text &amp; Languages</h2>
            <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
              Arabic is always primary. These values apply to future Reels only.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "18px" }} className="preview-control-grid">
              <div className="form-group">
                <label className="form-label">Translation Display</label>
                {([[["en"], "English"], [["de"], "German"], [["en", "de"], "English + German"]] as const).map(([languages, label]) => (
                  <label key={label} className="preset-option">
                    <input type="radio" name="translation-languages" checked={config.translation_languages.join(",") === languages.join(",")} onChange={() => setConfig({ ...config, translation_languages: [...languages] })} />
                    {label}
                  </label>
                ))}
              </div>
              <div className="form-group">
                <label className="form-label">Translation Font Size</label>
                {([[50, "Standard"], [40, "Small"], [30, "Smaller"]] as const).map(([value, label]) => (
                  <label key={value} className="preset-option"><input type="radio" name="translation-size" checked={config.translation_font_size === value} onChange={() => setConfig({ ...config, translation_font_size: value })} />{label} — {value}px</label>
                ))}
              </div>
              <div className="form-group">
                <label className="form-label">Arabic ↔ Translation Spacing</label>
                {([[80, "Standard"], [100, "Medium"], [120, "Wide"]] as const).map(([value, label]) => (
                  <label key={value} className="preset-option"><input type="radio" name="arabic-gap" checked={config.arabic_translation_gap_px === value} onChange={() => setConfig({ ...config, arabic_translation_gap_px: value })} />{label} — {value}px</label>
                ))}
              </div>
              <div className="form-group" style={{ opacity: config.translation_languages.length === 2 ? 1 : 0.55 }}>
                <label className="form-label">English ↔ German Gap</label>
                {([[10, "Compact"], [20, "Medium"], [30, "Wide"]] as const).map(([value, label]) => (
                  <label key={value} className="preset-option"><input type="radio" name="translation-stack-gap" disabled={config.translation_languages.length !== 2} checked={config.translation_stack_gap_px === value} onChange={() => setConfig({ ...config, translation_stack_gap_px: value })} />{label} — {value}px</label>
                ))}
                <p className="form-hint">Used only when English and German are both enabled.</p>
              </div>
            </div>

            {/* Subtitle Mode */}
            <div className="form-group">
              <label className="form-label">Subtitle Mode</label>
              <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                {([
                  ["progressive_phrase_sync", "Progressive Phrase Sync", "Short phrases synced with recitation"],
                  ["static_full_ayah", "Static Full Ayah", "Legacy full-ayah text blocks"],
                  ["progressive_word_highlight", "Progressive Word Highlight", "Requires word-level timing (unavailable)"],
                ] as const).map(([value, label, hint]) => (
                  <label
                    key={value}
                    style={{
                      display: "flex", alignItems: "flex-start", gap: "8px", cursor: value === "progressive_word_highlight" ? "not-allowed" : "pointer",
                      opacity: value === "progressive_word_highlight" ? 0.5 : 1,
                    }}
                  >
                    <input
                      type="radio"
                      name="subtitle_mode"
                      value={value}
                      checked={config.subtitle_mode === value}
                      disabled={value === "progressive_word_highlight"}
                      onChange={() => setConfig({ ...config, subtitle_mode: value })}
                      style={{ marginTop: "3px" }}
                    />
                    <div>
                      <span style={{ fontSize: "13px", fontWeight: 500 }}>{label}</span>
                      <p style={{ fontSize: "11px", color: "var(--text-muted)", margin: 0 }}>{hint}</p>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            {/* Arabic size remains independently configurable; translation uses approved presets above. */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }} className="grid-2">
              <div className="form-group">
                <label className="form-label" htmlFor="ar-font-size">
                  Arabic Font Size: {config.arabic_font_size}px
                </label>
                <input
                  id="ar-font-size"
                  type="range"
                  min={72} max={180} step={2}
                  value={config.arabic_font_size}
                  onChange={(e) => setConfig({ ...config, arabic_font_size: parseInt(e.target.value) })}
                  style={{ width: "100%" }}
                />
                <p className="form-hint">72–180 px (approved default 180)</p>
              </div>
              <div className="form-group">
                <label className="form-label">Translation Font Preset</label>
                <div style={{ fontSize: "13px", padding: "8px 0" }}>{config.translation_font_size}px — configured in Text &amp; Languages</div>
              </div>
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="sura-header-offset">Sura Header Position: {config.sura_header_top_offset}px</label>
              <input id="sura-header-offset" type="range" style={{ width: "100%" }}
                min={reelLayout.suraHeaderOffset.min} max={reelLayout.suraHeaderOffset.max} step={reelLayout.suraHeaderOffset.step}
                value={config.sura_header_top_offset}
                onChange={event => setConfig({ ...config, sura_header_top_offset: Number(event.target.value) })} />
              <p className="form-hint">Offset below the fixed text Safe Zone. Default: {reelLayout.suraHeaderOffset.default}px.</p>
            </div>

            {/* Max Lines */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
              <div className="form-group">
                <label className="form-label" htmlFor="ar-max-lines">Arabic Max Lines</label>
                <select
                  id="ar-max-lines"
                  value={config.arabic_max_lines}
                  onChange={(e) => setConfig({ ...config, arabic_max_lines: parseInt(e.target.value) })}
                  className="form-input"
                >
                  {[1,2,3,4,5].map(n => <option key={n} value={n}>{n}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="tr-max-lines">Translation Max Lines</label>
                <select
                  id="tr-max-lines"
                  value={config.translation_max_lines}
                  onChange={(e) => setConfig({ ...config, translation_max_lines: parseInt(e.target.value) })}
                  className="form-input"
                >
                  {[1,2,3,4,5].map(n => <option key={n} value={n}>{n}</option>)}
                </select>
              </div>
            </div>

            <p className="form-hint">Arabic line spacing is measured automatically, including diacritics. These historical controls are inactive.</p>
            {/* Line Spacing */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }} className="grid-2">
              <div className="form-group">
                <label className="form-label" htmlFor="ar-spacing">
                  Ar Spacing: {config.arabic_line_spacing.toFixed(2)}
                </label>
                <input
                  id="ar-spacing"
                  disabled
                  type="range"
                  min={0.95} max={1.60} step={0.05}
                  value={config.arabic_line_spacing}
                  onChange={(e) => setConfig({ ...config, arabic_line_spacing: parseFloat(e.target.value) })}
                  style={{ width: "100%" }}
                />
              </div>
              <div className="form-group">
                <label className="form-label" htmlFor="tr-spacing">
                  Tr Spacing: {config.translation_line_spacing.toFixed(2)}
                </label>
                <input
                  id="tr-spacing"
                  disabled
                  type="range"
                  min={1.0} max={1.6} step={0.05}
                  value={config.translation_line_spacing}
                  onChange={(e) => setConfig({ ...config, translation_line_spacing: parseFloat(e.target.value) })}
                  style={{ width: "100%" }}
                />
              </div>
            </div>

            {/* Text Outline & Shadow */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
              <div className="form-group">
                <label className="form-label">Text Outline</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                  {([
                    ["none", "None"],
                    ["minimal", "Minimal (Def)"],
                    ["thin", "Thin"],
                    ["medium", "Medium"],
                  ] as const).map(([value, label]) => (
                    <label key={value} style={{ display: "flex", alignItems: "center", gap: "4px", cursor: "pointer", fontSize: "12px" }}>
                      <input
                        type="radio"
                        name="text_outline"
                        value={value}
                        checked={config.text_outline === value}
                        onChange={() => setConfig({ ...config, text_outline: value })}
                      />
                      {label}
                    </label>
                  ))}
                </div>
                <p className="form-hint">Minimal = sub-pixel support; Quran Arabic uses a same-color edge.</p>
              </div>

              <div className="form-group">
                <label className="form-label">Text Shadow</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                  {([
                    ["off", "Off"],
                    ["very_soft", "Very Soft (Def)"],
                    ["soft", "Soft"],
                    ["medium", "Medium"],
                  ] as const).map(([value, label]) => (
                    <label key={value} style={{ display: "flex", alignItems: "center", gap: "4px", cursor: "pointer", fontSize: "12px" }}>
                      <input
                        type="radio"
                        name="text_shadow"
                        value={value}
                        checked={config.text_shadow === value}
                        onChange={() => setConfig({ ...config, text_shadow: value })}
                      />
                      {label}
                    </label>
                  ))}
                </div>
                <p className="form-hint">Very soft gentle separation without dark clouds</p>
              </div>
            </div>

            {/* Highlight Mode & Intensity */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
              <div className="form-group">
                <label className="form-label">Highlight Mode</label>
                <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                  {([
                    ["phrase_highlight", "Phrase Highlight (Default)"],
                    ["none", "None"],
                    ["word_highlight", "Word Highlight"],
                    ["progressive_reveal_highlight", "Progressive Reveal + Highlight"],
                  ] as const).map(([value, label]) => {
                    const isDisabled = value === "word_highlight" || value === "progressive_reveal_highlight";
                    return (
                      <label
                        key={value}
                        style={{
                          display: "flex", alignItems: "center", gap: "6px",
                          cursor: isDisabled ? "not-allowed" : "pointer",
                          opacity: isDisabled ? 0.45 : 1,
                          fontSize: "12px",
                        }}
                      >
                        <input
                          type="radio"
                          name="highlight_mode"
                          value={value}
                          checked={config.highlight_mode === value}
                          disabled={isDisabled}
                          onChange={() => setConfig({ ...config, highlight_mode: value })}
                        />
                        <span>{label}</span>
                      </label>
                    );
                  })}
                </div>
                <p className="form-hint">Word modes disabled: ayah-level timing only in MP3Quran</p>
              </div>

              <div className="form-group">
                <label className="form-label">Highlight Intensity</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                  {([
                    ["subtle", "Subtle"],
                    ["normal", "Normal (Def)"],
                    ["strong", "Strong"],
                  ] as const).map(([value, label]) => (
                    <label key={value} style={{ display: "flex", alignItems: "center", gap: "4px", cursor: "pointer", fontSize: "12px" }}>
                      <input
                        type="radio"
                        name="highlight_intensity"
                        value={value}
                        checked={config.highlight_intensity === value}
                        onChange={() => setConfig({ ...config, highlight_intensity: value })}
                      />
                      {label}
                    </label>
                  ))}
                </div>
                <p className="form-hint">Restrained, non-flashy brightness</p>
              </div>
            </div>

            {/* Highlight Color */}
            <div className="form-group">
              <label className="form-label">Highlight Color</label>
              <div style={{ display: "flex", gap: "8px" }}>
                {([
                  ["warm_white", "Warm White", "#FFF5F0"],
                  ["ivory", "Ivory", "#FFEFE0"],
                  ["soft_gold", "Soft Gold", "#FFDDC0"],
                  ["neutral_white", "Neutral White", "#FFFFFF"],
                ] as const).map(([value, label, color]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setConfig({ ...config, highlight_color: value })}
                    style={{
                      width: "40px", height: "28px",
                      backgroundColor: color,
                      border: config.highlight_color === value ? "2px solid var(--primary)" : "1px solid var(--border-subtle)",
                      borderRadius: "var(--radius-sm)",
                      cursor: "pointer",
                      position: "relative",
                    }}
                    title={label}
                  />
                ))}
              </div>
            </div>

            {/* Text Lead & Transition Duration */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
              <div className="form-group">
                <label className="form-label" htmlFor="subtitle-lead-ms">
                  Text Lead Before Recitation: {config.subtitle_lead_ms ?? 0}ms
                </label>
                <input
                  id="subtitle-lead-ms"
                  type="range"
                  min={0} max={1000} step={50}
                  value={config.subtitle_lead_ms ?? 0}
                  onChange={(e) => setConfig({ ...config, subtitle_lead_ms: parseInt(e.target.value) })}
                  style={{ width: "100%" }}
                />
                <p className="form-hint">Text lead before recitation (default: 0ms for simultaneous start)</p>
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="transition-ms">
                  Transition Duration: {config.transition_duration_ms}ms
                </label>
                <input
                  id="transition-ms"
                  type="range"
                  min={100} max={400} step={25}
                  value={config.transition_duration_ms}
                  onChange={(e) => setConfig({ ...config, transition_duration_ms: parseInt(e.target.value) })}
                  style={{ width: "100%" }}
                />
                <p className="form-hint">Fade in/out per phrase (100–400ms, approved default 100)</p>
              </div>
            </div>

            {/* Header Style, Content & Backing Intensity */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "12px" }}>
              <div className="form-group">
                <label className="form-label">Header Style</label>
                <select
                  value={config.header_style}
                  onChange={(e) => setConfig({ ...config, header_style: e.target.value as StoredConfig["header_style"] })}
                  className="form-input"
                >
                  <option value="minimal_capsule">Minimal Capsule</option>
                  <option value="soft_glass">Soft Glass</option>
                  <option value="plain_text">Plain Text</option>
                  <option value="hidden">Hidden</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Header Content</label>
                <select
                  value={config.header_content}
                  onChange={(e) => setConfig({ ...config, header_content: e.target.value as StoredConfig["header_content"] })}
                  className="form-input"
                >
                  <option value="arabic_translation_ayah_range">Name + Ayah Range</option>
                  <option value="arabic_translation_name">Name Only</option>
                  <option value="surah_name_only">Arabic Only</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Header Backing</label>
                <select
                  value={config.header_backing_intensity ?? "low"}
                  onChange={(e) => setConfig({ ...config, header_backing_intensity: e.target.value as "none" | "low" | "medium" | "high" })}
                  className="form-input"
                >
                  <option value="none">None (0% — no box)</option>
                  <option value="low">Low (12% subtle tint)</option>
                  <option value="medium">Medium (20%)</option>
                  <option value="high">High (28%)</option>
                </select>
              </div>
            </div>

            {/* Subtitle Backing */}
            <div className="form-group">
              <label className="form-label">Subtitle Backing</label>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                {([
                  ["localized_subtle", "Localized Subtle"],
                  ["soft_translucent", "Soft Translucent"],
                  ["soft_gradient", "Soft Gradient"],
                  ["shadow_only", "Shadow Only (Def)"],
                  ["none", "None"],
                ] as const).map(([value, label]) => (
                  <label key={value} style={{ display: "flex", alignItems: "center", gap: "4px", cursor: "pointer", fontSize: "12px" }}>
                    <input
                      type="radio"
                      name="subtitle_backing"
                      value={value}
                      checked={(config.subtitle_backing ?? "localized_subtle") === value}
                      onChange={() => setConfig({ ...config, subtitle_backing: value })}
                    />
                    {label}
                  </label>
                ))}
              </div>
              <p className="form-hint">Localized backing is fitted tightly around text bounding box with light 10–18% opacity</p>
            </div>
          </div>

          {/* Preview column */}
          <div className="subtitle-preview-column">
            <SubtitlePreview
              config={config}
              onRestoreDefaultStyle={() => {
                setConfig({ ...config, ...DEFAULT_PREVIEW_STYLE });
                setStatusMessage(null);
              }}
            />
          </div>
        </div>
      </div>

      {/* 8. System Integration Status */}
      <div className="card">
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "4px" }}>System Connection Health</h2>
        <p style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "16px" }}>
          Server-side configuration presence only. See Operations for recent publishing outcomes and channel reachability evidence.
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "12px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Backblaze B2</span>
            <span className={`badge ${systemStatus.b2 ? "badge-success" : "badge-failed"}`} style={{ fontSize: "11px" }}>
              {systemStatus.b2 ? "Configured" : "Missing"}
            </span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>GitHub Actions</span>
            <span className={`badge ${systemStatus.github ? "badge-success" : "badge-failed"}`} style={{ fontSize: "11px" }}>
              {systemStatus.github ? "Configured" : "Missing"}
            </span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Quran Foundation</span>
            <span className={`badge ${systemStatus.quranFoundation ? "badge-success" : "badge-neutral"}`} style={{ fontSize: "11px" }}>
              {systemStatus.quranFoundation ? "Configured" : "Optional/Missing"}
            </span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>English Translation</span>
            <span className={`badge ${systemStatus.englishTranslation ? "badge-success" : "badge-failed"}`}>{systemStatus.englishTranslation ? "english_rwwad" : "Unavailable"}</span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>German Translation</span>
            <span className={`badge ${systemStatus.germanTranslation ? "badge-success" : "badge-failed"}`}>{systemStatus.germanTranslation ? "german_rwwad" : "Unavailable"}</span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Audio / Yasser Timing</span>
            <span className={`badge ${systemStatus.audioYasserTiming ? "badge-success" : "badge-failed"}`}>{systemStatus.audioYasserTiming ? "Read 92 pinned" : "Unavailable"}</span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "6px", padding: "10px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontSize: "12px", fontWeight: 600 }}>Scheduler</span>
              <span className={`badge ${systemStatus.schedulerHealth?.status === "ACTIVE" ? "badge-success" : systemStatus.schedulerHealth?.status === "DEGRADED" ? "badge-failed" : "badge-neutral"}`}>
                {systemStatus.schedulerHealth?.status === "DEGRADED" ? "DEGRADED — heartbeat overdue" : systemStatus.schedulerHealth?.status || systemStatus.scheduler.replace("_", " ")}
              </span>
            </div>
            <div style={{ fontSize: "11px", color: "var(--text-muted)", display: "flex", flexDirection: "column", gap: "2px" }}>
              <div><strong>Automation:</strong> {systemStatus.schedulerHealth?.automation || (config.automation_enabled ? "ON" : "OFF")}</div>
              <div><strong>Trigger:</strong> {systemStatus.schedulerHealth?.trigger || "Supabase pg_cron + GitHub backup"}</div>
              <div><strong>Last heartbeat:</strong> {systemStatus.schedulerHealth?.lastHeartbeat ? new Date(systemStatus.schedulerHealth.lastHeartbeat).toLocaleString("de-DE", { timeZone: "Europe/Berlin" }) + ` (${systemStatus.schedulerHealth.lastHeartbeatSource || "cron"})` : "Never"}</div>
              <div><strong>Last scheduler claim:</strong> {systemStatus.schedulerHealth?.lastClaim || "Never"}</div>
              <div><strong>Last scheduled run:</strong> {systemStatus.schedulerHealth?.lastScheduledRun || "Never"}</div>
            </div>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Preview Catalog</span>
            <span className={`badge ${systemStatus.previewCatalog === "ready" ? "badge-success" : "badge-failed"}`}>
              {systemStatus.previewCatalog === "ready" ? "v3 Ready" : "Incomplete"}
            </span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Pexels API</span>
            <span className={`badge ${systemStatus.pexels ? "badge-success" : "badge-neutral"}`} style={{ fontSize: "11px" }}>
              {systemStatus.pexels ? "Configured" : "Optional/Missing"}
            </span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Buffer Facebook Page</span>
            <span className={`badge ${systemStatus.bufferFacebook ? "badge-neutral" : "badge-failed"}`} style={{ fontSize: "11px" }}>
              {systemStatus.bufferFacebook ? "Configuration present" : "Disabled / Missing"}
            </span>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", background: "var(--bg-app)", borderRadius: "var(--radius-sm)" }}>
            <span style={{ fontSize: "12px" }}>Instagram Publisher</span>
            <span className={`badge ${systemStatus.bufferInstagram ? "badge-neutral" : "badge-failed"}`} style={{ fontSize: "11px" }}>
              {systemStatus.bufferInstagram ? "Configuration present" : "Disabled / Missing"}
            </span>
          </div>
        </div>
      </div>

      {/* Status Message and Save Button */}
      {statusMessage && (
        <div
          style={{
            padding: "12px 16px",
            borderRadius: "var(--radius-md)",
            border: `1px solid ${
              statusMessage.type === "success"
                ? "var(--status-success-border)"
                : "var(--status-failed-border)"
            }`,
            background:
              statusMessage.type === "success"
                ? "var(--status-success-bg)"
                : "var(--status-failed-bg)",
            color:
              statusMessage.type === "success"
                ? "var(--status-success-text)"
                : "var(--status-failed-text)",
            fontSize: "13px",
            display: "flex",
            alignItems: "center",
            gap: "8px",
          }}
        >
          {statusMessage.type === "success" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
          <span>{statusMessage.text}</span>
        </div>
      )}

      <div>
        <button
          type="submit"
          disabled={loading || !configWritable}
          className="btn btn-primary"
          style={{ height: "40px", padding: "0 20px" }}
        >
          {loading ? (
            <>
              <RefreshCw size={15} className="spin" />
              <span>Saving Configuration...</span>
            </>
          ) : (
            <>
              <Save size={15} />
              <span>Save Configuration</span>
            </>
          )}
        </button>
      </div>
    </form>
  );
}
