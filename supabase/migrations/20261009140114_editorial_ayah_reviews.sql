-- Editorial review inventory; does not change the production publishing allowlist.
-- Current production eligibility authority remains data/reel_theme_catalog.json.
create table if not exists public.editorial_ayah_reviews (
 policy_version text not null,
 surah_number integer not null check (surah_number between 1 and 114),
 ayah_number integer not null check (ayah_number between 1 and 286),
 canonical_key text generated always as (surah_number::text || ':' || ayah_number::text) stored,
 review_status text not null check (review_status in ('APPROVED_REVIEWED','EXCLUDED_CONFIRMED','REVIEW_REQUIRED','NOT_YET_REVIEWED')),
 candidate_theme text,
 review_justification text,
 review_provenance text,
 source_evidence jsonb not null default '{}'::jsonb,
 reviewed_by text,
 reviewed_at timestamptz,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 primary key(policy_version,surah_number,ayah_number)
);
comment on table public.editorial_ayah_reviews is 'Nonproduction editorial review ledger. Only the reviewed, versioned application catalog authorizes publication. Pending and unreviewed verses must never be published.';
create index if not exists editorial_ayah_reviews_status_idx on public.editorial_ayah_reviews(policy_version, review_status);
alter table public.editorial_ayah_reviews enable row level security;
revoke all on public.editorial_ayah_reviews from public, anon, authenticated;
grant select, insert, update on public.editorial_ayah_reviews to service_role;

-- Canonical identity map is derived from engine/quran.py; all text hashes require separate source retrieval.
with surah_counts(surah_number, ayah_count) as (
 values (1,7),(2,286),(3,200),(4,176),(5,120),(6,165),(7,206),(8,75),(9,129),(10,109),(11,123),(12,111),(13,43),(14,52),(15,99),(16,128),(17,111),(18,110),(19,98),(20,135),(21,112),(22,78),(23,118),(24,64),(25,77),(26,227),(27,93),(28,88),(29,69),(30,60),(31,34),(32,30),(33,73),(34,54),(35,45),(36,83),(37,182),(38,88),(39,75),(40,85),(41,54),(42,53),(43,89),(44,59),(45,37),(46,35),(47,38),(48,29),(49,18),(50,45),(51,60),(52,49),(53,62),(54,55),(55,78),(56,96),(57,29),(58,22),(59,24),(60,13),(61,14),(62,11),(63,11),(64,18),(65,12),(66,12),(67,30),(68,52),(69,52),(70,44),(71,28),(72,28),(73,20),(74,56),(75,40),(76,31),(77,50),(78,40),(79,46),(80,42),(81,29),(82,19),(83,36),(84,25),(85,22),(86,17),(87,19),(88,26),(89,30),(90,20),(91,15),(92,21),(93,11),(94,8),(95,8),(96,19),(97,5),(98,8),(99,8),(100,11),(101,11),(102,8),(103,3),(104,9),(105,5),(106,4),(107,7),(108,3),(109,6),(110,3),(111,5),(112,4),(113,5),(114,6)
)
insert into public.editorial_ayah_reviews(policy_version,surah_number,ayah_number,review_status)
select 'calm-inviting-v1', s.surah_number, a.ayah_number, 'NOT_YET_REVIEWED'
from surah_counts s cross join lateral generate_series(1,s.ayah_count) as a(ayah_number)
on conflict (policy_version,surah_number,ayah_number) do nothing;

create or replace view public.editorial_review_summary with (security_invoker=true) as
select policy_version,
count(*)::integer as canonical_ayahs,
count(*) filter(where review_status='APPROVED_REVIEWED')::integer as approved_ayahs,
count(*) filter(where review_status='EXCLUDED_CONFIRMED')::integer as excluded_ayahs,
count(*) filter(where review_status='REVIEW_REQUIRED')::integer as requires_review_ayahs,
count(*) filter(where review_status='NOT_YET_REVIEWED')::integer as unreviewed_ayahs,
max(updated_at) as last_updated_at
from public.editorial_ayah_reviews group by policy_version;
comment on view public.editorial_review_summary is 'Read-only nonproduction review progress. Counts do not grant publishing eligibility.';
revoke all on public.editorial_review_summary from public, anon, authenticated;
grant select on public.editorial_review_summary to service_role;

do $$
declare a record;
begin
select * into a from public.editorial_review_summary where policy_version='calm-inviting-v1';
if a.canonical_ayahs<>6236 or a.approved_ayahs<>0 or a.requires_review_ayahs<>0 or a.unreviewed_ayahs<>6236 or a.excluded_ayahs<>0 then
raise exception 'Editorial source inventory mismatch: %', row_to_json(a);
end if;
end $$;
