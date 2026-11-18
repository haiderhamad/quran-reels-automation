-- READ ONLY. Recompute the historical native-v1 canonical Python JSON
-- SHA-256 of the embedded reviewer_assessment object, WITHOUT outputting
-- Quran text, source footnotes, or confidential reviewer content.
--
-- Python canonical_json_sha256:
-- json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
--
-- The observed legacy schema has flat scalar JSON fields and arrays of
-- scalar exclusions. jsonb::text is NOT the canonical representation:
-- PostgreSQL orders object keys differently and adds spaces after commas.
--
-- When new nested object/array shapes appear, this specific SQL expression
-- may no longer reproduce Python canonical encoding: do NOT treat a mismatch
-- as evidence of model tampering until the serialization scope is inspected.
-- Never replace the authenticated source/reviewer audit with hash equality.
WITH legacy_reviewer_content AS (
  SELECT
    a.surah_number,
    a.ayah_number,
    a.run_id,
    a.evidence->'_evaluator_audit'->>'raw_assessment_sha256' AS claimed_digest,
    a.evidence->'_evaluator_audit'->'reviewer_assessment' AS stored_assessment
  FROM public.editorial_ayah_assessments a
  WHERE a.policy_version = 'calm-inviting-v1'
    AND a.evidence->'_evaluator_audit' ? 'reviewer_assessment'
),
canonicalized AS (
  SELECT
    reviewer.surah_number,
    reviewer.ayah_number,
    reviewer.run_id,
    reviewer.claimed_digest,
    (
      SELECT '{' || string_agg(
        to_jsonb(k)::text || ':' ||
        CASE
          WHEN jsonb_typeof(reviewer.stored_assessment->k) = 'array'
            THEN '[' || coalesce((
              SELECT string_agg(item::text, ',' ORDER BY ord)
              FROM jsonb_array_elements(reviewer.stored_assessment->k)
                   WITH ORDINALITY AS x(item, ord)
            ), '') || ']'
          ELSE (reviewer.stored_assessment->k)::text
        END,
        ',' ORDER BY k
      ) || '}'
      FROM jsonb_object_keys(reviewer.stored_assessment) AS keys(k)
    ) AS python_style_canonical_json
  FROM legacy_reviewer_content reviewer
),
comparison AS (
  SELECT
    surah_number,
    ayah_number,
    run_id,
    (claimed_digest ~ '^[a-f0-9]{64}$') AS digest_well_formed,
    (
      encode(
        extensions.digest(
          convert_to(python_style_canonical_json, 'UTF8'), 'sha256'
        ), 'hex'
      ) = claimed_digest
    ) AS digest_matches
  FROM canonicalized
)
SELECT
  count(*) AS assessment_pass_rows,
  count(DISTINCT (surah_number, ayah_number)) AS distinct_ayahs,
  count(DISTINCT run_id) AS distinct_runs,
  count(*) FILTER (WHERE digest_well_formed AND digest_matches) AS digest_matches,
  count(*) FILTER (WHERE NOT digest_well_formed OR digest_matches IS DISTINCT FROM TRUE)
    AS mismatched_or_missing_digest
FROM comparison;

-- No raw review JSON is selected or exported; no DB mutations performed.
