import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it, expect, vi } from "vitest";
import { BackgroundOperationsCard } from "../components/BackgroundOperationsCard";

const props = {
  poolSummary: { available: true, total: 120, verified: 110, stale: 10, highRisk: 4, selectable: 100, unused: 85, used: 15 },
  candidateSummary: { available: true, total: 40, pending: 30, processing: 1, verified: 9, safetyRejected: 0, metadataIneligible: 0, transientFailure: 0, lastDiscovery: null, lastActivity: null },
  discoveryState: { category: "water", nextCategory: "forest", completedAt: "2026-09-20T12:00:00Z", queryCursor: 2, pageCursor: 3 },
  reuseSummary: { available: true, count: 2, lastAt: "2026-09-20T10:00:00Z" },
};
describe("background operations card rendering", () => {
  it("renders durable category/cursor, counts and bounded reuse evidence without network", () => {
    const fetch = vi.fn(() => { throw new Error("No network in rendered fixture"); });
    vi.stubGlobal("fetch", fetch);
    const html = renderToStaticMarkup(<BackgroundOperationsCard {...props} />);
    expect(html).toContain("water / forest"); expect(html).toContain("2 / 3");
    expect(html).toContain("85 never used + 15 used before = 100 ready");
    expect(html).toContain("Inventory and exclusions");
    expect(html).toContain("Candidate queue");
    expect(html).toContain("these are events, not used asset totals");
    expect(html).toContain("Last 30 production runs"); expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
  it("shows the live-style numbers as subsets rather than additive clip counts", () => {
    const html = renderToStaticMarkup(<BackgroundOperationsCard {...props}
      poolSummary={{ ...props.poolSummary, total: 195, verified: 194, selectable: 191, unused: 75, used: 116, stale: 1 }} />);
    expect(html).toContain("75 never used + 116 used before = 191 ready");
    expect(html).toContain("195</strong> total records");
    expect(html).toContain("194</strong> verified records");
    expect(html).toContain("not extra ready videos");
  });
  it("shows unavailable evidence explicitly instead of reporting zero", () => {
    const html = renderToStaticMarkup(<BackgroundOperationsCard {...props} poolSummary={{ ...props.poolSummary, available: false }} discoveryState={null} reuseSummary={{ available: false, count: 0, lastAt: null }} />);
    expect(html).toContain("Pool counts unavailable"); expect(html).toContain("No durable discovery record"); expect(html).toContain("Unavailable");
  });
});
