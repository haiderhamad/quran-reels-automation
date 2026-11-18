import React from "react";
import type {
  getBackgroundCandidateSummaryFromSupabase,
  getBackgroundPoolOperationsSummaryFromSupabase,
  getBackgroundDiscoveryStateFromSupabase,
  getRecentBackgroundReuseFromSupabase,
} from "@/lib/supabase-state";

type Props = {
  candidateSummary: Awaited<ReturnType<typeof getBackgroundCandidateSummaryFromSupabase>>;
  poolSummary: Awaited<ReturnType<typeof getBackgroundPoolOperationsSummaryFromSupabase>>;
  discoveryState: Awaited<ReturnType<typeof getBackgroundDiscoveryStateFromSupabase>>;
  reuseSummary: Awaited<ReturnType<typeof getRecentBackgroundReuseFromSupabase>>;
};

const subtle = { color: "var(--text-muted)", fontSize: "12px" } as const;
const group = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: "12px", fontSize: "12px" } as const;
const sectionTitle = { fontSize: "13px", fontWeight: 600, marginBottom: "10px" } as const;

function utc(value: string | null | undefined): string {
  return value ? value.slice(0, 16).replace("T", " ") + " UTC" : "—";
}

export function BackgroundOperationsCard({ candidateSummary, poolSummary, discoveryState, reuseSummary }: Props) {
  const poolCount = (value: number) => poolSummary.available ? value : "—";
  const queueCount = (value: number) => candidateSummary.available ? value : "—";
  const unusedPercent = poolSummary.available && poolSummary.selectable > 0
    ? (poolSummary.unused / poolSummary.selectable) * 100 : 0;

  return (
    <section className="card-elevated" aria-labelledby="background-pool-heading">
      <h2 id="background-pool-heading" style={{ fontSize: "15px", fontWeight: 600, marginBottom: "6px" }}>Background Pool</h2>
      <p style={{ ...subtle, marginBottom: "16px" }}>
        One shared pool for Mac and GitHub Actions. Only backgrounds with completed safety checks are available for Reels.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: "12px", marginBottom: "18px" }}>
        <div style={{ padding: "14px", border: "1px solid var(--border-color, #374151)", borderRadius: "8px" }}>
          <strong style={{ fontSize: "13px" }}>Ready for Reels</strong>
          {!poolSummary.available && <div role="status" style={subtle}>Pool counts unavailable</div>}
          <div style={{ fontSize: "27px", fontWeight: 650, marginTop: "4px" }}>{poolCount(poolSummary.selectable)}</div>
          <div style={{ ...subtle, marginTop: "4px" }}>
            {poolCount(poolSummary.unused)} never used + {poolCount(poolSummary.used)} used before = {poolCount(poolSummary.selectable)} ready
          </div>
          {poolSummary.available && poolSummary.selectable > 0 && (
            <div role="img" aria-label={`${poolSummary.unused} unused and ${poolSummary.used} previously used backgrounds`}
              style={{ display: "flex", width: "100%", height: "8px", overflow: "hidden", borderRadius: "4px", marginTop: "10px", background: "var(--surface-secondary, #334155)" }}>
              <span style={{ width: `${unusedPercent}%`, background: "var(--accent, #2A9D8F)" }} />
              <span style={{ flex: 1, background: "var(--text-muted, #64748b)" }} />
            </div>
          )}
          <p style={{ ...subtle, marginTop: "8px" }}>Previously used clips remain reusable after the required checks; they are not extra assets.</p>
        </div>
        <div style={{ padding: "14px", border: "1px solid var(--border-color, #374151)", borderRadius: "8px" }}>
          <strong style={{ fontSize: "13px" }}>Inventory and exclusions</strong>
          <div style={{ fontSize: "12px", marginTop: "10px", display: "grid", gap: "7px" }}>
            <div><strong>{poolCount(poolSummary.total)}</strong> total records · <strong>{poolCount(poolSummary.verified)}</strong> verified records</div>
            <div><strong>{poolCount(poolSummary.highRisk)}</strong> high-risk flags · <strong>{poolCount(poolSummary.stale)}</strong> stale records</div>
          </div>
          <p style={{ ...subtle, marginTop: "10px" }}>Inventory counts records/variants; ready counts unique selectable videos. Exclusions can overlap, so do not add or subtract these figures.</p>
        </div>
      </div>

      <h3 style={sectionTitle}>Candidate queue <span style={{ ...subtle, fontWeight: 400 }}>(not extra ready videos)</span></h3>
      <div style={{ ...group, marginBottom: "18px" }}>
        <div><strong>Awaiting safety scan</strong><div>{queueCount(candidateSummary.pending)}</div></div>
        <div><strong>Being checked</strong><div>{queueCount(candidateSummary.processing)}</div></div>
        <div><strong>Qualified in queue</strong><div>{queueCount(candidateSummary.verified)}</div></div>
      </div>

      <h3 style={sectionTitle}>Discovery and reuse</h3>
      <div style={group}>
        <div><strong>Last discovery</strong><div>{discoveryState?.completedAt ? utc(discoveryState.completedAt) : "No durable discovery record"}</div></div>
        <div><strong>Last / next category</strong><div>{discoveryState ? `${discoveryState.category} / ${discoveryState.nextCategory}` : "—"}</div></div>
        <div><strong>Query / page cursor</strong><div>{discoveryState ? `${discoveryState.queryCursor} / ${discoveryState.pageCursor}` : "—"}</div></div>
        <div><strong>Recent reuse warnings</strong><div>{reuseSummary.available ? reuseSummary.count : "Unavailable"}</div>
          <div style={subtle}>Last 30 production runs; these are events, not used asset totals.</div>
          {reuseSummary.lastAt && <div style={subtle}>{utc(reuseSummary.lastAt)}</div>}
        </div>
        <div><strong>Last queue activity</strong><div>{utc(candidateSummary.lastActivity)}</div></div>
      </div>
    </section>
  );
}
