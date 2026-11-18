"use client";

import { useEffect, useState } from "react";
import { RefreshCw, ShieldCheck, Square } from "lucide-react";

type PoolProvider = "both" | "pexels" | "pixabay";
type ProviderQueryMetric = {
  provider: "pexels" | "pixabay";
  query: string;
  scanned: number;
  safe: number;
};
type PoolStatus = {
  statusSchemaVersion: number;
  state: string;
  provider: PoolProvider;
  targetNewSafe: number;
  foundSafe: number;
  discovered: number;
  metadataSkipped: number;
  downloads: number;
  safetyScanned: number;
  safetyRejected: number;
  transientFailures: number;
  currentCandidate: { provider: "pexels" | "pixabay"; query: string | null; assetId: string } | null;
  elapsedSeconds: number;
  policyFingerprint: string | null;
  stats: Record<string, number>;
  providerQueryMetrics: ProviderQueryMetric[];
  b2Operations: { listOperations: number; metadataReads: number; writes: number };
  message: string | null;
};

const TARGETS = [5, 10, 20, 50] as const;
const PROVIDERS: readonly { value: PoolProvider; label: string }[] = [
  { value: "both", label: "Both" },
  { value: "pexels", label: "Pexels" },
  { value: "pixabay", label: "Pixabay" },
];
const ACTIVE = new Set(["starting", "scanning", "stopping"]);
const FAILED = new Set(["failed", "technical_error"]);

function elapsedLabel(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  return minutes > 0 ? `${minutes}m ${remaining}s` : `${remaining}s`;
}

function count(stats: Record<string, number>, key: string): number {
  const value = stats[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function stateBadgeClass(state: string | undefined, active: boolean): string {
  if (active) return "badge-blocked";
  if (state && FAILED.has(state)) return "badge-failed";
  if (state === "completed") return "badge-success";
  return "badge-neutral";
}

export function BackgroundPoolBuilder({ configAuthoritative }: { configAuthoritative: boolean }) {
  const [target, setTarget] = useState<number>(5);
  const [provider, setProvider] = useState<PoolProvider>("both");
  const [status, setStatus] = useState<PoolStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const active = status ? ACTIVE.has(status.state) : false;

  const pollProgress = async () => {
    try {
      const response = await fetch("/api/background-pool/builder", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) {
        setError(data.error || "Local pool status is unavailable.");
        return;
      }
      const nextStatus = data.status as PoolStatus;
      setStatus(nextStatus);
      setError(null);
      if (ACTIVE.has(nextStatus.state)) setProvider(nextStatus.provider);
    } catch {
      setError("Network error while reading local pool status.");
    }
  };

  useEffect(() => {
    void pollProgress();
    const timer = window.setInterval(() => void pollProgress(), active ? 2000 : 10000);
    return () => window.clearInterval(timer);
  }, [active]);

  const action = async (name: "start" | "stop") => {
    setError(null);
    try {
      const response = await fetch("/api/background-pool/builder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(name === "start"
          ? { action: name, addSafe: target, provider }
          : { action: name }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.error || "Local pool action failed.");
        return;
      }
      setStatus(data.status);
    } catch {
      setError("Network error while controlling the local pool builder.");
    }
  };

  const refreshPoolTotals = async () => {
    setError(null);
    try {
      const response = await fetch("/api/background-pool/builder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "refresh_totals" }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.error || "Authoritative pool totals refresh failed.");
        return;
      }
      setStatus(data.status);
    } catch {
      setError("Network error while refreshing authoritative pool totals.");
    }
  };

  const stats = status?.stats || {};
  const poolTotals = [
    ["Verified Safe — Current Policy", "verified_current_policy"],
    ["Verified Stale Policy", "verified_stale_policy"],
    ["Known Safety Rejected", "known_safety_rejected"],
    ["Metadata Ineligible", "metadata_ineligible"],
    ["Production Unused", "production_unused"],
    ["Stale / Missing", "missing_or_stale"],
  ] as const;
  const runCounts = [
    ["Target New Safe", status?.targetNewSafe ?? 0],
    ["Safe Found", status?.foundSafe ?? 0],
    ["Discovered", status?.discovered ?? 0],
    ["Metadata Skipped", status?.metadataSkipped ?? 0],
    ["Downloads", status?.downloads ?? 0],
    ["Safety Scanned", status?.safetyScanned ?? 0],
    ["Safety Rejected", status?.safetyRejected ?? 0],
    ["Transient Failures", status?.transientFailures ?? 0],
  ] as const;

  return (
    <section className="card-elevated" aria-labelledby="background-pool-title">
      <div style={{ display: "flex", justifyContent: "space-between", gap: "12px", alignItems: "flex-start" }}>
        <div>
          <h2 id="background-pool-title" style={{ fontSize: "15px", fontWeight: 600 }}>Background Pool Builder</h2>
          <p style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "3px" }}>
            Local Mac only. Source videos are temporary; B2 receives verified JSON metadata only.
          </p>
        </div>
        <span className={`badge ${stateBadgeClass(status?.state, active)}`}>{status?.state || "idle"}</span>
      </div>

      <h3 style={{ fontSize: "12px", fontWeight: 600, marginTop: "18px", textTransform: "uppercase", color: "var(--text-muted)" }}>Pool Totals</h3>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: "10px", marginTop: "8px" }}>
        {poolTotals.map(([label, key]) => (
          <div key={key}><small style={{ color: "var(--text-muted)" }}>{label}</small><div style={{ fontSize: "18px", fontWeight: 600 }}>{count(stats, key)}</div></div>
        ))}
      </div>
      {(stats.pexels_verified_current_policy !== undefined || stats.pixabay_verified_current_policy !== undefined) && (
        <div style={{ color: "var(--text-muted)", fontSize: "11px", marginTop: "6px" }}>
          Pexels Safe: {count(stats, "pexels_verified_current_policy")} · Pixabay Safe: {count(stats, "pixabay_verified_current_policy")}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: "14px", marginTop: "18px" }}>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "6px" }}>Provider</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
            {PROVIDERS.map((item) => (
              <button key={item.value} type="button" aria-pressed={provider === item.value} className={`btn ${provider === item.value ? "btn-primary" : "btn-secondary"}`} disabled={active} onClick={() => setProvider(item.value)}>{item.label}</button>
            ))}
          </div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: "12px", marginBottom: "6px" }}>Add New Safe Clips</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
            {TARGETS.map((value) => <button key={value} type="button" aria-pressed={target === value} className={`btn ${target === value ? "btn-primary" : "btn-secondary"}`} disabled={active} onClick={() => setTarget(value)}>{value}</button>)}
          </div>
        </div>
      </div>

      <div style={{ marginTop: "14px", display: "flex", gap: "10px", flexWrap: "wrap" }}>
        <button type="button" className="btn btn-primary" disabled={active || !configAuthoritative} onClick={() => void action("start")}><ShieldCheck size={14} /> Start Local Scan</button>
        <button type="button" className="btn btn-secondary" disabled={!active} onClick={() => void action("stop")}><Square size={13} /> Stop</button>
        <button type="button" className="btn btn-secondary" disabled={active} onClick={() => void refreshPoolTotals()}><RefreshCw size={13} /> Refresh Pool Totals</button>
      </div>
      {!configAuthoritative && (
        <div role="alert" style={{ marginTop: "8px", color: "var(--status-blocked-text)", fontSize: "12px" }}>
          Start is blocked because StoredConfig could not be loaded authoritatively from B2.
        </div>
      )}

      <h3 style={{ fontSize: "12px", fontWeight: 600, marginTop: "18px", textTransform: "uppercase", color: "var(--text-muted)" }}>Current Run</h3>
      <div aria-live="polite" style={{ marginTop: "8px", fontSize: "12px" }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(135px, 1fr))", gap: "8px" }}>
          {runCounts.map(([label, value]) => <div key={label}><small style={{ color: "var(--text-muted)" }}>{label}</small><div style={{ fontWeight: 600 }}>{value}</div></div>)}
          <div><small style={{ color: "var(--text-muted)" }}>Current Provider</small><div style={{ fontWeight: 600 }}>{status?.currentCandidate?.provider || "—"}</div></div>
          <div><small style={{ color: "var(--text-muted)" }}>Current Query</small><div style={{ fontWeight: 600 }}>{status?.currentCandidate?.query || "—"}</div></div>
          <div><small style={{ color: "var(--text-muted)" }}>Current Asset</small><div style={{ fontWeight: 600 }}>{status?.currentCandidate?.assetId || "—"}</div></div>
          <div><small style={{ color: "var(--text-muted)" }}>Elapsed</small><div style={{ fontWeight: 600 }}>{elapsedLabel(status?.elapsedSeconds ?? 0)}</div></div>
        </div>
        {status?.statusSchemaVersion === 1 && <div style={{ marginTop: "8px", color: "var(--status-blocked-text)" }}>Legacy V1 status; V2 accounting will appear when a V2 builder run starts.</div>}
        {status?.policyFingerprint && <div style={{ marginTop: "8px", color: "var(--text-muted)" }}>Policy: {status.policyFingerprint.slice(0, 12)}</div>}
        {status?.b2Operations && (
          <div style={{ marginTop: "4px", color: "var(--text-muted)" }}>
            B2 operations — lists: {status.b2Operations.listOperations}, reads: {status.b2Operations.metadataReads}, writes: {status.b2Operations.writes}
          </div>
        )}
        {status?.message && <div style={{ marginTop: "4px", color: "var(--text-muted)" }}>{status.message}</div>}
        {error && <div style={{ marginTop: "6px", color: "var(--status-failed-text)" }}>{error}</div>}
      </div>

      {status?.providerQueryMetrics && status.providerQueryMetrics.length > 0 && (
        <div style={{ marginTop: "18px" }}>
          <h3 style={{ fontSize: "12px", fontWeight: 600, marginBottom: "8px", textTransform: "uppercase", color: "var(--text-muted)" }}>Provider / Query Metrics</h3>
          <div className="table-container">
            <table className="table">
              <thead><tr><th>Provider / Query</th><th>Scanned</th><th>Safe</th><th>Safe %</th></tr></thead>
              <tbody>
                {status.providerQueryMetrics.map((metric) => (
                  <tr key={`${metric.provider}:${metric.query}`}>
                    <td><strong style={{ textTransform: "capitalize" }}>{metric.provider}</strong> / {metric.query}</td>
                    <td>{metric.scanned}</td>
                    <td>{metric.safe}</td>
                    <td>{metric.scanned > 0 ? `${Math.round((metric.safe / metric.scanned) * 100)}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
