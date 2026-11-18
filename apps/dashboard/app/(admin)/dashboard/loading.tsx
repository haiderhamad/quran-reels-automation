import React from "react";

export default function DashboardLoading() {
  return (
    <section
      className="card-elevated editorial-coverage"
      aria-labelledby="editorial-coverage-loading-title"
      aria-busy="true"
    >
      <h2 id="editorial-coverage-loading-title">Quran Reels — Publishing Coverage</h2>
      <p className="editorial-coverage__message" role="status">
        Loading coverage data…
      </p>
      <div className="editorial-coverage__loading-grid" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </div>
    </section>
  );
}
