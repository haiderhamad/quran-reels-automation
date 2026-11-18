import { describe, expect, it } from "vitest";
import {
  evaluatePublicationCycle,
  fingerprintFrozenPublicationCatalog,
  proposeNextPublicationCycle,
  type ConfirmedPublicationReceipt,
  type FrozenPublicationCatalog,
  type PendingPublicationAttempt,
  type PublicationCycleSnapshot,
} from "../lib/publication-cycle-machine";

const unsignedCatalog: Omit<FrozenPublicationCatalog, "fingerprint"> = {
  version: "calm-inviting-v1-frozen",
  policyVersion: "calm-inviting-v1",
  arabicSourceVersion: "quran-foundation-v4",
  translationSourceVersion: "quranenc-rowwad-en-1.0.19",
  approvedAyahKeys: ["2:21", "2:22", "3:8", "114:6"],
  sourceEvidence: ["2:21", "2:22", "3:8", "114:6"].map((key) => ({
    key, arabicSha256: "b".repeat(64), translationSha256: "c".repeat(64),
  })),
  passages: [
    { id: "s2-21-22", authorizationFingerprint: "d".repeat(32), ayahKeys: ["2:21", "2:22"] },
    { id: "s3-8", authorizationFingerprint: "e".repeat(32), ayahKeys: ["3:8"] },
    { id: "s114-6", authorizationFingerprint: "f".repeat(32), ayahKeys: ["114:6"] },
  ],
};
const catalog: FrozenPublicationCatalog = {
  ...unsignedCatalog,
  fingerprint: fingerprintFrozenPublicationCatalog(unsignedCatalog),
};
const snapshot: PublicationCycleSnapshot = { cycleNumber: 1, catalog };
const receipt = (
  passageId: string,
  runId: string,
  overrides: Partial<ConfirmedPublicationReceipt> = {},
): ConfirmedPublicationReceipt => ({
  cycleNumber: 1,
  catalogFingerprint: catalog.fingerprint,
  platform: "facebook",
  runId,
  passageId,
  externalPostId: "fb-" + runId,
  confirmedAt: "2026-10-25T15:45:00Z",
  ayahKeys: catalog.passages.find((passage) => passage.id === passageId)!.ayahKeys,
  ...overrides,
});
const inFlight = (
  passageId: string,
  runId: string,
  overrides: Partial<PendingPublicationAttempt> = {},
): PendingPublicationAttempt => ({
  cycleNumber: 1,
  catalogFingerprint: catalog.fingerprint,
  runId, passageId, state: "IN_FLIGHT",
  ...overrides,
});

describe("nonpublishing cycle state machine", () => {
  it("starts at zero and distinguishes authorized ayahs from 114 canonical surahs", () => {
    expect(evaluatePublicationCycle(snapshot, [], [])).toEqual({
      cycleNumber: 1,
      catalogVersion: "calm-inviting-v1-frozen",
      state: "IN_PROGRESS",
      approvedSurahs: 3,
      startedSurahs: 0,
      completedSurahs: 0,
      completedSurahPercentage: 0,
      approvedAyahs: 4,
      confirmedFacebookAyahs: 0,
      confirmedFacebookAyahPercentage: 0,
      remainingAyahs: 4,
      pendingAttempts: 0,
      conflictingDeliveries: 0,
      nextEligiblePassageId: "s2-21-22",
    });
  });

  it("requires every approved ayah in a surah before counting it complete", () => {
    const record = receipt("s2-21-22", "r1");
    const result = evaluatePublicationCycle(snapshot, [record], []);
    expect(result.approvedSurahs).toBe(3);
    expect(result.completedSurahs).toBe(1);
    expect(result.startedSurahs).toBe(1);
    expect(result.completedSurahPercentage).toBe(33.3);
    expect(result.confirmedFacebookAyahs).toBe(2);
    expect(result.confirmedFacebookAyahPercentage).toBe(50);
    expect(result.nextEligiblePassageId).toBe("s3-8");
  });

  it("makes a byte-identical confirmed receipt replay idempotent", () => {
    const record = receipt("s3-8", "r2");
    const result = evaluatePublicationCycle(snapshot, [record, { ...record }], []);
    expect(result.confirmedFacebookAyahs).toBe(1);
    expect(result.conflictingDeliveries).toBe(0);
    expect(result.remainingAyahs).toBe(3);
  });

  it("does not count Instagram as Facebook completion", () => {
    const instagram = receipt("s2-21-22", "r3", {
      platform: "instagram", externalPostId: "ig-3",
    });
    const result = evaluatePublicationCycle(snapshot, [instagram], []);
    expect(result.confirmedFacebookAyahs).toBe(0);
    expect(result.completedSurahs).toBe(0);
  });

  it("prevents reselecting a reserved passage", () => {
    const result = evaluatePublicationCycle(snapshot, [], [
      inFlight("s2-21-22", "active"),
    ]);
    expect(result.nextEligiblePassageId).toBe("s3-8");
    expect(result.pendingAttempts).toBe(1);
  });

  it("cannot roll over on generated, unknown, or incomplete evidence", () => {
    expect(() => proposeNextPublicationCycle(snapshot, [], [])).toThrow("cannot roll over");
    const firstTwo = [
      receipt("s2-21-22", "r1"),
      receipt("s3-8", "r2"),
    ];
    expect(() => proposeNextPublicationCycle(snapshot, firstTwo, [])).toThrow("cannot roll over");
    const all = [...firstTwo, receipt("s114-6", "r3")];
    expect(() => proposeNextPublicationCycle(snapshot, all, [
      inFlight("s114-6", "unknown", { state: "UNKNOWN_OUTCOME" }),
    ])).toThrow("cannot roll over");
    expect(proposeNextPublicationCycle(snapshot, all, [])).toEqual({
      cycleNumber: 2, catalog,
    });
  });

  it("does not pretend an empty/unfrozen catalog is complete", () => {
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...catalog, approvedAyahKeys: [], passages: [] },
    }, [], [])).toThrow("empty or unfinished");
  });

  it("rejects other-cycle evidence and unknown or unapproved passage keys", () => {
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1", { cycleNumber: 2 }),
    ], [])).toThrow("different cycle/catalog");
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1", { catalogFingerprint: "b".repeat(64) }),
    ], [])).toThrow("different cycle/catalog");
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1", { ayahKeys: ["3:7"] }),
    ], [])).toThrow("do not match");
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1", { passageId: "unknown" }),
    ], [])).toThrow("unapproved passage");
  });

  it("rejects forged or malformed proof metadata", () => {
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1", { confirmedAt: "yesterday" }),
    ], [])).toThrow("UTC");
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1", { externalPostId: "" }),
    ], [])).toThrow("external post id");
  });

  it("blocks overlapping duplicate real deliveries rather than silently counting them as success", () => {
    const d = evaluatePublicationCycle(snapshot, [
      receipt("s2-21-22", "a"), receipt("s2-21-22", "b"),
      receipt("s3-8", "c"), receipt("s114-6", "d"),
    ], []);
    expect(d.state).toBe("BLOCKED");
    expect(d.conflictingDeliveries).toBe(1);
    expect(() => proposeNextPublicationCycle(snapshot, [
      receipt("s2-21-22", "a"), receipt("s2-21-22", "b"),
      receipt("s3-8", "c"), receipt("s114-6", "d"),
    ], [])).toThrow("cannot roll over");
  });

  it("rejects contradictory retry receipts or reused post identity", () => {
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1"), receipt("s3-8", "r1", { externalPostId: "different" }),
    ], [])).toThrow("Conflicting verified run/post");
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1"), receipt("s3-8", "r2", { externalPostId: "fb-r1" }),
    ], [])).toThrow("Conflicting verified run/post");
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "r1"), receipt("s3-8", "r1", { confirmedAt: "2026-10-25T16:00:00Z" }),
    ], [])).toThrow("Contradictory replay");
  });

  it("rejects catalog mutation, orphan approved identities, and noncontiguous ranges", () => {
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...catalog, fingerprint: "wrong" },
    }, [], [])).toThrow("fingerprint");
    const orphanUnsigned = {
      ...unsignedCatalog,
      approvedAyahKeys: [...unsignedCatalog.approvedAyahKeys, "4:1"],
      sourceEvidence: [
        ...unsignedCatalog.sourceEvidence,
        { key: "4:1", arabicSha256: "b".repeat(64), translationSha256: "c".repeat(64) },
      ],
    };
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...orphanUnsigned, fingerprint: fingerprintFrozenPublicationCatalog(orphanUnsigned) },
    }, [], [])).toThrow("lack an exact authorized passage");
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...catalog, passages: [
        { ...catalog.passages[0], ayahKeys: ["2:22", "2:21"] }, ...catalog.passages.slice(1),
      ] },
    }, [], [])).toThrow("single contiguous");
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...catalog, approvedAyahKeys: [...catalog.approvedAyahKeys, "2:21"] },
    }, [], [])).toThrow("Duplicate approved");
  });

  it("blocks UNKNOWN_OUTCOME until verified instead of treating it as failure", () => {
    const result = evaluatePublicationCycle(snapshot, [], [
      inFlight("s2-21-22", "maybe-posted", { state: "UNKNOWN_OUTCOME" }),
    ]);
    expect(result.state).toBe("BLOCKED");
    expect(result.confirmedFacebookAyahs).toBe(0);
    expect(result.nextEligiblePassageId).toBeNull();
  });

  it("rejects overlapping reservations on different authorized passages", () => {
    const overlappingUnsigned = { ...unsignedCatalog, passages: [
      ...unsignedCatalog.passages,
      { id: "s2-22", authorizationFingerprint: "a".repeat(32), ayahKeys: ["2:22"] },
    ] };
    const overlapping = {
      ...overlappingUnsigned,
      fingerprint: fingerprintFrozenPublicationCatalog(overlappingUnsigned),
    };
    expect(() => evaluatePublicationCycle({ cycleNumber: 1, catalog: overlapping }, [], [
      inFlight("s2-21-22", "first-run", { catalogFingerprint: overlapping.fingerprint }),
      inFlight("s2-22", "second-run", { catalogFingerprint: overlapping.fingerprint }),
    ])).toThrow("Overlapping active passage reservations");
  });

  it("rejects contradictory same-run passage claims and pending reservations", () => {
    expect(() => evaluatePublicationCycle(snapshot, [
      receipt("s3-8", "one-run"),
      receipt("s114-6", "one-run", { externalPostId: "fb-one-run" }),
    ], [])).toThrow("Contradictory replay of verified receipt");
    expect(() => evaluatePublicationCycle(snapshot, [], [
      inFlight("s3-8", "one-run"), inFlight("s114-6", "one-run"),
    ])).toThrow("Pending run has conflicting reserved passage");
  });

  it("proposes repeat cycles 1 → 2 → 3 without rescreening or recycling old proofs", () => {
    const firstCycleProofs = [
      receipt("s2-21-22", "cycle-1-a"),
      receipt("s3-8", "cycle-1-b"),
      receipt("s114-6", "cycle-1-c"),
    ];
    const secondCycle = proposeNextPublicationCycle(snapshot, firstCycleProofs, []);
    expect(secondCycle.cycleNumber).toBe(2);
    expect(secondCycle.catalog).toBe(catalog);
    expect(evaluatePublicationCycle(secondCycle, [], []).confirmedFacebookAyahs).toBe(0);
    expect(() => evaluatePublicationCycle(secondCycle, firstCycleProofs, [])).toThrow("different cycle/catalog");

    const secondCycleProofs = firstCycleProofs.map((proof, index) => ({
      ...proof,
      cycleNumber: 2,
      runId: `cycle-2-${index}`,
      externalPostId: `fb-cycle-2-${index}`,
      confirmedAt: "2026-10-25T17:00:00Z",
    }));
    const thirdCycle = proposeNextPublicationCycle(secondCycle, secondCycleProofs, []);
    expect(thirdCycle.cycleNumber).toBe(3);
    expect(thirdCycle.catalog).toBe(catalog);
    expect(evaluatePublicationCycle(thirdCycle, [], []).remainingAyahs).toBe(4);
  });

  it("cryptographically detects source text, translation, authorization and order drift", () => {
    const cases: FrozenPublicationCatalog[] = [
      { ...catalog, sourceEvidence: catalog.sourceEvidence.map((entry, index) =>
        index === 0 ? { ...entry, arabicSha256: "a".repeat(64) } : entry) },
      { ...catalog, sourceEvidence: catalog.sourceEvidence.map((entry, index) =>
        index === 0 ? { ...entry, translationSha256: "d".repeat(64) } : entry) },
      { ...catalog, translationSourceVersion: "unreviewed-translation" },
      { ...catalog, policyVersion: "weaker-policy" },
      { ...catalog, passages: catalog.passages.map((passage, index) =>
        index === 0 ? { ...passage, authorizationFingerprint: "a".repeat(32) } : passage) },
      { ...catalog, passages: [...catalog.passages].reverse() },
      { ...catalog, approvedAyahKeys: [...catalog.approvedAyahKeys].reverse() },
      { ...catalog, fingerprint: "a".repeat(64) },
    ];
    for (const modified of cases) {
      expect(() => evaluatePublicationCycle({ cycleNumber: 1, catalog: modified }, [], []))
        .toThrow("fingerprint does not match");
    }
  });

  it("rejects missing or duplicated source evidence rather than approving by count alone", () => {
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...catalog, sourceEvidence: catalog.sourceEvidence.slice(1) },
    }, [], [])).toThrow("source evidence count");
    expect(() => evaluatePublicationCycle({
      cycleNumber: 1,
      catalog: { ...catalog, sourceEvidence: [
        catalog.sourceEvidence[0], catalog.sourceEvidence[0], ...catalog.sourceEvidence.slice(2),
      ] },
    }, [], [])).toThrow("duplicate or malformed");
  });

  it("never mutates input snapshots or receipts", () => {
    const record = receipt("s3-8", "r1");
    const initial = JSON.stringify(snapshot);
    const orig = JSON.stringify(record);
    evaluatePublicationCycle(snapshot, [record], []);
    expect(JSON.stringify(snapshot)).toBe(initial);
    expect(JSON.stringify(record)).toBe(orig);
  });
});
