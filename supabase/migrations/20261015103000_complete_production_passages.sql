-- An automatic decision authorizes its target identity. Context members must
-- independently have a valid authorization before the whole range is selected.
-- Preserve all approval records, including ranges awaiting context completion.
create or replace view public.editorial_production_authorized_passages
with (security_invoker = true)
as
with candidates as (
  select policy_version, surah_number, passage_first_ayah, passage_last_ayah,
    array_agg(authorization_id order by authorization_id) as authorization_ids,
    array_agg(distinct approval_basis order by approval_basis) as approval_bases,
    array_agg(distinct (surah_number::text || ':' || ayah_number::text)
      order by (surah_number::text || ':' || ayah_number::text)) as candidate_ayah_keys,
    md5(string_agg(authorization_id::text, ',' order by authorization_id)) as authorization_fingerprint
  from public.editorial_production_authorized_ayahs
  group by policy_version, surah_number, passage_first_ayah, passage_last_ayah
)
select c.* from candidates c
where not exists (
  select 1 from generate_series(c.passage_first_ayah, c.passage_last_ayah) member(ayah)
  where not exists (
    select 1 from public.editorial_production_authorized_ayahs a
    where a.policy_version = c.policy_version and a.surah_number = c.surah_number
      and a.ayah_number = member.ayah
      and a.passage_first_ayah >= c.passage_first_ayah
      and a.passage_last_ayah <= c.passage_last_ayah
  )
);

-- Add a versioned RPC instead of breaking the original ingestion/client
-- contract. Return the exact source evidence used by screening when present.
create or replace function public.get_production_authorized_passages_v2(p_policy_version text)
returns jsonb
language sql
stable
set search_path = pg_catalog, public
as $authorized_passages_v2$
  select coalesce(jsonb_agg(row_payload order by surah_number, first_ayah, last_ayah), '[]'::jsonb)
  from (
    select p.surah_number, p.passage_first_ayah as first_ayah, p.passage_last_ayah as last_ayah,
      to_jsonb(p) || jsonb_build_object('source_evidence', coalesce(e.sources, '{}'::jsonb)) as row_payload
    from public.editorial_production_authorized_passages p
    left join lateral (
      select jsonb_object_agg(s.surah_number::text || ':' || s.ayah_number::text,
        jsonb_build_object('source_hash', s.source_hash, 'translation_hash', s.translation_hash,
          'translation_language', s.translation_language, 'translation_version', s.translation_version)) as sources
      from (
        select distinct on (s.ayah_number) s.*
        from public.editorial_source_evidence s
        where s.policy_version = p.policy_version and s.surah_number = p.surah_number
          and s.ayah_number between p.passage_first_ayah and p.passage_last_ayah
          and s.translation_language = 'en' and s.translation_version = '1.0.19'
          and s.source_hash = public.editorial_normalized_text_sha256(s.arabic_text)
          and s.translation_hash = public.editorial_normalized_text_sha256(s.translation_text)
          and exists (
            select 1 from public.editorial_production_authorized_ayahs a
            where a.policy_version = s.policy_version and a.surah_number = s.surah_number
              and a.ayah_number = s.ayah_number
              and (a.approval_basis = 'LEGACY_CATALOG'
                or (a.source_hash = s.source_hash and a.translation_hash = s.translation_hash))
          )
        order by s.ayah_number, s.retrieved_at desc, s.source_hash
      ) s
    ) e on true
    where p.policy_version = p_policy_version
      and ('LEGACY_CATALOG' = any(p.approval_bases)
        or (select count(*) from jsonb_object_keys(coalesce(e.sources, '{}'::jsonb)))
          = p.passage_last_ayah - p.passage_first_ayah + 1)
  ) resolved;
$authorized_passages_v2$;

revoke all on function public.get_production_authorized_passages_v2(text)
  from public, anon, authenticated;
grant execute on function public.get_production_authorized_passages_v2(text) to service_role;
