import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, it } from "vitest";

const readJson = (file: string) => JSON.parse(readFileSync(path.resolve(file), "utf8"));

it("starts each independent installation with no previous operator's publication history", () => {
  const snapshot = readJson("lib/editorial-reconciliation.json");
  expect(snapshot.entries.length).toBe(0);
  expect(snapshot.generated_at_utc).toBeNull();
});

it("distributes only canonical keys and decision labels for the legacy progress display", () => {
  const inventory = readJson("../../data/editorial_inventory.json");
  expect(inventory.summary).toEqual({
    APPROVED_REVIEWED: 0, EXCLUDED_CONFIRMED: 0, REVIEW_REQUIRED: 0, NOT_YET_REVIEWED: 6236,
  });
  expect(inventory.records.every((record: { editorial_eligibility_status: string }) =>
    record.editorial_eligibility_status === "NOT_YET_REVIEWED")).toBe(true);
  expect(readJson("../../data/reel_theme_catalog.json").ranges).toEqual([]);
  expect(inventory.records).toHaveLength(6236);
  expect(new Set(inventory.records.map((record: { canonical_key: string }) => record.canonical_key)).size).toBe(6236);
  expect(inventory.records.every((record: object) =>
    Object.keys(record).sort().join(",") === "canonical_key,editorial_eligibility_status")).toBe(true);
});

it("configures the Dashboard to read the same Supabase state as the self-hosted Worker", () => {
  const example = readFileSync(path.resolve(".env.example"), "utf8");
  expect(example).toMatch(/^STATE_AUTHORITY=supabase$/m);
  for (const gate of ["SCHEDULER_ACTIVE", "BUFFER_PUBLISHING_ENABLED", "BUFFER_FACEBOOK_ENABLED", "BUFFER_INSTAGRAM_ENABLED"]) {
    expect(example).toContain(gate + "=false");
  }
});
