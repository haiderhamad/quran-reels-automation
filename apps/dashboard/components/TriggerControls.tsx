"use client";

import React, { useEffect, useState } from "react";
import { ExternalLink, RefreshCw, Search, Send, ShieldAlert, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";

type ActionKind = "test" | "manual_production";

export function TriggerControls({
  publishAllowed,
  publishBlockedReason,
  facebookConnected,
  instagramConnected,
}: {
  publishAllowed: boolean;
  publishBlockedReason?: string;
  facebookConnected: boolean;
  instagramConnected: boolean;
}) {
  const router = useRouter();
  const [loading, setLoading] = useState<ActionKind | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [detailUrl, setDetailUrl] = useState<string | null>(null);
  const [result, setResult] = useState<{ success: boolean; message: string; workflowUrl?: string } | null>(null);
  const [publishFacebook, setPublishFacebook] = useState(true);
  const [publishInstagram, setPublishInstagram] = useState(true);

  useEffect(() => {
    if (!runId || runStatus === "SUCCESS" || runStatus === "FAILED") return;
    const poll = window.setInterval(async () => {
      try {
        const response = await fetch(`/api/actions/status?runId=${encodeURIComponent(runId)}`);
        if (!response.ok) return;
        const data = await response.json();
        if (data.found && data.run?.status) {
          setRunStatus(data.run.status);
          setDetailUrl(`/runs/${data.run.date}/${data.run.slot}?runId=${encodeURIComponent(data.run.run_id)}`);
          if (data.run.status === "SUCCESS" || data.run.status === "FAILED") router.refresh();
        }
      } catch {
        // A transient status-poll failure does not alter the accepted dispatch.
      }
    }, 5000);
    return () => window.clearInterval(poll);
  }, [runId, runStatus, router]);

  const handleTrigger = async (runKind: ActionKind) => {
    setLoading(runKind);
    setResult(null);
    setRunId(null);
    setRunStatus(null);
    setDetailUrl(null);
    try {
      const response = await fetch("/api/actions/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: runKind === "test" ? "generate_test" : "publish_now",
          run_kind: runKind,
          publish_facebook: runKind === "manual_production" ? publishFacebook : undefined,
          publish_instagram: runKind === "manual_production" ? publishInstagram : undefined,
        }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) {
        setResult({ success: false, message: data.error || "The generation request was not accepted." });
        return;
      }
      setRunId(data.runId);
      setRunStatus("DISPATCHED");
      setResult({ success: true, message: data.message, workflowUrl: data.workflowUrl });
    } catch {
      setResult({ success: false, message: "Network error while requesting GitHub Actions." });
    } finally {
      setLoading(null);
    }
  };

  const handleDiscovery = async () => {
    setLoading("test");
    setResult(null);
    try {
      const response = await fetch("/api/actions/trigger", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "background_discovery" }) });
      const data = await response.json();
      setResult({ success: response.ok && data.success, message: data.message || data.error || "Background discovery request was not accepted.", workflowUrl: data.workflowUrl });
    } catch {
      setResult({ success: false, message: "Network error while requesting background discovery." });
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="card-elevated" style={{ marginTop: "20px" }}>
      <h3 style={{ fontSize: "14px", fontWeight: 600 }}>Publishing</h3>
      <p style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "2px", marginBottom: "16px" }}>
        Generate Test Now privately renders an eligible ayah from the frozen catalog. Publish Now creates a production run.
      </p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "10px" }}>
        <button type="button" onClick={() => handleTrigger("test")} disabled={loading !== null} className="btn btn-primary">
          {loading === "test" ? <RefreshCw size={14} className="spin" /> : <Sparkles size={14} />}
          <span>Generate Test Now</span>
        </button>
        <button type="button" onClick={() => handleTrigger("manual_production")} disabled={loading !== null || !publishAllowed} className="btn btn-secondary" title={publishAllowed ? "Generate and publish the next eligible production Reel" : publishBlockedReason || "Publishing readiness is incomplete"}>
          {loading === "manual_production" ? <RefreshCw size={14} className="spin" /> : <Send size={14} />}
          <span>Publish Now</span>
        </button>
        <button type="button" onClick={handleDiscovery} disabled={loading !== null} className="btn btn-secondary">
          {loading === "test" ? <RefreshCw size={14} className="spin" /> : <Search size={14} />}
          <span>Find Backgrounds Now</span>
        </button>
      </div>
      <fieldset disabled={loading !== null || !publishAllowed} style={{ border: 0, padding: 0, margin: "12px 0 0", display: "flex", gap: "14px", fontSize: "12px" }}>
        <legend style={{ color: "var(--text-muted)", marginBottom: "5px" }}>Publishing targets</legend>
        <label><input type="checkbox" checked={publishFacebook} onChange={(event) => setPublishFacebook(event.target.checked)} /> Facebook {facebookConnected ? "(connected)" : "(not configured)"}</label>
        <label><input type="checkbox" checked={publishInstagram} onChange={(event) => setPublishInstagram(event.target.checked)} /> Instagram {instagramConnected ? "(connected)" : "(not configured)"}</label>
      </fieldset>
      {!publishAllowed && (
        <div style={{ marginTop: "12px", display: "flex", gap: "7px", color: "var(--status-blocked-text)", fontSize: "12px" }}>
          <ShieldAlert size={15} style={{ flexShrink: 0 }} />
          <span>Publish Now is blocked: {publishBlockedReason || "publishing readiness is incomplete."} Generate Test Now remains available.</span>
        </div>
      )}
      {result && (
        <div style={{ marginTop: "14px", padding: "10px 14px", borderRadius: "var(--radius-md)", border: `1px solid ${result.success ? "var(--status-success-border)" : "var(--status-failed-border)"}`, background: result.success ? "var(--status-success-bg)" : "var(--status-failed-bg)", color: result.success ? "var(--status-success-text)" : "var(--status-failed-text)", fontSize: "13px" }}>
          <div>{result.message}</div>
          {runId && <div style={{ marginTop: "4px", fontFamily: "var(--font-mono)", fontSize: "11px" }}>Run: {runId} · Status: {runStatus}</div>}
          {detailUrl && <a href={detailUrl} style={{ display: "inline-flex", marginTop: "6px" }}>{runStatus === "SUCCESS" ? "Open Reel preview" : "Open run details"}</a>}
          {result.workflowUrl && <a href={result.workflowUrl} target="_blank" rel="noopener noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: "4px", marginTop: "6px" }}>View workflow <ExternalLink size={11} /></a>}
        </div>
      )}
    </div>
  );
}
