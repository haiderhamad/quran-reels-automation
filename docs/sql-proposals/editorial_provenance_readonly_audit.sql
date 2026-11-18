-- READ-ONLY; no DML/DDL/RPC. Do NOT apply as migration.
-- Production policy: calm-inviting-v1.
-- 2026-10-25 correction: accept BOTH proven historical metadata names:
-- raw_output_sha256 (native v3, original reviewer output FILE digest)
-- and raw_assessment_sha256 (older native reviewer digest).
-- Their serialization scopes differ. Presence of either does NOT by itself
-- prove model independence, saved original bytes, or interpretation quality.

-- Query 1: six canonical, distinct-ayah evidence counters.
with per_ayah as (
  select surah_number, ayah_number, count(*) as pass_rows,
    bool_or(not (
      evidence ? 'passage_first_ayah'
      and evidence ? 'passage_last_ayah'
      and evidence ? 'standalone_sufficient'
    )) as missing_passage,
    bool_or(coalesce(
      evidence->'_evaluator_audit'->>'raw_output_sha256',
      evidence->'_evaluator_audit'->>'raw_assessment_sha256'
    ) is null) as missing_raw
  from public.editorial_ayah_assessments
  where policy_version = 'calm-inviting-v1'
  group by surah_number, ayah_number
)
select count(*) as reviewed_identities, sum(pass_rows) as assessment_rows,
 count(*) filter (where missing_passage) as affected_passage,
 count(*) filter (where missing_raw) as affected_raw,
 count(*) filter (where missing_raw or missing_passage) as affected_union,
 count(*) filter (where not missing_raw and not missing_passage) as neither_gap
from per_ayah;

-- Query 2: live screening state, NOT publication or source certification.
select screening_status, count(*) as ayahs
from public.editorial_ayah_reviews
where policy_version = 'calm-inviting-v1'
group by screening_status
order by screening_status;

-- Query 3: distinct canonical ayahs with legacy incomplete passage data.
select surah_number, count(*) as affected_ayahs
from (
  select surah_number, ayah_number
  from public.editorial_ayah_assessments
  where policy_version = 'calm-inviting-v1'
  and not (
    evidence ? 'passage_first_ayah'
    and evidence ? 'passage_last_ayah'
    and evidence ? 'standalone_sufficient'
  )
  group by surah_number, ayah_number
) as flagged
group by surah_number
order by surah_number;

-- Query 4: overlap with 70 historically identified fallback ayahs.
select count(*) as affected_known_fallback_ayahs
from (
  select surah_number, ayah_number
  from public.editorial_ayah_assessments
  where policy_version = 'calm-inviting-v1'
  and not (
    evidence ? 'passage_first_ayah'
    and evidence ? 'passage_last_ayah'
    and evidence ? 'standalone_sufficient'
  )
  and (
    (surah_number = 21 and ayah_number between 52 and 112)
    or (surah_number = 22 and ayah_number between 1 and 9)
  )
  group by surah_number, ayah_number
) as flagged;

-- Query 5: exactly two distinct pass indices for every assessment key.
select count(*) as problematic_identities
from (
  select surah_number, ayah_number
  from public.editorial_ayah_assessments
  where policy_version = 'calm-inviting-v1'
  group by surah_number, ayah_number
  having count(*) <> 2 or count(distinct pass_number) <> 2
) as anomalies;

-- Query 6: missing or malformed digest across both authentic field names.
select count(*) as pass_rows,
 count(*) filter (
   where coalesce(
     evidence->'_evaluator_audit'->>'raw_output_sha256',
     evidence->'_evaluator_audit'->>'raw_assessment_sha256'
   ) is null
 ) as missing_raw_sha,
 count(*) filter (
   where coalesce(
     evidence->'_evaluator_audit'->>'raw_output_sha256',
     evidence->'_evaluator_audit'->>'raw_assessment_sha256'
   ) is not null and (
     length(coalesce(
       evidence->'_evaluator_audit'->>'raw_output_sha256',
       evidence->'_evaluator_audit'->>'raw_assessment_sha256'
     )) <> 64 or not (
       coalesce(
         evidence->'_evaluator_audit'->>'raw_output_sha256',
         evidence->'_evaluator_audit'->>'raw_assessment_sha256'
       ) ~ '^[a-f0-9]{64}'
     )
   )
 ) as malformed_raw_sha,
 count(*) filter (where (evidence->>'prompt_sha256') is null) as missing_prompt_sha
from public.editorial_ayah_assessments
where policy_version = 'calm-inviting-v1';

-- All six queries are aggregate-only SELECTs; never output raw Quran text.
