"use client";

import React, { useState } from "react";
import { Play, Film, AlertCircle, RefreshCw } from "lucide-react";

export function MediaPreviewPlayer({ mediaKey }: { mediaKey?: string | null }) {
  const [loading, setLoading] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!mediaKey) {
    return (
      <div style={{ color: "var(--text-muted)", fontSize: "12px", fontStyle: "italic" }}>
        No media file associated with this run record.
      </div>
    );
  }

  const handleRequestPreview = async () => {
    setLoading(true);
    setError(null);
    setExpired(false);

    try {
      const res = await fetch("/api/actions/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mediaKey }),
      });

      const data = await res.json();
      if (res.ok && data.exists && data.previewUrl) {
        setPreviewUrl(data.previewUrl);
      } else if (res.ok && !data.exists) {
        setExpired(true);
      } else {
        setError(data.error || "Failed to load media preview.");
      }
    } catch {
      setError("Network error fetching preview URL.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        padding: "16px",
        background: "var(--bg-app)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <Film size={16} color="var(--primary)" />
          <span style={{ fontSize: "13px", fontWeight: 600 }}>Reel Video Preview</span>
        </div>

        {!previewUrl && !expired && (
          <button
            onClick={handleRequestPreview}
            disabled={loading}
            className="btn btn-primary"
            style={{ padding: "4px 10px", fontSize: "12px" }}
          >
            {loading ? <RefreshCw size={12} className="spin" /> : <Play size={12} />}
            <span>{loading ? "Generating Secure URL..." : "Load Preview"}</span>
          </button>
        )}
      </div>

      {expired && (
        <div
          style={{
            padding: "12px",
            background: "var(--status-neutral-bg)",
            border: "1px solid var(--status-neutral-border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text-muted)",
            fontSize: "12px",
            textAlign: "center",
          }}
        >
          <strong>Media Expired:</strong> The rendered video file has been deleted from Backblaze B2 under the 7-day retention policy. Audit metadata remains permanently preserved.
        </div>
      )}

      {error && (
        <div
          style={{
            padding: "10px",
            background: "var(--status-failed-bg)",
            border: "1px solid var(--status-failed-border)",
            borderRadius: "var(--radius-sm)",
            color: "var(--status-failed-text)",
            fontSize: "12px",
            display: "flex",
            alignItems: "center",
            gap: "6px",
          }}
        >
          <AlertCircle size={14} />
          <span>{error}</span>
        </div>
      )}

      {previewUrl && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "10px" }}>
          <video
            src={previewUrl}
            controls
            playsInline
            style={{
              width: "100%",
              maxWidth: "280px",
              aspectRatio: "9/16",
              borderRadius: "var(--radius-md)",
              background: "#000",
              border: "1px solid var(--border-default)",
            }}
          />
          <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>
            Temporary presigned preview link (expires in 10 minutes)
          </span>
        </div>
      )}
    </div>
  );
}
