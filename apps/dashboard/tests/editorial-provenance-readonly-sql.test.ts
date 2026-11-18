import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const sqlFile = resolve(process.cwd(), "../../docs/sql-proposals/editorial_provenance_readonly_audit.sql");
const sql = readFileSync(sqlFile, "utf8");
const historicalFile = resolve(process.cwd(), "../../docs/sql-proposals/editorial_embedded_assessment_digest_readonly.sql");
const historicalSql = readFileSync(historicalFile, "utf8");
const historicalStatements = historicalSql.split("\n")
  .filter((line) => !line.trimStart().startsWith("--"))
  .join("\n")
  .split(";")
  .map((statement) => statement.trim())
  .filter(Boolean);

const statements = sql
  .split("\n")
  .filter((line) => !line.trimStart().startsWith("--"))
  .join("\n")
  .split(";")
  .map((statement) => statement.trim())
  .filter(Boolean);

describe("editorial provenance SQL read-only diagnostic", () => {
  it("contains six fixed-policy aggregate SELECT queries only, no mutation or exposed raw text", () => {
    expect(statements).toHaveLength(6);
    for (const statement of statements) {
      expect(statement).toMatch(/^(with\s+|select\s+)/i);
      expect(statement).not.toMatch(/\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|execute|copy|call|pg_sleep|dblink|http_post)\b/i);
      expect(statement).not.toMatch(/\b(arabic_text|translation_text|footnotes|bearer|secret_key|service_role)\b/i);
      expect(statement).toContain("policy_version = 'calm-inviting-v1'");
    }
  });

  it("recomputes legacy embedded reviewer SHA-256 with an aggregate-only, source-pinned SELECT", () => {
    expect(historicalStatements).toHaveLength(1);
    const query = historicalStatements[0];
    expect(query).toMatch(/^with legacy_reviewer_content as/i);
    expect(query).toContain("policy_version = 'calm-inviting-v1'");
    expect(query).toContain("jsonb_object_keys(reviewer.stored_assessment)");
    expect(query).toContain("string_agg(item::text, ',' ORDER BY ord)");
    expect(query).toContain("ORDER BY k");
    expect(query).toContain("extensions.digest(");
    expect(query).toContain("raw_assessment_sha256");
    expect(query).toContain("mismatched_or_missing_digest");
    expect(query).not.toMatch(/\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|execute|call|copy)\b/i);
    expect(query).not.toMatch(/select\s+\*|\breturning\b/i);
  });

  it("does not conflate missing raw-response provenance, missing passage schema and NOT_SCREENED", () => {
    expect(statements[0]).toContain("count(*) filter (where missing_passage)");
    expect(statements[0]).toContain("count(*) filter (where missing_raw)");
    expect(statements[0]).toContain("count(*) filter (where missing_raw or missing_passage)");
    expect(statements[0]).toContain("count(*) filter (where not missing_raw and not missing_passage)");
    expect(statements[1]).toContain("from public.editorial_ayah_reviews");
    expect(statements[2]).toContain("group by surah_number");
    expect(statements[3]).toContain("surah_number = 21 and ayah_number between 52 and 112");
    expect(statements[3]).toContain("surah_number = 22 and ayah_number between 1 and 9");
    expect(statements[4]).toContain("having count(*) <> 2 or count(distinct pass_number) <> 2");
    expect(statements[5]).toContain("missing_raw_sha");
    expect(statements[5]).toContain("malformed_raw_sha");
    // Different validated native-review generations use different raw digest names.
    for (const query of [statements[0], statements[5]]) {
      expect(query).toContain("raw_output_sha256");
      expect(query).toContain("raw_assessment_sha256");
      expect(query).toContain("coalesce(");
    }
  });
});
