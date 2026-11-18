create or replace view public.editorial_screening_surah_summary
with (security_invoker = true)
as
select c.surah_number, count(*)::integer as total_ayahs,
  count(*) filter (where r.screening_status = 'SCREENED')::integer as screened_ayahs,
  count(*) filter (where r.screening_status is distinct from 'SCREENED')::integer as remaining_ayahs,
  count(*) filter (where r.screening_status = 'SCREENED'
    and r.semantic_assessment = 'ELIGIBLE_CANDIDATE')::integer as eligible_candidates,
  count(*) filter (where exists (
    select 1 from public.editorial_production_authorized_ayahs a
    where a.policy_version = 'calm-inviting-v1'
      and a.surah_number = c.surah_number and a.ayah_number = c.ayah_number
  ))::integer as production_approved_ayahs
from public.editorial_canonical_ayahs c
left join public.editorial_ayah_reviews r
  on r.policy_version = 'calm-inviting-v1'
  and r.surah_number = c.surah_number and r.ayah_number = c.ayah_number
group by c.surah_number;

revoke all on public.editorial_screening_surah_summary from public, anon, authenticated;
grant select on public.editorial_screening_surah_summary to service_role;
