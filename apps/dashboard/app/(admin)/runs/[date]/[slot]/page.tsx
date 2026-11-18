import Link from "next/link";
import { notFound } from "next/navigation";
import { getRunRecord } from "@/lib/b2";
import { GenerationStatusBadge, PlatformStatusBadge } from "@/components/StatusBadges";
import { MediaPreviewPlayer } from "@/components/MediaPreviewPlayer";
import {
  ArrowLeft,
  Calendar,
  Clock,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  Film,
  Facebook,
  Instagram,
  CheckCircle2,
  XCircle,
  Layers,
} from "lucide-react";

export const dynamic = "force-dynamic";

export default async function RunDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ date: string; slot: string }>;
  searchParams: Promise<{ runId?: string }>;
}) {
  const { date, slot } = await params;
  const { runId } = await searchParams;

  const run = await getRunRecord(date, slot, runId);
  if (!run) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
        <Link href="/runs" className="btn btn-secondary" style={{ alignSelf: "flex-start" }}>
          <ArrowLeft size={14} />
          <span>Back to Runs</span>
        </Link>
        <div className="card" style={{ padding: "40px", textAlign: "center" }}>
          <h2 style={{ fontSize: "16px", fontWeight: 600, color: "var(--status-failed-text)" }}>
            Run Record Not Found
          </h2>
          <p style={{ color: "var(--text-muted)", fontSize: "13px", marginTop: "8px" }}>
            Could not find an audit record for date <code>{date}</code>, slot <code>{slot}</code>
            {runId ? ` (runId: ${runId})` : ""} in Backblaze B2.
          </p>
        </div>
      </div>
    );
  }

  const hasUnknownStatus = run.facebook_status === "UNKNOWN" || run.instagram_status === "UNKNOWN";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
      {/* Top Breadcrumb & Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Link href="/runs" className="btn btn-secondary" style={{ padding: "6px 12px", fontSize: "12px" }}>
          <ArrowLeft size={13} />
          <span>Back to Runs</span>
        </Link>
        <div style={{ color: "var(--text-muted)", fontSize: "12px", fontFamily: "var(--font-mono)" }}>
          Run ID: {run.run_id}
        </div>
      </div>

      <div>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <h1 style={{ fontSize: "20px", fontWeight: 600 }}>
            Run Details: <span style={{ textTransform: "capitalize" }}>{run.slot}</span> ({run.date})
          </h1>
          <GenerationStatusBadge status={run.status} />
        </div>
        <p style={{ color: "var(--text-muted)", fontSize: "13px", marginTop: "4px" }}>
          Recorded at {run.started_at ? new Date(run.started_at).toLocaleString("en-GB") : "Unknown"}
        </p>
      </div>

      {/* UNKNOWN Social Status Banner */}
      {hasUnknownStatus && (
        <div
          style={{
            padding: "16px",
            background: "var(--status-unknown-bg)",
            border: "1px solid var(--status-unknown-border)",
            borderRadius: "var(--radius-md)",
            color: "var(--status-unknown-text)",
            display: "flex",
            alignItems: "flex-start",
            gap: "12px",
          }}
        >
          <AlertTriangle size={20} style={{ flexShrink: 0, marginTop: "2px" }} />
          <div>
            <h3 style={{ fontSize: "14px", fontWeight: 600 }}>
              Manual Verification Required Before Retry
            </h3>
            <p style={{ fontSize: "12px", marginTop: "4px", lineHeight: 1.5 }}>
              Meta social media publishing returned an ambiguous network outcome during the publish phase. Automatic republishing has been disabled to prevent potential duplicate postings. Please verify publication directly in Meta Business Suite before performing any manual retry.
            </p>
          </div>
        </div>
      )}

      {/* Main Grid: Content Details & Preview */}
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "20px" }} className="grid-2">
        {/* Left Column: Generation & Social Publishing Details */}
        <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          {/* Generation & Verse Details */}
          <div className="card">
            <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "16px" }}>Quran & Generation Metadata</h2>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }} className="grid-2">
              <div>
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Surah Arabic / English</span>
                <div style={{ fontSize: "14px", fontWeight: 600, marginTop: "2px" }}>
                  {run.surah_name_ar ? `${run.surah_name_ar} • ${run.surah_name_en}` : "—"}
                </div>
              </div>

              <div>
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Ayah Range</span>
                <div style={{ fontSize: "14px", fontWeight: 600, marginTop: "2px" }}>
                  {run.first_ayah !== undefined && run.first_ayah !== null
                    ? `Surah ${run.surah_number}:${run.first_ayah}–${run.last_ayah}`
                    : "—"}
                </div>
              </div>

              <div>
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Rendered Duration</span>
                <div style={{ fontSize: "14px", fontWeight: 600, marginTop: "2px" }}>
                  {run.duration_seconds ? `${run.duration_seconds.toFixed(2)} seconds` : "—"}
                </div>
              </div>

              <div>
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>Visual Safety Verification</span>
                <div style={{ display: "flex", alignItems: "center", gap: "6px", marginTop: "2px" }}>
                  {run.final_safety_passed ? (
                    <>
                      <ShieldCheck size={16} color="var(--status-success-text)" />
                      <span style={{ color: "var(--status-success-text)", fontWeight: 500, fontSize: "13px" }}>
                        Passed (Zero Humans/Faces)
                      </span>
                    </>
                  ) : (
                    <>
                      <ShieldAlert size={16} color="var(--status-failed-text)" />
                      <span style={{ color: "var(--status-failed-text)", fontWeight: 500, fontSize: "13px" }}>
                        {run.final_safety_passed === false ? "Failed Safety Check" : "Not Verified"}
                      </span>
                    </>
                  )}
                </div>
              </div>
            </div>

            <div style={{ marginTop: "16px", fontSize: "12px", color: "var(--text-muted)" }}>
              <strong>Translations:</strong> {run.translation_languages.map((language) => language.toUpperCase()).join(" + ")}
              {Object.entries(run.translation_sources).map(([language, source]) => (
                <div key={language} style={{ marginTop: "4px" }}>
                  {language.toUpperCase()}: {source.source} · <code>{source.translation_key}</code> · version {source.version}
                </div>
              ))}
            </div>

            {run.failure_reason && (
              <div
                style={{
                  marginTop: "16px",
                  padding: "10px 12px",
                  background: "var(--status-failed-bg)",
                  border: "1px solid var(--status-failed-border)",
                  borderRadius: "var(--radius-sm)",
                  color: "var(--status-failed-text)",
                  fontSize: "12px",
                }}
              >
                <strong>Generation Error:</strong> {run.failure_reason}
              </div>
            )}
          </div>

          {/* Social Publishing Details */}
          <div className="card">
            <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "16px" }}>Social Media Publishing Breakdown</h2>

            <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              {/* Facebook Card */}
              <div
                style={{
                  padding: "14px",
                  background: "var(--bg-app)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-md)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    <Facebook size={16} color="var(--primary)" />
                    <span style={{ fontWeight: 600 }}>Facebook Page (Primary)</span>
                  </div>
                  <PlatformStatusBadge platform="facebook" status={run.facebook_status} isPrimary />
                </div>

                <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                  Media ID: <code>{run.facebook_media_id || "None"}</code>
                </div>

                {run.facebook_failure_reason && (
                  <div
                    style={{
                      marginTop: "8px",
                      padding: "8px",
                      background: "var(--status-failed-bg)",
                      border: "1px solid var(--status-failed-border)",
                      borderRadius: "var(--radius-sm)",
                      color: "var(--status-failed-text)",
                      fontSize: "12px",
                    }}
                  >
                    <strong>Failure:</strong> {run.facebook_failure_reason}
                  </div>
                )}
              </div>

              {/* Instagram Card */}
              <div
                style={{
                  padding: "14px",
                  background: "var(--bg-app)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-md)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    <Instagram size={16} color="var(--primary)" />
                    <span style={{ fontWeight: 600 }}>Instagram Account (Optional)</span>
                  </div>
                  <PlatformStatusBadge platform="instagram" status={run.instagram_status} />
                </div>

                <div style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                  Media ID: <code>{run.instagram_media_id || "None"}</code>
                </div>

                {run.instagram_failure_reason && (
                  <div
                    style={{
                      marginTop: "8px",
                      padding: "8px",
                      background: "var(--status-blocked-bg)",
                      border: "1px solid var(--status-blocked-border)",
                      borderRadius: "var(--radius-sm)",
                      color: "var(--status-blocked-text)",
                      fontSize: "12px",
                    }}
                  >
                    <strong>Notice:</strong> {run.instagram_failure_reason}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Background Footage & Videographer Provenance */}
          <div className="card">
            <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "12px" }}>Footage Provenance & Credits</h2>

            <div style={{ fontSize: "13px" }}>
              <span style={{ color: "var(--text-muted)" }}>Pexels Video IDs: </span>
              {run.pexels_video_ids && run.pexels_video_ids.length > 0 ? (
                <span>{run.pexels_video_ids.join(", ")}</span>
              ) : (
                <span style={{ color: "var(--text-muted)" }}>None</span>
              )}
            </div>

            {run.provenance && Object.keys(run.provenance).length > 0 && (
              <div style={{ marginTop: "12px" }}>
                <span style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "4px" }}>
                  Detailed Provenance Record:
                </span>
                <pre
                  style={{
                    padding: "10px",
                    background: "var(--bg-app)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "var(--radius-sm)",
                    fontSize: "11px",
                    overflowX: "auto",
                    color: "var(--text-secondary)",
                  }}
                >
                  {JSON.stringify(run.provenance, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>

        {/* Right Column: Media Preview */}
        <div>
          <MediaPreviewPlayer mediaKey={run.media_key} />
        </div>
      </div>
    </div>
  );
}
