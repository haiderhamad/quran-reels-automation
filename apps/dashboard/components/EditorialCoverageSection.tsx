import React from "react";
import type { EditorialCoverageLoadResult, EditorialScreeningLoadResult, PublishingEvidenceLoadResult } from "@/lib/supabase-state";
import type { StoredConfig } from "@/lib/schema";
import { CANONICAL_AYAH_COUNT } from "@/lib/editorial-coverage";
import { computePublishingProgress, type PlatformOperationsSummary, type PublishingPlatform } from "@/lib/publishing-progress";
import { summarizeAuthorizedSurahProgress } from "@/lib/publishing-cycle-progress";
import { getProductionPublishingSchedule } from "@/lib/schedule";
import { BookOpenCheck, Facebook, Instagram, Layers, Share2 } from "lucide-react";

function formatBerlinDate(value: string | null): string {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not available";
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Europe/Berlin",
  }).format(date) + " Europe/Berlin";
}

function healthClass(value: PlatformOperationsSummary["operationalHealth"]): string {
  if (value === "Degraded") return "editorial-coverage__health editorial-coverage__health--failed";
  if (value === "Awaiting confirmation" || value === "Needs attention") return "editorial-coverage__health editorial-coverage__health--warning";
  return "editorial-coverage__health";
}

function PlatformEvidenceCard({
  platform,
  summary,
  verifiedAyahs,
}: {
  platform: PublishingPlatform;
  summary: PlatformOperationsSummary | null;
  verifiedAyahs: number | null;
}) {
  const label = platform === "facebook" ? "Facebook" : "Instagram";
  const Icon = platform === "facebook" ? Facebook : Instagram;

  return (
    <article className="editorial-coverage__platform-card" aria-label={`${label} publishing evidence`}>
      <div className="editorial-coverage__platform-heading">
        <h3><Icon size={16} aria-hidden="true" /> {label}</h3>
        {summary ? <span className={healthClass(summary.operationalHealth)}>{summary.operationalHealth}</span> : <span className="editorial-coverage__health">Not verified</span>}
      </div>
      <dl className="editorial-coverage__status-list">
        <div><dt>Configuration present</dt><dd>{summary ? (summary.configured ? "Yes" : "No") : "Not verified"}</dd></div>
        <div><dt>Buffer channel reachable</dt><dd>{summary?.bufferReachability ?? "Not verified"}</dd></div>
        <div>
          <dt>Last publishing attempt</dt>
          <dd>
            {summary?.latestAttempt
              ? `${summary.latestAttempt.status}${summary.latestAttempt.errorCode ? ` — ${summary.latestAttempt.errorCode}` : ""} (${formatBerlinDate(summary.latestAttempt.occurredAt)})`
              : "Not available"}
          </dd>
        </div>
        <div><dt>Last confirmed historical delivery</dt><dd>{summary ? formatBerlinDate(summary.lastConfirmedHistoricalDeliveryAt) : "Not verified"}</dd></div>
        <div><dt>Verified historical delivery records</dt><dd>{summary ? summary.historicallyVerifiedDeliveryRecords.toLocaleString() : "Not verified"}</dd></div>
        <div><dt>Verified historical ayahs</dt><dd>{verifiedAyahs === null ? "Not verified" : verifiedAyahs.toLocaleString()}</dd></div>
      </dl>
    </article>
  );
}

export function EditorialCoverageSection({
  load,
  screeningLoad,
  publishingLoad,
  schedule,
  configured,
}: {
  load: EditorialCoverageLoadResult;
  screeningLoad: EditorialScreeningLoadResult;
  publishingLoad: PublishingEvidenceLoadResult;
  schedule: StoredConfig["publishing_schedule"];
  configured: Record<PublishingPlatform, boolean>;
}) {
  const screening = screeningLoad.status === "ready" ? screeningLoad.summary : null;
  const preliminaryDrafts = screeningLoad.status === "ready" ? screeningLoad.preliminaryDrafts ?? null : null;
  const historicalCoverage = load.status === "ready" ? load.summary : null;
  const productionAuthorizedCandidateKeys = screeningLoad.status === "ready"
    ? new Set(screeningLoad.productionAuthorizedCandidateKeys)
    : null;
  const publishing = publishingLoad.status === "ready" && productionAuthorizedCandidateKeys
    ? computePublishingProgress(
      publishingLoad.records,
      publishingLoad.claims,
      configured,
      productionAuthorizedCandidateKeys,
      new Set(screeningLoad.status === "ready" ? screeningLoad.productionAuthorizedRanges ?? [] : []),
    )
    : null;
  const historicalDeliverySurahs = publishing && productionAuthorizedCandidateKeys
    ? summarizeAuthorizedSurahProgress(
      productionAuthorizedCandidateKeys,
      publishing.historicallyVerifiedAyahKeys,
    )
    : null;
  const currentGenerationCycle = productionAuthorizedCandidateKeys
    && load.status === "ready" && load.coveredAyahKeys
    ? summarizeAuthorizedSurahProgress(
      productionAuthorizedCandidateKeys,
      load.coveredAyahKeys,
    )
    : null;
  const enabledTimes = getProductionPublishingSchedule(schedule)
    .map((entry) => entry.time)
    .sort();
  const scheduleLabel = enabledTimes.length
    ? `${enabledTimes.join(" / ")} Europe/Berlin`
    : "Not available";

  return (
    <section className="card-elevated editorial-coverage" aria-labelledby="editorial-coverage-title">
      <div className="editorial-coverage__header">
        <div className="editorial-coverage__title">
          <Layers size={18} color="var(--primary)" aria-hidden="true" />
          <h2 id="editorial-coverage-title">Quran Editorial Review and Publishing Evidence</h2>
        </div>
        <span className="editorial-coverage__policy">
          {screening
            ? `Policy ${screening.policy_version} · ${screening.production_approved_passages.toLocaleString()} valid passage ranges`
            : "Live policy state not verified"}
        </span>
      </div>

      <section className="editorial-coverage__subsection" aria-labelledby="editorial-review-title">
        <div className="editorial-coverage__section-heading">
          <BookOpenCheck size={17} color="var(--primary)" aria-hidden="true" />
          <h3 id="editorial-review-title">Quran editorial review</h3>
        </div>
        <p className="editorial-coverage__explanation">
          Two successful independent editorial passes followed by a final eligible classification automatically authorize the validated coherent passage. Ineligible, ambiguous, and unreviewed ayahs remain excluded.
        </p>
        {screeningLoad.status === "unavailable" && (
          <p className="editorial-coverage__message editorial-coverage__message--error" role="status">{screeningLoad.error}</p>
        )}
        <dl className="editorial-coverage__metrics">
          <div><dt>Canonical ayahs</dt><dd>{screening ? screening.canonical_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Screened</dt><dd>{screening ? screening.screened_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Not screened</dt><dd>{screening ? screening.not_screened_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Eligible candidates</dt><dd>{screening ? screening.eligible_candidates.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Ineligible candidates</dt><dd>{screening ? screening.ineligible_candidates.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Ambiguous / review required</dt><dd>{screening ? screening.ambiguous_candidates.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Production-approved candidate identities</dt><dd>{screening ? screening.production_approved_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Automatic from eligible screening</dt><dd>{screening ? screening.automatic_production_approved_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Preserved legacy approvals</dt><dd>{screening ? screening.legacy_production_approved_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Valid coherent passage ranges</dt><dd>{screening ? screening.production_approved_passages.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Preliminary ChatGPT draft reviews</dt><dd>{preliminaryDrafts ? preliminaryDrafts.total.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Drafts awaiting independent second assessment</dt><dd>{preliminaryDrafts ? preliminaryDrafts.awaiting_second_pass.toLocaleString() : "Not verified"}</dd></div>
        </dl>
        <div className="editorial-coverage__progress-row">
          <span>Committed screening coverage across the canonical Quran</span>
          <strong>{screening ? `${screening.screened_ayahs.toLocaleString()} of ${screening.canonical_ayahs.toLocaleString()} ayahs` : "Not verified"}</strong>
        </div>
        <div
          className="editorial-coverage__progress-track"
          role="progressbar"
          aria-label="Canonical ayahs with committed editorial screening"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={screening ? Number(((screening.screened_ayahs / screening.canonical_ayahs) * 100).toFixed(2)) : 0}
          aria-valuetext={screening ? `${screening.screened_ayahs} of ${screening.canonical_ayahs} ayahs screened` : "Screening not verified"}
        >
          <div style={{ width: `${screening ? (screening.screened_ayahs / screening.canonical_ayahs) * 100 : 0}%` }} />
        </div>
        <p className="editorial-coverage__message" role="status">
          {screening
            ? screening.not_screened_ayahs > 0
              ? `One-time full-Quran scan INCOMPLETE: ${screening.not_screened_ayahs.toLocaleString()} canonical ayahs remain unreviewed; final catalog freeze is blocked.`
              : "All canonical ayah statuses have been screened; final catalog freeze still requires independent source/reviewer evidence audit and legacy technical-fallback remediation."
            : "One-time full-Quran scan and catalog freeze readiness: Not verified."}
        </p>
        <p className="editorial-coverage__message">
          Last successful screening: {screening ? formatBerlinDate(screening.last_successful_screening_at) : "Not verified"}. Worker: {screeningLoad.status === "ready" && screeningLoad.worker ? `${screeningLoad.worker.status} (${screeningLoad.worker.committed_ayahs} committed; ${screeningLoad.worker.failed_ayahs} failed${screeningLoad.worker.last_error ? `; latest error: ${screeningLoad.worker.last_error}` : ""})` : "No worker status recorded"}.
        </p>
        <p className="editorial-coverage__review-link">
          <a href="/editorial">Browse all 114 surahs and individual ayah evidence</a>.{" "}
          The v1 channel selection is frozen at 2,211 ayah IDs. Historical screening metrics are diagnostic only; the publisher must use the ordered v1 catalog and verified delivery receipts.
        </p>
      </section>

      <section className="editorial-coverage__subsection" aria-labelledby="publishing-evidence-title">
        <div className="editorial-coverage__section-heading">
          <Share2 size={17} color="var(--primary)" aria-hidden="true" />
          <h3 id="publishing-evidence-title">Actual publishing progress</h3>
        </div>
        <p className="editorial-coverage__explanation">
          Historical delivery records are not proof that a Reel remains live. No connected source currently verifies present-day Facebook or Instagram presence.
        </p>
        {publishingLoad.status === "unavailable" && (
          <p className="editorial-coverage__message editorial-coverage__message--error" role="status">{publishingLoad.error}</p>
        )}
        <div className="card" aria-label="Verified surah delivery and current generation cycle">
          <h4 style={{ fontSize: "14px", fontWeight: 600, marginBottom: "10px" }}>
            Approved-surah delivery progress
          </h4>
          <div className="editorial-coverage__progress-row">
            <span>Surahs with confirmed historical delivery of every approved ayah</span>
            <strong>{historicalDeliverySurahs
              ? `${historicalDeliverySurahs.completedSurahs} of ${historicalDeliverySurahs.eligibleSurahs} surahs · ${historicalDeliverySurahs.percentage.toFixed(1)}%`
              : "Not verified"}</strong>
          </div>
          {historicalDeliverySurahs && (
            <div className="editorial-coverage__progress-track"
              role="progressbar"
              aria-label="Approved surahs with verified historical delivery of every approved ayah"
              aria-valuemin={0} aria-valuemax={100}
              aria-valuenow={historicalDeliverySurahs.percentage}
              aria-valuetext={`${historicalDeliverySurahs.completedSurahs} of ${historicalDeliverySurahs.eligibleSurahs} approved surahs`}>
              <div style={{ width: `${historicalDeliverySurahs.percentage}%` }} />
            </div>
          )}
          <dl className="editorial-coverage__metrics">
            <div><dt>Approved surahs eligible for Reels</dt><dd>{historicalDeliverySurahs?.eligibleSurahs ?? (productionAuthorizedCandidateKeys
              ? summarizeAuthorizedSurahProgress(productionAuthorizedCandidateKeys, []).eligibleSurahs
              : "Not verified")}</dd></div>
            <div><dt>Surahs with some verified historical delivery</dt><dd>{historicalDeliverySurahs?.startedSurahs ?? "Not verified"}</dd></div>
            <div><dt>Approved ayahs with verified historical delivery</dt><dd>{historicalDeliverySurahs
              ? `${historicalDeliverySurahs.completedAyahs} of ${historicalDeliverySurahs.eligibleAyahs}`
              : "Not verified"}</dd></div>
            <div><dt>Current generation cycle</dt><dd>{historicalCoverage?.cycleNumber ?? "Not verified"}</dd></div>
            <div><dt>Surahs fully generated/uploaded in this cycle</dt><dd>{currentGenerationCycle
              ? `${currentGenerationCycle.completedSurahs} of ${currentGenerationCycle.eligibleSurahs} (${currentGenerationCycle.percentage.toFixed(1)}%)`
              : "Not verified"}</dd></div>
            <div><dt>Surahs with some generation in this cycle</dt><dd>{currentGenerationCycle?.startedSurahs ?? "Not verified"}</dd></div>
            <div><dt>Verified posts within this specific cycle</dt><dd>Not verified by cycle</dd></div>
          </dl>
          <p className="editorial-coverage__explanation">
            Delivery counts use identity-bound historical platform evidence, not generated files or
            Buffer acceptance. This is not a live-platform presence count. Generation-cycle counts
            are separate; true per-cycle publishing requires cycle-stamped confirmed delivery
            events before automated posting-cycle rollover can be certified.
          </p>
        </div>
        <dl className="editorial-coverage__metrics">
          <div><dt>Production-approved candidate identities</dt><dd>{screening ? screening.production_approved_ayahs.toLocaleString() : "Not verified"}</dd></div>
          <div><dt>Currently verified published</dt><dd>Not verified</dd></div>
          <div><dt>Remaining unpublished</dt><dd>Not verified</dd></div>
          <div><dt>Current publication completion</dt><dd>Not verified</dd></div>
          <div><dt>Unverified delivery outcomes (platform claims)</dt><dd>{publishing ? publishing.unverifiedDeliveryOutcomes.toLocaleString() : "Not verified"}</dd></div>
        </dl>
        {publishing && (
          <>
            <div className="editorial-coverage__history-note">
              <strong>Historical evidence for approved ayahs:</strong> {publishing.historicallyVerifiedApprovedAyahs} of {publishing.approvedAyahs} distinct ayahs have identity-bound delivery evidence ({publishing.approvedAyahsWithoutVerifiedHistoricalDelivery} have no verified historical delivery record). This is not a current-live count.
            </div>
            <div className="editorial-coverage__platforms">
              <PlatformEvidenceCard platform="facebook" summary={publishing.platforms.facebook} verifiedAyahs={publishing.facebookHistoricallyVerifiedAyahs} />
              <PlatformEvidenceCard platform="instagram" summary={publishing.platforms.instagram} verifiedAyahs={publishing.instagramHistoricallyVerifiedAyahs} />
            </div>
            <details className="editorial-coverage__advanced">
              <summary>Historical cleanup and coverage details</summary>
              <div className="editorial-coverage__details">
                <div>
                  <h3>Publication cleanup reconciliation</h3>
                  <dl className="editorial-coverage__status-list">
                    <div><dt>Uncertain review outcomes</dt><dd>{publishing.reconciliationUncertainReviews.toLocaleString()} platform records</dd></div>
                    <div><dt>Retraction/removal not confirmed</dt><dd>{publishing.reconciliationRetractionsRequired.toLocaleString()} platform records</dd></div>
                    <div><dt>Confirmed retraction or absence</dt><dd>{publishing.confirmedRetractionOutcomes} platform records</dd></div>
                    <div><dt>Historically verified delivery records</dt><dd>{publishing.reconciliationValidHistoricalDeliveries.toLocaleString()} platform records; current presence not verified</dd></div>
                    <div><dt>All unresolved reconciliation records</dt><dd>{publishing.reconciliationUnresolvedOutcomes} platform records</dd></div>
                    <div><dt>Reconciliation snapshot</dt><dd>{formatBerlinDate(publishing.reconciliationGeneratedAt)}</dd></div>
                  </dl>
                </div>
              </div>
            </details>
          </>
        )}
        <details className="editorial-coverage__advanced">
          <summary>Historical coverage_state and schedule details</summary>
          <div className="editorial-coverage__details">
            <div className="editorial-coverage__refresh">
              <h3>Publishing schedule</h3>
              <p>{scheduleLabel}</p>
              <h3>Historical coverage_state</h3>
              {load.status === "error" && <p className="editorial-coverage__message editorial-coverage__message--error">{load.error}</p>}
              {load.status === "missing" && <p>Historical coverage data is not available.</p>}
              {historicalCoverage && <>
                <p>Cycle {historicalCoverage.cycleNumber}: {historicalCoverage.eligibleCoveredAyahs} of {historicalCoverage.eligibleAyahs} current catalog ayahs were historically covered ({historicalCoverage.coveragePercentage.toFixed(1)}%).</p>
                <p>All-corpus historical coverage keys: {historicalCoverage.historicallyCoveredAyahs.toLocaleString()} distinct ayahs.</p>
                <p>Coverage state last updated: {formatBerlinDate(historicalCoverage.sourceUpdatedAt)}</p>
              </>}
              <p>Canonical ayah identities: {(historicalCoverage?.canonicalAyahs ?? CANONICAL_AYAH_COUNT).toLocaleString()}</p>
              <p>Coverage is a generation-history metric, not a publication metric.</p>
            </div>
          </div>
        </details>
      </section>
    </section>
  );
}
