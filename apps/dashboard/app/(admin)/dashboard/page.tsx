import { BackgroundOperationsCard } from "@/components/BackgroundOperationsCard";
import {
  CALM_REELS_SHORTLIST_SIZE,
  CALM_REELS_V1_MANUAL_PUBLISH_READY,
  CALM_REELS_V1_MANUAL_PUBLISH_BLOCK_REASON,
} from "@/lib/calm-reels-shortlist";

import { RecentApprovedReels } from "@/components/RecentApprovedReels";
import { getStoredConfigState, listRecentRunRecords } from "@/lib/b2";
import { getPublishPlatformBlockReason, getSystemConnectionStatusWithPublishing, isPublishNowAllowed } from "@/lib/system";
import { GenerationStatusBadge } from "@/components/StatusBadges";
import { TriggerControls } from "@/components/TriggerControls";
import { BackgroundPoolBuilder } from "@/components/BackgroundPoolBuilder";
import { isLocalBuilderEnvironmentEnabled } from "@/lib/local-background-pool-builder";
import { getBackgroundDiscoveryStateFromSupabase, getRecentBackgroundReuseFromSupabase, getBackgroundCandidateSummaryFromSupabase, getBackgroundPoolOperationsSummaryFromSupabase, getEditorialScreeningFromSupabase, getCalmReelsFreshCycleFromSupabase, listRecentApprovedReelsFromSupabase } from "@/lib/supabase-state";
import { getProductionPublishingSchedule, getUpcomingPublishingTimes } from "@/lib/schedule";
import {
  Clock,
  Activity,
  ShieldCheck,
  ShieldAlert,
  Calendar,
  Facebook,
  Instagram,
} from "lucide-react";

export const dynamic = "force-dynamic";

function getTodayBerlinDate(): string {
  try {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Berlin",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    return formatter.format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

export default async function DashboardPage() {
  // These reads have no data dependencies; await only the slowest read.
  const [configState, runs, editorialScreening, freshCycle, sysStatus, candidateSummary, poolSummary, discoveryState, reuseSummary] = await Promise.all([
    getStoredConfigState(),
    listRecentRunRecords(10),
    getEditorialScreeningFromSupabase(),
    getCalmReelsFreshCycleFromSupabase(),
    getSystemConnectionStatusWithPublishing(),
    getBackgroundCandidateSummaryFromSupabase(),
    getBackgroundPoolOperationsSummaryFromSupabase(),
    getBackgroundDiscoveryStateFromSupabase(),
    getRecentBackgroundReuseFromSupabase(),
  ]);
  const approvedReels = editorialScreening.status === "ready"
    ? await listRecentApprovedReelsFromSupabase(5, new Set(editorialScreening.productionAuthorizedRanges ?? []))
    : { status: "error" as const, records: [] as [], error: "Current production authorization could not be verified." };
  const config = configState.config;
  const todayDate = getTodayBerlinDate();

  const schedule = getProductionPublishingSchedule(config.publishing_schedule);
  const upcoming = getUpcomingPublishingTimes(schedule, new Date(), 4);
  const rightsAllowed = isPublishNowAllowed(config.reciter_reuse_confirmed, sysStatus.reciterReuseRuntimeGate);
  const platformBlockReason = getPublishPlatformBlockReason(
    config.facebook_enabled,
    config.instagram_enabled,
    sysStatus.bufferFacebook,
    sysStatus.bufferInstagram,
  );
  const bothChannelsEnabled = config.facebook_enabled && config.instagram_enabled;
  const publishBlockedReason = !CALM_REELS_V1_MANUAL_PUBLISH_READY
    ? CALM_REELS_V1_MANUAL_PUBLISH_BLOCK_REASON
    : !bothChannelsEnabled
    ? "Enable both Facebook and Instagram channels for owner-v1 publishing."
    : !rightsAllowed
    ? "StoredConfig and RECITER_REUSE_CONFIRMED must both be true."
    : platformBlockReason || undefined;

  return (
    <div className="operations-page" style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
      {/* Page Header */}
      <div className="operations-page__header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <h1 style={{ fontSize: "20px", fontWeight: 600 }}>Operations Overview</h1>
          <p style={{ color: "var(--text-muted)", fontSize: "13px", marginTop: "2px" }}>
            Operational monitoring for Quran Reels generation and publishing.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "6px", color: "var(--text-muted)", fontSize: "12px" }}>
          <Calendar size={14} />
          <span>Berlin Date: <strong>{todayDate}</strong></span>
        </div>
      </div>

      <section className="card" aria-label="Calm Quran Reels editorial inventory">
        <strong>Calm Quran Reels v1 · {CALM_REELS_SHORTLIST_SIZE.toLocaleString("en-US")} ayahs</strong>
        {freshCycle.status === "ready" ? <>
          <p style={{ fontSize: "24px", fontWeight: 700 }}>{freshCycle.progress.confirmedAyahs.toLocaleString("en-US")} / {freshCycle.progress.totalAyahs.toLocaleString("en-US")} confirmed published</p>
          <p>Cycle {freshCycle.progress.cycleNumber} · {((freshCycle.progress.confirmedAyahs / freshCycle.progress.totalAyahs) * 100).toFixed(2)}% complete</p>
          <progress value={freshCycle.progress.confirmedAyahs} max={freshCycle.progress.totalAyahs} aria-label="Confirmed published ayahs in current cycle" style={{ width: "100%" }} />
          <p>Next ayah: {freshCycle.progress.nextAyahKey ?? "All confirmed — awaiting verified next-cycle rollover"}</p>
          <p className="text-muted">Fresh publication cycle; earlier posts are not counted. Previously safety-checked videos remain preserved.</p>
        </> : <p role="alert">Fresh publication count unavailable. No old counts will be substituted.</p>}
      </section>

      {/* Operational Highlights Cards */}
      <div
        className="operations-page__highlights"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "16px",
        }}
      >
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "8px" }}>
            <span style={{ color: "var(--text-muted)", fontSize: "12px", fontWeight: 500 }}>
              AUTOMATION STATUS
            </span>
            <Activity size={16} color="var(--primary)" />
          </div>
          <div style={{ fontSize: "18px", fontWeight: 600 }}>
            {config.automation_enabled ? (
              <span style={{ color: sysStatus.schedulerHealth?.status === "ACTIVE" ? "var(--status-success-text)" : "var(--status-failed-text)" }}>
                {sysStatus.schedulerHealth?.status === "ACTIVE" ? "Active" : sysStatus.schedulerHealth?.status === "DEGRADED" ? "Degraded" : "Enabled"}
              </span>
            ) : (
              <span style={{ color: "var(--text-muted)" }}>Disabled</span>
            )}
          </div>
          <p style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "4px" }}>
            {config.automation_enabled
              ? (sysStatus.schedulerHealth?.details || `Scheduler: ${sysStatus.schedulerHealth?.status || "Active"}`)
              : "Scheduled generation paused"}
          </p>
        </div>

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "8px" }}>
            <span style={{ color: "var(--text-muted)", fontSize: "12px", fontWeight: 500 }}>
              FACEBOOK (PRIMARY)
            </span>
            <Facebook size={16} color="var(--primary)" />
          </div>
          <div style={{ fontSize: "18px", fontWeight: 600 }}>
            {config.facebook_enabled ? (
              sysStatus.bufferFacebook ? (
                <span style={{ color: "var(--text-secondary)" }}>Configuration present</span>
              ) : (
                <span style={{ color: "var(--status-failed-text)" }}>Missing Credentials</span>
              )
            ) : (
              <span style={{ color: "var(--text-muted)" }}>Disabled</span>
            )}
          </div>
          <p style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "4px" }}>
            {config.facebook_enabled ? "Configured target; delivery evidence is shown below" : "Ignored in production"}
          </p>
        </div>

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "8px" }}>
            <span style={{ color: "var(--text-muted)", fontSize: "12px", fontWeight: 500 }}>
              INSTAGRAM (OPTIONAL)
            </span>
            <Instagram size={16} color="var(--primary)" />
          </div>
          <div style={{ fontSize: "18px", fontWeight: 600 }}>
            {config.instagram_enabled ? (
              sysStatus.bufferInstagram ? (
                <span style={{ color: "var(--text-secondary)" }}>Configuration present</span>
              ) : (
                <span style={{ color: "var(--status-failed-text)" }}>Missing Channel</span>
              )
            ) : (
              <span style={{ color: "var(--text-muted)" }}>Disabled</span>
            )}
          </div>
          <p style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "4px" }}>
            {config.instagram_enabled ? "Configured target; recent delivery may still be failing" : "Ignored in production"}
          </p>
        </div>

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "8px" }}>
            <span style={{ color: "var(--text-muted)", fontSize: "12px", fontWeight: 500 }}>
              RECITER REUSE RIGHTS
            </span>
            {config.reciter_reuse_confirmed && sysStatus.reciterReuseRuntimeGate ? (
              <ShieldCheck size={16} color="var(--status-success-text)" />
            ) : (
              <ShieldAlert size={16} color="var(--status-blocked-text)" />
            )}
          </div>
          <div style={{ fontSize: "18px", fontWeight: 600 }}>
            {config.reciter_reuse_confirmed && sysStatus.reciterReuseRuntimeGate ? (
              <span style={{ color: "var(--status-success-text)" }}>Confirmed</span>
            ) : (
              <span style={{ color: "var(--status-blocked-text)" }}>Blocked (Fail-Closed)</span>
            )}
          </div>
          <p style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "4px" }}>
            {config.reciter_reuse_confirmed && sysStatus.reciterReuseRuntimeGate
              ? "Public publishing authorized"
              : "Social publishing gated"}
          </p>
        </div>
      </div>



      {/* Dynamic daily schedule */}
      <div>
        <h2 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "12px" }}>
          Daily Publishing Schedule
        </h2>
        <div className="card-elevated" style={{ marginBottom: "12px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", fontWeight: 600 }}><Clock size={16} />{config.automation_enabled ? "Automation enabled" : "Automation paused"}</div>
          {upcoming.length ? upcoming.map((item, index) => <div key={`${item.date}-${item.slotId}`} style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "5px" }}>{index === 0 ? "Next publishing" : "Upcoming"}: {item.dayLabel} {item.time}</div>) : <div style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "5px" }}>No enabled times configured.</div>}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "12px" }}>
          {schedule.map((slot) => {
            const run = runs.find((item) => item.date === todayDate && (item.schedule_slot_id === slot.id || item.slot === slot.id));
            return <div className="card" key={slot.id}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: "8px" }}><strong>{slot.time}</strong><span className={`badge ${slot.enabled ? "badge-success" : "badge-neutral"}`}>{slot.enabled ? "Enabled" : "Disabled"}</span></div>
              <div style={{ color: "var(--text-muted)", fontSize: "11px", marginTop: "4px" }}>{slot.id}</div>
              <div style={{ marginTop: "10px" }}>{run ? <GenerationStatusBadge status={run.status} /> : <span style={{ color: "var(--text-muted)", fontSize: "12px" }}>No run today</span>}</div>
            </div>;
          })}
        </div>
      </div>

      {/* Manual Trigger Section */}
      <TriggerControls
        publishAllowed={CALM_REELS_V1_MANUAL_PUBLISH_READY && bothChannelsEnabled && rightsAllowed && !platformBlockReason}
        publishBlockedReason={publishBlockedReason}
        facebookConnected={sysStatus.bufferFacebook}
        instagramConnected={sysStatus.bufferInstagram}
      />

      <BackgroundOperationsCard candidateSummary={candidateSummary} poolSummary={poolSummary} discoveryState={discoveryState} reuseSummary={reuseSummary} />

      {isLocalBuilderEnvironmentEnabled() && (
        <BackgroundPoolBuilder configAuthoritative={configState.authoritative} />
      )}

      <RecentApprovedReels load={approvedReels} />
    </div>
  );
}
