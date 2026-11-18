import React from "react";
import Link from "next/link";
import { getEditorialAyahsFromSupabase, getEditorialScreeningFromSupabase, getEditorialSurahProgressFromSupabase, getCalmReelsFreshCycleFromSupabase } from "@/lib/supabase-state";
import { EDITORIAL_FILTERS, editorialPagination, type EditorialFilter } from "@/lib/editorial-review";
import { isCanonicalAyahKey } from "@/lib/canonical-quran";
import { CALM_REELS_SHORTLIST_SIZE, CALM_REELS_SHORTLIST_VERSION, summarizeCalmReelsLiveAuthorizationOverlap } from "@/lib/calm-reels-shortlist";

export const dynamic = "force-dynamic";

export default async function EditorialPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const query = await searchParams;
  const value = (key: string) => typeof query[key] === "string" ? query[key] as string : undefined;
  const surah = Number(value("surah"));
  const selectedSurah = isCanonicalAyahKey(`${surah}:1`) ? surah : null;
  const filter: EditorialFilter = EDITORIAL_FILTERS.find((status) => status === value("status")) ?? "ALL";
  const page = editorialPagination(value("page"), 12);
  const overviewPage = editorialPagination(value("overviewPage"), 6);
  const progress = ["remaining", "complete"].includes(value("progress") ?? "") ? value("progress") : "all";
  const [summary, screening, freshCycle] = await Promise.all([getEditorialSurahProgressFromSupabase(), getEditorialScreeningFromSupabase(), getCalmReelsFreshCycleFromSupabase()]);
  const reconciled = screening.status === "ready" && !summary.error
    && summary.rows.reduce((sum, row) => sum + row.screened_ayahs, 0) === screening.summary.screened_ayahs
    && summary.rows.reduce((sum, row) => sum + row.eligible_candidates, 0) === screening.summary.eligible_candidates
    && summary.rows.reduce((sum, row) => sum + row.production_approved_ayahs, 0) === screening.summary.production_approved_ayahs;
  const filtered = summary.rows.filter((row) => progress === "remaining" ? row.remaining_ayahs > 0
    : progress === "complete" ? row.remaining_ayahs === 0 : true);
  const overviewRows = filtered.slice((overviewPage - 1) * 20, overviewPage * 20);
  const scopeOverlap = reconciled && screening.status === "ready"
    ? summarizeCalmReelsLiveAuthorizationOverlap(
      screening.productionAuthorizedCandidateKeys,
      screening.productionAuthorizedRanges ?? [],
    ) : null;
  const details = selectedSurah && reconciled && screening.status === "ready"
    ? await getEditorialAyahsFromSupabase(selectedSurah, filter, page, new Set(screening.productionAuthorizedCandidateKeys)) : null;
  const ayahUrl = (next: number) => `/editorial?surah=${selectedSurah}&status=${filter}&page=${next}#ayahs`;

  return <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
    <header><h1>Quran Editorial Review</h1><p className="text-muted">All 114 surahs · 6,236 canonical ayahs. Final eligible results receive automatic production approval.</p>
      <Link href="/dashboard">← Operations overview</Link></header>
    <section className="card" aria-labelledby="calm-shortlist-heading">
      <h2 id="calm-shortlist-heading">Calm Quran Reels · Frozen editorial shortlist</h2>
      <p><strong>{CALM_REELS_SHORTLIST_SIZE.toLocaleString("en-US")} ayahs</strong> · {CALM_REELS_SHORTLIST_VERSION}</p>
      <p className="text-muted">Curated topic scope from Quran/QSAC: no unwanted-keyword/QSAC-caution signals. This is not a production authorization or proof of complete-context suitability.</p>
      <p>{scopeOverlap
        ? `${scopeOverlap.shortlistAyahsAlreadyAuthorized} ayahs match existing Supabase authorization; ${scopeOverlap.fullyInScopeAuthorizedPassages} exact authorized complete passages contain ${scopeOverlap.ayahsInsideFullAuthorizedPassages} distinct in-scope ayahs.`
        : "Current live approval overlap cannot be verified."}</p>
      {freshCycle.status === "ready" ? <>
        <p>{`Cycle ${freshCycle.progress.cycleNumber}: ${freshCycle.progress.confirmedAyahs} / ${freshCycle.progress.totalAyahs} confirmed published · ${((freshCycle.progress.confirmedAyahs / freshCycle.progress.totalAyahs) * 100).toFixed(2)}% complete · Next ayah ${freshCycle.progress.nextAyahKey ?? "awaiting verified rollover"}`}</p>
        <progress value={freshCycle.progress.confirmedAyahs} max={freshCycle.progress.totalAyahs} aria-label="Confirmed published ayahs in current cycle" style={{ width: "100%" }} />
      </> : <p role="alert">Current publication counter unavailable (not replaced by old historical counts).</p>}
      <p className="text-muted">Fresh start: earlier published posts are excluded from this cycle. Existing safety-checked video media is retained.</p>
    </section>
    {!reconciled ? <p role="alert">{summary.error ?? "Surah totals could not be reconciled with the current screening ledger."}</p> : <>
      <section className="card" aria-labelledby="surah-progress">
        <h2 id="surah-progress">Surah-by-surah progress</h2>
        <form method="get" style={{ display: "flex", gap: 12, alignItems: "center", padding: "16px 0" }}>
          <label htmlFor="progress">Coverage</label><select id="progress" name="progress" defaultValue={progress}>
            <option value="all">All surahs</option><option value="remaining">Screening remaining</option><option value="complete">Screening complete</option>
          </select><button type="submit" className="btn btn-secondary">Filter</button>
        </form>
        <div className="table-container"><table className="table"><thead><tr><th>Surah</th><th>Total ayahs</th><th>Screened</th><th>Remaining</th><th>Eligible candidates</th><th>Production approved</th></tr></thead>
          <tbody>{overviewRows.map((row) => <tr key={row.surah_number}><td><Link href={`/editorial?surah=${row.surah_number}#ayahs`}>Surah {row.surah_number}</Link></td><td>{row.total_ayahs}</td><td>{row.screened_ayahs}</td><td>{row.remaining_ayahs}</td><td>{row.eligible_candidates}</td><td>{row.production_approved_ayahs}</td></tr>)}</tbody></table></div>
        {!overviewRows.length && <p>No surahs match this filter.</p>}
        <nav aria-label="Surah overview pages" style={{ display: "flex", gap: 20, padding: "16px 0" }}>
          {overviewPage > 1 && <Link href={`/editorial?progress=${progress}&overviewPage=${overviewPage - 1}`}>Previous surahs</Link>}
          <span>Page {overviewPage} · {filtered.length} matching surahs</span>
          {overviewPage * 20 < filtered.length && <Link href={`/editorial?progress=${progress}&overviewPage=${overviewPage + 1}`}>Next surahs</Link>}
        </nav><p>Publishing progress counts only posts confirmed in the fresh Calm Reels v1 cycle.</p>
      </section>
      {selectedSurah && <section className="card" id="ayahs" aria-labelledby="ayah-title">
        <h2 id="ayah-title">Surah {selectedSurah}: ayah evidence</h2>
        <form method="get" style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", padding: "16px 0" }}>
          <input type="hidden" name="surah" value={selectedSurah} /><label htmlFor="status">Editorial status</label>
          <select id="status" name="status" defaultValue={filter}>{EDITORIAL_FILTERS.map((status) => <option key={status} value={status}>{status.replaceAll("_", " ")}</option>)}</select>
          <button type="submit" className="btn btn-secondary">Filter ayahs</button>
        </form>
        {details?.error && <p role="alert">{details.error}</p>}
        {details && !details.error && !details.rows.length && <p>No ayahs match this page and filter.</p>}
        {details?.rows.map((row) => <article key={row.ayah_number} style={{ borderTop: "1px solid var(--border-subtle)", padding: "20px 0" }}>
          <h3>{selectedSurah}:{row.ayah_number} · {row.screening_status.replaceAll("_", " ")}</h3>
          <p>{row.semantic_assessment?.replaceAll("_", " ") ?? "Not screened"} · Production approval: {row.productionApproved ? "Approved" : "Excluded"} · Current publication cycle: see the fresh v1 counter above</p>
          {row.screening_status === "SCREENED" && (row.evidenceIssues?.length
            ? <p role="alert">Two-pass source/evidence audit needs attention: {row.evidenceIssues.join(", ").replaceAll("_", " ")}. This status is not proof of valid independent reviews.</p>
            : row.evidenceIssues
              ? <p className="text-muted">Two-pass evidence is structurally consistent. Authentic source and independent reviewer execution remain separately unverified.</p>
              : <p role="alert">Two-pass evidence audit unavailable; no independent reviewer verification is established.</p>)}
          {row.productionApproved && row.screening_status === "SCREENED" && row.semantic_assessment !== "ELIGIBLE_CANDIDATE"
            && <p role="alert">Preserved legacy approval conflicts with current screening; review required.</p>}
          {row.source ? <><p lang="ar" dir="rtl" style={{ fontSize: 26, lineHeight: 2 }}>{row.source.arabic_text}</p><p>{row.source.translation_text}</p><p className="text-muted">QuranEnc {row.source.translation_language} · version {row.source.translation_version}</p></>
            : <p>Authenticated screening source evidence is not yet available for this ayah.</p>}
          {row.assessments.map((assessment) => <details key={assessment.pass_number} style={{ marginTop: 12 }}>
            <summary>Independent pass {assessment.pass_number}: {String(assessment.semantic_assessment).replaceAll("_", " ")}</summary>
            <p>{assessment.assessment_rationale}</p><p>{assessment.context_notes}</p>
            <p className="text-muted">Evaluator: {assessment.evaluator_version} · Run: {assessment.run_id}</p>
            {assessment.evidence && <p>Required coherent passage: {assessment.evidence.passage_first_ayah}–{assessment.evidence.passage_last_ayah}. Standalone sufficient: {assessment.evidence.standalone_sufficient ? "Yes" : "No"}.</p>}
          </details>)}
        </article>)}
        <nav aria-label="Ayah pages" style={{ display: "flex", gap: 20, paddingTop: 16 }}>{page > 1 && <Link href={ayahUrl(page - 1)}>Previous ayahs</Link>}<span>Page {page} · up to 25 ayahs</span>{details?.hasNext && <Link href={ayahUrl(page + 1)}>Next ayahs</Link>}</nav>
      </section>}
    </>}
  </div>;
}
