import Link from "next/link";
import { listRecentRunRecords } from "@/lib/b2";
import { GenerationStatusBadge, PlatformStatusBadge } from "@/components/StatusBadges";
import { History, ArrowRight } from "lucide-react";

export const dynamic = "force-dynamic";

export default async function RunsHistoryPage() {
  const runs = await listRecentRunRecords(50);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <h1 style={{ fontSize: "20px", fontWeight: 600 }}>Technical Run History / Audit</h1>
          <p style={{ color: "var(--text-muted)", fontSize: "13px", marginTop: "2px" }}>
            Audit log of scheduled and manual Quran Reel generation jobs.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "6px", color: "var(--text-muted)", fontSize: "12px" }}>
          <History size={14} />
          <span>Showing latest {runs.length} runs</span>
        </div>
      </div>

      <div className="card">
        {runs.length === 0 ? (
          <div style={{ padding: "32px", textAlign: "center", color: "var(--text-muted)", fontSize: "13px" }}>
            No run records found in the authoritative state store.
          </div>
        ) : (
          <div className="table-container technical-runs-container">
            <table className="table technical-runs-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Slot</th>
                  <th>Surah & Ayahs</th>
                  <th>Duration</th>
                  <th>Generation</th>
                  <th>Facebook</th>
                  <th>Instagram</th>
                  <th>Started At</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={`${run.date}-${run.slot}-${run.run_id}`}>
                    <td data-label="Date" style={{ fontWeight: 500 }}>{run.date}</td>
                    <td data-label="Slot">
                      <span style={{ textTransform: "capitalize" }}>{run.slot}</span>
                    </td>
                    <td data-label="Surah & Ayahs">
                      {run.surah_name_en ? (
                        <span>
                          {run.surah_name_en} ({run.surah_number}:{run.first_ayah}–{run.last_ayah})
                        </span>
                      ) : (
                        <span style={{ color: "var(--text-muted)" }}>—</span>
                      )}
                    </td>
                    <td data-label="Duration">{run.duration_seconds ? `${run.duration_seconds.toFixed(1)}s` : "—"}</td>
                    <td data-label="Generation">
                      <GenerationStatusBadge status={run.status} />
                    </td>
                    <td data-label="Facebook">
                      <PlatformStatusBadge platform="facebook" status={run.facebook_status} isPrimary />
                    </td>
                    <td data-label="Instagram">
                      <PlatformStatusBadge platform="instagram" status={run.instagram_status} />
                    </td>
                    <td data-label="Started At" style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                      {run.started_at ? new Date(run.started_at).toLocaleTimeString("en-GB") : "—"}
                    </td>
                    <td data-label="Action">
                      <Link
                        href={`/runs/${run.date}/${run.slot}?runId=${encodeURIComponent(run.run_id)}`}
                        className="btn btn-secondary"
                        style={{ padding: "3px 8px", fontSize: "11px" }}
                      >
                        <span>Details</span>
                        <ArrowRight size={11} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
