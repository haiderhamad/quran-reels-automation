"use client";

import reelLayout from "../reel-layout.json";
import React, { useEffect, useId, useState } from "react";
import { AlertCircle, RotateCcw, ZoomIn } from "lucide-react";
import type { StoredConfig } from "@/lib/schema";
import {
  DEFAULT_PREVIEW_CASE, PREVIEW_CASES, loadPreviewCatalog, selectPreview,
  type PreviewCase, type PreviewCatalogManifest,
} from "@/lib/preview-catalog";

/** Static native libass glyphs + production-generated positions; no browser wrapping. */
export function SubtitlePreview({ config, onRestoreDefaultStyle }: {
  config: StoredConfig; onRestoreDefaultStyle: () => void;
}) {
  const [previewCase, setPreviewCase] = useState<PreviewCase>(DEFAULT_PREVIEW_CASE);
  const [comparison, setComparison] = useState(false);
  const selectedSample = previewCase === "2-line" && comparison ? "2-line-comparison" : previewCase;
  const [manifest, setManifest] = useState<PreviewCatalogManifest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [readyAssets, setReadyAssets] = useState("");
  const [inspect, setInspect] = useState(false);
  const [background, setBackground] = useState<"slate" | "charcoal">("slate");
  const backgroundImage = `/previews/v3/${background}.webp`;
  const [safeZone, setSafeZone] = useState(true);
  const [guides, setGuides] = useState(false);
  const [retry, setRetry] = useState(0);
  const id = useId().replace(/:/g, "");

  useEffect(() => {
    let active = true;
    setError(null);
    loadPreviewCatalog().then(m => { if (active) setManifest(m); })
      .catch(e => { if (active) setError(String(e.message)); });
    return () => { active = false; };
  }, [retry]);

  let selected: ReturnType<typeof selectPreview> | null = null;
  let selectionError: string | null = null;
  if (manifest) {
    try { selected = selectPreview(manifest, config, selectedSample); }
    catch (e) { selectionError = e instanceof Error ? e.message : "Preview layout unavailable."; }
  }
  const sources = selected ? [backgroundImage, selected.header, ...selected.glyphs.map(g => g.image)] : [];
  const assetKey = sources.join("|");
  useEffect(() => {
    if (!assetKey) return;
    let active = true;
    setError(null);
    Promise.all(assetKey.split("|").map(src => {
      const image = new window.Image();
      image.src = src;
      return image.decode();
    })).then(() => { if (active) setReadyAssets(assetKey); })
      .catch(() => { if (active) setError("A static preview image could not be loaded. Retry to reload the catalog."); });
    return () => { active = false; };
  }, [assetKey, retry]);

  const ready = selected && manifest && readyAssets === assetKey;
  const failure = error || selectionError;
  const gap = config.arabic_translation_gap_px;
  const renderCanvas = (detail: boolean) => {
    if (!selected || !manifest) return null;
    const { layout, glyphs } = selected;
    const safe = manifest.safe_zone;
    const headerShift = config.sura_header_top_offset - reelLayout.suraHeaderOffset.default;
    const [left, top, right, bottom] = layout.backing;
    const filterId = `${id}-${detail ? "detail" : "reel"}`;
    const arOutline = { none: 0, minimal: 0.55, thin: 0.75, medium: 1 }[config.text_outline];
    const trOutline = { none: 0, minimal: 0.45, thin: 0.70, medium: 1 }[config.text_outline];
    const shadow = { off: 0, very_soft: 0.7, soft: 1, medium: 1.5 }[config.text_shadow];
    const shadowOpacity = { off: 0, very_soft: 47 / 255, soft: 71 / 255, medium: 111 / 255 }[config.text_shadow];
    return <svg xmlns="http://www.w3.org/2000/svg" viewBox={detail ? `0 ${top - 40} 1080 ${bottom - top + 80}` : "0 0 1080 1920"}
      role="img" aria-label={`${previewCase} renderer layout, Arabic gap ${gap}px${detail ? ", enlarged text detail" : ", 1080 by 1920"}`}>
      <defs>
        {[0, 1].map(i => <filter key={i} id={`${filterId}-${i}`} x="-10%" y="-10%" width="120%" height="120%" colorInterpolationFilters="sRGB">
          <feMorphology in="SourceAlpha" operator="dilate" radius={i === 0 ? arOutline : trOutline} result="expanded" />
          <feFlood floodColor={i === 0 ? manifest.colors[config.highlight_color] : "#000"} floodOpacity={i === 0 ? 1 : 63 / 255} />
          <feComposite in2="expanded" operator="in" result="outline" />
          <feFlood floodColor={i === 0 ? manifest.colors[config.highlight_color] : config.highlight_intensity === "strong" ? "#f4f4f4" : config.highlight_intensity === "subtle" ? "#e4dfd8" : "#ece6e0"} />
          <feComposite in2="SourceAlpha" operator="in" result="colored" />
          <feMerge><feMergeNode in="outline" /><feMergeNode in="colored" /></feMerge>
          <feDropShadow dx={i === 1 && shadow > 1 ? 1 : shadow} dy={i === 1 && shadow > 1 ? 1 : shadow} stdDeviation="0" floodOpacity={shadowOpacity} />
        </filter>)}
        <filter id={`${filterId}-backing`}><feGaussianBlur stdDeviation="3" /></filter>
      </defs>
      <image href={backgroundImage} width="1080" height="1920" />
      <rect width="1080" height="1920" fill="black" opacity={manifest.overlay_darkness} />
      <image href={selected.header} y={headerShift} width={reelLayout.width} height="300" />
      {safeZone && !detail && <rect data-preview-safe-zone="true" x={safe.x} y={safe.y} width={safe.width} height={safe.height} fill="none" stroke="#e2e8f0" strokeOpacity="0.4" strokeWidth="2" strokeDasharray="10 8" />}
      {(config.subtitle_backing === "localized_subtle" || config.subtitle_backing === "soft_translucent") &&
        <rect x={left} y={top} width={right - left} height={bottom - top} fill="#1e1814"
          opacity={config.subtitle_backing === "localized_subtle" ? 39 / 255 : 63 / 255} filter={`url(#${filterId}-backing)`} />}
      {glyphs.map((glyph, i) => <image key={layout.glyphs[i]} href={glyph.image}
        data-preview-block={i === 0 ? "arabic" : config.translation_languages[i - 1]}
        x={layout.anchor_x + glyph.left_offset - 2} y={layout.tops[i] - 2}
        width={glyph.right_offset - glyph.left_offset + 4} height={glyph.bottom_offset - glyph.top_offset + 4}
        filter={`url(#${filterId}-${i === 0 ? 0 : 1})`} />)}
      {guides && <g fill="none" stroke="#38bdf8" strokeWidth="2" opacity="0.9">
        {!detail && <path d={`M80 ${manifest.header_top + headerShift} H1000 M540 100 V1640`} strokeDasharray="8 8" />}
        {glyphs.map((glyph, i) => <rect key={i} x={layout.anchor_x + glyph.left_offset} y={layout.tops[i]}
          width={glyph.right_offset - glyph.left_offset} height={glyph.bottom_offset - glyph.top_offset} />)}
        {glyphs[0].visual_line_ranges.map(([lineTop, lineBottom], i) => <rect key={`line-${i}`}
          x={layout.anchor_x + glyphs[0].left_offset} y={layout.tops[0] + lineTop}
          width={glyphs[0].right_offset - glyphs[0].left_offset} height={lineBottom - lineTop}
          stroke="#fbbf24" strokeDasharray="6 5" />)}
        <path d={`M960 ${layout.tops[1] - gap} v${gap} m-8 0 h16 M952 ${layout.tops[1] - gap} h16`} />
        <text x="945" y={layout.tops[1] - gap / 2 + 8} fill="#7dd3fc" stroke="none" textAnchor="end" fontSize="24">{gap}px</text>
      </g>}
    </svg>;
  };

  return <section id="reel-preview" className="static-preview-panel" aria-label="Static renderer preview">
    <a className="preview-mobile-jump" href="#reel-visual-style">Back to visual settings</a>
    <div className="static-preview-heading">
      <h3>Subtitle preview</h3>
    </div>
    <div className="preview-case-row">
      <span id={`${id}-case-label`}>Preview content</span>
      <div className="preview-case-selector" role="group" aria-labelledby={`${id}-case-label`}>
        {PREVIEW_CASES.map((value, index) => <button key={value} type="button" aria-pressed={previewCase === value}
          onClick={() => setPreviewCase(value)}>{`${index + 1} ${index === 0 ? "Line" : "Lines"}`}</button>)}
      </div>
    </div>
    {previewCase === "2-line" && <div className="preview-background-row">
      <label htmlFor={`${id}-sample`}>Two-line sample</label>
      <select id={`${id}-sample`} className="form-input" value={comparison ? "comparison" : "dense"}
        onChange={e => setComparison(e.target.value === "comparison")}>
        <option value="dense">Sketchbook</option>
        <option value="comparison">Studio window</option>
      </select>
    </div>}
    <div className="preview-background-row">
      <label htmlFor={`${id}-background`}>Preview background</label>
      <select id={`${id}-background`} className="form-input" value={background}
        onChange={e => setBackground(e.target.value as "slate" | "charcoal")}>
        <option value="slate">Slate gradient</option><option value="charcoal">Charcoal gradient</option>
      </select>
    </div>
    <div className="preview-toolbar">
      <span><strong>{gap}px</strong> Arabic ↔ translation</span>
      <label><input type="checkbox" checked={safeZone} onChange={e => setSafeZone(e.target.checked)} /> Safe Zone</label>
      <label><input type="checkbox" checked={guides} onChange={e => setGuides(e.target.checked)} /> Guides</label>
      <button type="button" className="btn btn-secondary" aria-pressed={inspect} onClick={() => setInspect(!inspect)}><ZoomIn size={14} /> Text detail</button>
    </div>
    {inspect && ready && <div className="preview-detail" aria-label="Enlarged text detail">{renderCanvas(true)}</div>}
    <div className="static-preview-frame" aria-busy={!ready && !failure}>
      {ready && !failure ? renderCanvas(false) : <div className={`static-preview-placeholder${failure ? " static-preview-error" : ""}`}>
        {failure && <AlertCircle size={24} />}<span role={failure ? "alert" : "status"}>{failure || "Loading native renderer preview…"}</span>
        {failure && <button type="button" className="btn btn-secondary" onClick={() => { setManifest(null); setReadyAssets(""); setRetry(r => r + 1); }}>Retry preview</button>}
      </div>}
    </div>
    <div className="static-preview-summary" aria-live="polite">
      <span>{selected ? `Typography sample · ${selected.sample.label} · Arabic ${selected.glyphs[0].visual_line_ranges.length} line(s) · ${config.translation_languages.map((lang, i) => `${lang.toUpperCase()} ${selected!.glyphs[i + 1].visual_line_ranges.length} line(s)`).join(" · ")}` : "Static libass catalog"}</span>
      {selected && selected.glyphs[0].visual_line_ranges.length > 1 && <span>Arabic clear line gap: {selected.glyphs[0].visual_line_ranges.slice(1).map(([top], i) => `${top - selected!.glyphs[0].visual_line_ranges[i][1]}px`).join(" / ")} · including marks</span>}
      <span>1080 × 1920 · 9:16 · All measurements are final Reel pixels</span>
    </div>
    {selected?.exceedsLines && <p className="preview-fit-warning" role="status">This sample exceeds your maximum lines. The preview shows the full sample. Each production panel receives its own fit check.</p>}
    <p className="form-hint preview-note">The preview uses original studio-themed sample text. Line counts are measured at the largest font size. Smaller fonts may wrap differently. Libass supplies glyphs and positions. Browser effects approximate outline, shadow and backing. Header shadows use the default setting.</p>
    <div className="static-preview-actions"><button type="button" className="btn btn-secondary" onClick={onRestoreDefaultStyle}><RotateCcw size={14} /> Restore Default Style</button><span className="form-hint">Changes the unsaved visual settings only.</span></div>
  </section>;
}
