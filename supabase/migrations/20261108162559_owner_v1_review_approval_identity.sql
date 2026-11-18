-- Local migration artifact only. Applying it requires a separately approved release.
-- GRANT SELECT is additive: hosted default ACLs can already grant every right.
-- Repair only this registry, including existing column grants, not global defaults.
revoke all privileges on table public.calm_reels_v1_reviewed_passages from service_role;
revoke all privileges (first_ayah_key,last_ayah_key,approval_reference,reviewed_at)
  on table public.calm_reels_v1_reviewed_passages from service_role;
grant select on table public.calm_reels_v1_reviewed_passages to service_role;

-- An inherited/PUBLIC grant cannot be removed by revoking this role's direct ACL.
-- Refuse the transaction rather than silently leave effective write authority.
do $review_acl$
begin
  if not pg_catalog.has_table_privilege('service_role','public.calm_reels_v1_reviewed_passages','SELECT')
    or pg_catalog.has_table_privilege('service_role','public.calm_reels_v1_reviewed_passages',
      'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
    or pg_catalog.has_any_column_privilege('service_role','public.calm_reels_v1_reviewed_passages',
      'INSERT,UPDATE,REFERENCES') then
    raise exception 'Review registry must be SELECT-only for service_role';
  end if;
end;
$review_acl$;

-- A shared advisory lock serializes review writes with claims without granting
-- UPDATE on this owner-controlled registry to the worker's read-only role.
create function public.calm_reels_v1_lock_review_approval()
returns trigger language plpgsql security invoker
set search_path = ''
as $review_lock$
declare
  old_key text;
  new_key text;
begin
  if tg_op <> 'INSERT' then old_key := old.first_ayah_key; end if;
  if tg_op <> 'DELETE' then new_key := new.first_ayah_key; end if;
  -- Sort both keys when an operator changes the first boundary.
  if old_key is not null and (new_key is null or old_key <= new_key) then
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('quran-reels:review:'||old_key,0));
  end if;
  if new_key is not null and new_key is distinct from old_key then
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('quran-reels:review:'||new_key,0));
  end if;
  if old_key is not null and new_key is not null and old_key > new_key then
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('quran-reels:review:'||old_key,0));
  end if;
  if tg_op = 'DELETE' then return old; end if;
  return new;
end;
$review_lock$;
revoke all on function public.calm_reels_v1_lock_review_approval() from public,anon,authenticated;
create trigger calm_reels_v1_review_approval_lock
before insert or update or delete on public.calm_reels_v1_reviewed_passages
for each row execute function public.calm_reels_v1_lock_review_approval();

-- Old workers cannot silently bypass the new evidence-bound reservation contract.
create or replace function public.calm_reels_v1_claim_next_reviewed_passage(
  p_cycle_number integer, p_first_ayah_key text, p_last_ayah_key text, p_run_id text
) returns jsonb
language plpgsql security invoker
set search_path = ''
as $legacy_claim$
begin
  raise exception 'Review approval proof required; use the five-argument claim';
end;
$legacy_claim$;
revoke all on function public.calm_reels_v1_claim_next_reviewed_passage(integer,text,text,text)
  from public,anon,authenticated;
grant execute on function public.calm_reels_v1_claim_next_reviewed_passage(integer,text,text,text)
  to service_role;

create function public.calm_reels_v1_claim_next_reviewed_passage(
  p_cycle_number integer, p_first_ayah_key text, p_last_ayah_key text, p_run_id text,
  p_review_approval_sha256 text
) returns jsonb
language plpgsql security invoker
set search_path = ''
as $claim_reviewed$
declare
  current_cycle public.calm_reels_v1_cycle_state%rowtype;
  saved public.run_records%rowtype;
  next_key text;
  outstanding public.calm_reels_v1_dispatch_claims%rowtype;
  surah_n integer;
  first_n integer;
  last_n integer;
  keys text[];
  reviewed_record public.calm_reels_v1_reviewed_passages%rowtype;
  current_approval jsonb;
  current_approval_sha256 text;
begin
  -- A waiting claim needs a fresh statement snapshot after an operator commit.
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'Review approval claim requires READ COMMITTED';
  end if;
  select * into current_cycle from public.calm_reels_v1_cycle_state
    where singleton=true for update;
  if not found or current_cycle.catalog_sha256 <>
      'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d'
    or current_cycle.total_ayahs <> 2211
    or p_cycle_number is distinct from current_cycle.cycle_number
    or p_run_id is null or length(p_run_id) not between 1 and 120 then
    raise exception 'Invalid frozen v1 cycle or run identity';
  end if;

  select m.ayah_key into next_key from public.calm_reels_v1_catalog_members m
    where not exists (
      select 1 from public.calm_reels_v1_confirmed_posts p
      where p.cycle_number=current_cycle.cycle_number and p.ayah_key=m.ayah_key
        and p.platform='facebook'
    ) order by m.ordinal limit 1;
  if next_key is null or p_first_ayah_key is distinct from next_key then
    raise exception 'Only first pending owner ayah may be claimed';
  end if;

  -- The matching write trigger holds this lock for every operator review change.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('quran-reels:review:'||p_first_ayah_key,0));
  select * into reviewed_record from public.calm_reels_v1_reviewed_passages reviewed
    where reviewed.first_ayah_key=p_first_ayah_key and reviewed.last_ayah_key=p_last_ayah_key;
  if not found then
    raise exception 'No exact owner-reviewed extended Reel authorization';
  end if;
  current_approval := pg_catalog.jsonb_build_object(
    'first_ayah_key',reviewed_record.first_ayah_key,
    'last_ayah_key',reviewed_record.last_ayah_key,
    'approval_reference',reviewed_record.approval_reference,
    'reviewed_at',reviewed_record.reviewed_at);
  current_approval_sha256 := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    pg_catalog.jsonb_build_array(reviewed_record.first_ayah_key,reviewed_record.last_ayah_key,
      reviewed_record.approval_reference,reviewed_record.reviewed_at)::text,'UTF8')),'hex');
  if p_review_approval_sha256 is null or p_review_approval_sha256 !~ '^[a-f0-9]{64}$'
    or p_review_approval_sha256 is distinct from current_approval_sha256
    or reviewed_record.approval_reference not like 'private-owner-%'
    or length(pg_catalog.btrim(reviewed_record.approval_reference)) < 9
    or reviewed_record.reviewed_at > current_date then
    raise exception 'Review approval changed or proof invalid';
  end if;

  select * into saved from public.run_records r where r.run_id=p_run_id for share;
  if not found or saved.status is distinct from 'SUCCESS'
    or saved.final_safety_passed is distinct from true
    or saved.run_kind not in ('scheduled','manual_production')
    or saved.media_key is null
    or length(saved.media_key)=0
    or saved.value->>'run_id' is distinct from p_run_id
    or saved.value->'provenance'->'owner_v1'->>'catalog_id'
      is distinct from 'calm-reels-editorial-shortlist-v1'
    or saved.value->'provenance'->'owner_v1'->>'cycle_number'
      is distinct from current_cycle.cycle_number::text
    or saved.value->'provenance'->'owner_v1'->>'editorial_selection_only'
      is distinct from 'true' then
    raise exception 'Saved final-safe owner Reel is not eligible for reservation';
  end if;
  if saved.value#>'{provenance,owner_v1,review_approval}' is distinct from current_approval
    or saved.value#>>'{provenance,owner_v1,review_approval_sha256}'
       is distinct from current_approval_sha256 then
    raise exception 'Review approval changed since generation';
  end if;
  surah_n := (saved.value->>'surah_number')::integer;
  first_n := (saved.value->>'first_ayah')::integer;
  last_n := (saved.value->>'last_ayah')::integer;
  if surah_n is null or first_n is null or last_n is null
    or surah_n not between 1 and 114 or first_n < 1
    or last_n < first_n + 2
    or p_first_ayah_key is distinct from (surah_n::text||':'||first_n::text)
    or p_last_ayah_key is distinct from (surah_n::text||':'||last_n::text)
    or saved.value->'provenance'->'owner_v1'->>'ayah_key'
      is distinct from p_first_ayah_key
    or not exists (
      select 1 from public.calm_reels_v1_reviewed_passages reviewed
      where reviewed.first_ayah_key=p_first_ayah_key
        and reviewed.last_ayah_key=p_last_ayah_key
    ) then
    raise exception 'No exact owner-reviewed extended Reel authorization';
  end if;

  select pg_catalog.array_agg(surah_n::text||':'||g.n::text order by g.n)
    into keys from pg_catalog.generate_series(first_n,last_n) as g(n);
  if keys is null
    or saved.value#>'{provenance,owner_v1,ayah_keys}'
       is distinct from pg_catalog.to_jsonb(keys)
    or exists (
      select 1 from pg_catalog.unnest(keys) as member(key)
      where not exists (
        select 1 from public.calm_reels_v1_catalog_members m
        where m.ayah_key=member.key
      ) or exists (
        select 1 from public.calm_reels_v1_confirmed_posts p
        where p.cycle_number=current_cycle.cycle_number and p.ayah_key=member.key
          and p.platform='facebook'
      ) or coalesce((saved.value#>'{provenance,owner_v1,source_sha256_by_key}'->>member.key), '')
        !~ '^[a-f0-9]{64}$'
    ) then
    raise exception 'Claim requires every unconfirmed member with a verified source';
  end if;

  select * into outstanding from public.calm_reels_v1_dispatch_claims c
    where c.cycle_number=current_cycle.cycle_number and c.state='CLAIMED';
  if found then
    if outstanding.run_id=p_run_id
      and outstanding.ayah_key=p_first_ayah_key
      and outstanding.last_ayah_key=p_last_ayah_key then
      return pg_catalog.jsonb_build_object(
        'state','ALREADY_CLAIMED','ayah_key',p_first_ayah_key,
        'last_ayah_key',p_last_ayah_key,'cycle_number',current_cycle.cycle_number,
        'review_approval_sha256',current_approval_sha256);
    end if;
    raise exception 'Pending dispatch requires independent provider reconciliation';
  end if;
  insert into public.calm_reels_v1_dispatch_claims
    (cycle_number,ayah_key,last_ayah_key,run_id)
  values (current_cycle.cycle_number,p_first_ayah_key,p_last_ayah_key,p_run_id);
  return pg_catalog.jsonb_build_object(
    'state','CLAIMED','ayah_key',p_first_ayah_key,
    'last_ayah_key',p_last_ayah_key,'cycle_number',current_cycle.cycle_number,
        'review_approval_sha256',current_approval_sha256);
end;
$claim_reviewed$;

revoke all on function public.calm_reels_v1_claim_next_reviewed_passage(integer,text,text,text,text)
  from public,anon,authenticated;
grant execute on function public.calm_reels_v1_claim_next_reviewed_passage(integer,text,text,text,text)
  to service_role;

notify pgrst, 'reload schema';
