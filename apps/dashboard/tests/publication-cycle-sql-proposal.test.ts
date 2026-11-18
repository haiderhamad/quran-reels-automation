import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * STATIC REGRESSION CHECKS ONLY. These protect intended SQL proposal content;
 * they cannot certify SQL parsing, live PostgreSQL behavior, or migrations.
 */
const file = resolve(process.cwd(), "../../docs/sql-proposals/publication_cycle_ledger_v1.sql");
const sql = readFileSync(file, "utf-8");
const executableSql = sql.split("\n").filter((line) => !line.trimStart().startsWith("--")).join("\n");
const tables = [
  "publication_catalog_versions",
  "publication_catalog_ayahs",
  "publication_catalog_passages",
  "publication_catalog_passage_ayahs",
  "publication_cycles",
  "publication_cycle_attempts",
  "publication_cycle_ayah_claims",
  "publication_cycle_deliveries",
];

describe("unapplied publishing-cycle SQL proposal security boundary", () => {
  it("remains explicitly outside the auto-applied migrations directory", () => {
    expect(file).toContain("/docs/sql-proposals/");
    expect(sql).toContain("DO NOT APPLY to production");
    expect(sql).toContain("this proposal has NOT been executed anywhere");
    expect(sql).not.toMatch(/(?:^|\n)\s*drop\s+(?:table|schema|function)\b/i);
  });

  it("isolates cycle coverage from generated/uploaded video history", () => {
    expect(sql).toContain("coverage_state / coverage_cycles track generation/upload");
    expect(sql).toContain("create table if not exists public.publication_cycles");
    expect(sql).toContain("create table if not exists public.publication_cycle_deliveries");
    expect(sql).not.toMatch(/(?:update|delete\s+from|truncate|alter\s+table)\s+public\.(?:coverage_state|coverage_cycles)\b/i);
  });

  it("enables RLS and removes browser-role table access on all new tables", () => {
    for (const table of tables) {
      expect(sql).toContain(`create table if not exists public.${table}`);
      expect(sql).toContain(`alter table public.${table} enable row level security;`);
      expect(sql).not.toMatch(new RegExp(`grant\\s+[^;]+?\\s+to\\s+(?:anon|authenticated)\\b`, "i"));
    }
    expect(sql).toMatch(/revoke all on public\.publication_catalog_versions,[\s\S]+?public\.publication_cycle_deliveries\s+from public, anon, authenticated;/);
  });

  it("guards active-cycle singleton, source evidence, source-manifest hash, and immutable snapshots", () => {
    expect(sql).toContain("create unique index if not exists publication_single_active_cycle_idx");
    expect(sql).toContain("where state in ('IN_PROGRESS', 'BLOCKED')");
    expect(sql).toContain("manifest_sha256 text not null unique");
    expect(sql).toContain("arabic_sha256 text not null");
    expect(sql).toContain("translation_sha256 text not null");
    expect(sql).toContain("authorization_fingerprint text not null");
    for (const table of [...tables.slice(1, 4), "publication_cycle_deliveries"]) {
      expect(sql).toContain(`create trigger ${table}_immutable`);
    }
    expect(sql).toContain("create trigger publication_catalog_versions_guard");
    expect(sql).toContain("security invoker");
    expect(executableSql).not.toMatch(/security definer/i);
  });

  it("permits only an audited DRAFT to FROZEN transition and serializes late-member insertions", () => {
    expect(sql).toContain("state text not null default 'DRAFT'");
    expect(sql).toContain("check ((state = 'FROZEN') = (frozen_at is not null))");
    expect(sql).toContain("if OLD.state <> 'DRAFT' then");
    expect(sql).toContain("if NEW.state <> 'FROZEN' or NEW.frozen_at is null");
    expect(sql).toContain("member_ayah_count <> NEW.approved_ayah_count");
    expect(sql).toContain("member_surah_count <> NEW.approved_surah_count");
    expect(sql).toContain("noncontiguous or incorrectly linked frozen passage");
    expect(sql).toContain("approved ayah has no production-authorized passage");
    expect(sql).toContain("for update;");
    for (const table of tables.slice(1, 4)) {
      expect(sql).toContain(`create trigger ${table}_insert_guard`);
    }
    expect(sql).toContain("cannot insert members into a frozen or missing catalog");
    expect(sql).toContain("publication cycles require a FROZEN catalog");
    expect(sql).toContain("create trigger publication_cycles_frozen_catalog_guard");
    expect(sql).toContain("grant select, update on public.publication_catalog_versions to service_role");
    expect(sql).toContain("revoke all on function public.guard_publication_catalog_version()");
  });

  it("requires distinct protected ayah locks, source-backed run IDs and trustworthy delivery identity", () => {
    expect(sql).toMatch(/primary key \(cycle_number, canonical_key\)/);
    expect(sql).toContain("publication_passage_reserved_or_delivered_idx");
    expect(sql).toContain("references public.run_records(run_id) on delete restrict");
    expect(sql).toContain("unique (cycle_number, attempt_id, platform)");
    expect(sql).toContain("unique (platform, publisher_backend, external_post_id)");
    expect(sql).toContain("external_evidence_sha256 text not null");
    expect(sql).toContain("UNKNOWN_OUTCOME");
    expect(sql).toContain("FOR UPDATE");
    expect(sql).toContain("trusted backend must independently verify");
  });

  it("uses valid paired PostgreSQL dollar-quoting for every PL/pgSQL trigger body", () => {
    // Previously three function bodies used a lone "$" delimiter, which is
    // syntactically invalid in PostgreSQL despite passing substring tests.
    const definitions = [...executableSql.matchAll(/create or replace function public\.(\w+)\(\)[\s\S]*?\bas (\$\$)\s*\n[\s\S]*?\n\$\$;/g)];
    expect(definitions.map((match) => match[1])).toEqual([
      "guard_publication_catalog_version",
      "guard_publication_catalog_member_insert",
      "guard_publication_cycle_catalog",
      "reject_publication_immutable_mutation",
    ]);
    expect([...executableSql.matchAll(/^as \$\$/gm)]).toHaveLength(4);
    expect([...executableSql.matchAll(/^\$\$;$/gm)]).toHaveLength(4);
    expect(executableSql.split("\n")).not.toContain("as $");
    expect(executableSql).not.toMatch(/^\$;$/gm);
  });

  it("contains no posting RPC, public-granted function, or schedule dispatch side effect", () => {
    expect(sql).not.toMatch(/\b(?:http_post|net\.http_post|workflow_dispatch|cron\.schedule|supabase\.functions\.invoke)\b/i);
    expect(sql).not.toMatch(/\b(?:grant execute|create policy)\b/i);
    expect(sql).toContain("No SECURITY DEFINER helper, no browser-exposed RPC, no automatic rollover");
  });
});
