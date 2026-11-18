import React from "react";
import Link from "next/link";
import { ArrowRight, CheckCircle2, Film } from "lucide-react";
import type { ApprovedReelsLoadResult } from "@/lib/supabase-state";

export function RecentApprovedReels({ load }: { load: ApprovedReelsLoadResult }) {
  return (
    <section className="card recent-approved" aria-labelledby="recent-approved-title">
      <div className="recent-approved__header">
        <div>
          <div className="recent-approved__title"><Film size={18} aria-hidden="true" /><h2 id="recent-approved-title">Recent Approved Reels</h2></div>
          <p>Source-verified production Reels with confirmed historical delivery. Current platform presence is not verified.</p>
        </div>
        <Link href="/runs" className="btn btn-secondary">Technical Run History <ArrowRight size={14} aria-hidden="true" /></Link>
      </div>

      {load.status === "error" ? (
        <p className="recent-approved__empty" role="alert">{load.error}</p>
      ) : load.records.length === 0 ? (
        <p className="recent-approved__empty">No eligible deliveries have sufficient reconciliation evidence. All records remain in Technical Run History.</p>
      ) : (
        <ol className="recent-approved__list">
          {load.records.map((run) => (
            <li className="recent-approved__item" key={run.run_id}>
              <div className="recent-approved__item-main">
                <strong>{run.surah_name_en || `Surah ${run.surah_number}`} · {run.surah_number}:{run.first_ayah}{run.last_ayah !== run.first_ayah ? `–${run.last_ayah}` : ""}</strong>
                <span>{run.date} · {run.run_kind === "scheduled" ? "Scheduled" : "Manual production"}</span>
              </div>
              <div className="recent-approved__platforms" aria-label="Historical delivery evidence">
                {run.facebook_status === "SUCCESS" && (run.facebook_media_id || run.facebook_reel_url) && <span><CheckCircle2 size={14} aria-hidden="true" /> Facebook delivered</span>}
                {run.instagram_status === "SUCCESS" && run.instagram_media_id && <span><CheckCircle2 size={14} aria-hidden="true" /> Instagram delivered</span>}
              </div>
              <Link href={`/runs/${run.date}/${run.slot}?runId=${encodeURIComponent(run.run_id)}`} className="recent-approved__inspect">Inspect <ArrowRight size={13} aria-hidden="true" /></Link>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
