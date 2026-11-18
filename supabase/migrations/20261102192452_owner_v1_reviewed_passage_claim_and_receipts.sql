create table public.calm_reels_v1_reviewed_passages (
  first_ayah_key text primary key references public.calm_reels_v1_catalog_members(ayah_key),
  last_ayah_key text not null references public.calm_reels_v1_catalog_members(ayah_key),
  approval_reference text not null check(length(approval_reference)>8),
  reviewed_at date not null
);
-- Each installation records only its own reviewed passages.
alter table public.calm_reels_v1_reviewed_passages enable row level security;
revoke all on public.calm_reels_v1_reviewed_passages from public, anon, authenticated;
grant select on public.calm_reels_v1_reviewed_passages to service_role;

-- Keep the existing one/two-ayah claim RPC from silently splitting an approved
-- longer passage during a version transition. Check every overlapping catalog
-- member, not only the first reviewed key.
create function public.calm_reels_v1_guard_reviewed_claim()
returns trigger language plpgsql security invoker
set search_path = ''
as $guard_reviewed$
begin
  if exists (
    select 1
    from public.calm_reels_v1_reviewed_passages p
    join public.calm_reels_v1_catalog_members reviewed_first
      on reviewed_first.ayah_key=p.first_ayah_key
    join public.calm_reels_v1_catalog_members reviewed_last
      on reviewed_last.ayah_key=p.last_ayah_key
    join public.calm_reels_v1_catalog_members requested_first
      on requested_first.ayah_key=new.ayah_key
    join public.calm_reels_v1_catalog_members requested_last
      on requested_last.ayah_key=new.last_ayah_key
    where requested_first.ordinal <= reviewed_last.ordinal
      and requested_last.ordinal >= reviewed_first.ordinal
      and (new.ayah_key is distinct from p.first_ayah_key
        or new.last_ayah_key is distinct from p.last_ayah_key)
  ) then
    raise exception 'Claim overlaps an exact owner-reviewed passage';
  end if;
  return new;
end;
$guard_reviewed$;

create trigger calm_reels_v1_reviewed_claim_guard
before insert or update of ayah_key,last_ayah_key
on public.calm_reels_v1_dispatch_claims
for each row execute function public.calm_reels_v1_guard_reviewed_claim();
revoke all on function public.calm_reels_v1_guard_reviewed_claim()
  from public,anon,authenticated;

-- First pending, one outstanding claim per cycle, exact saved run and approved
-- boundary. All five member identities are reserved by ONE transaction/claim.
create function public.calm_reels_v1_claim_next_reviewed_passage(
  p_cycle_number integer, p_first_ayah_key text, p_last_ayah_key text, p_run_id text
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
begin
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
        'last_ayah_key',p_last_ayah_key,'cycle_number',current_cycle.cycle_number);
    end if;
    raise exception 'Pending dispatch requires independent provider reconciliation';
  end if;
  insert into public.calm_reels_v1_dispatch_claims
    (cycle_number,ayah_key,last_ayah_key,run_id)
  values (current_cycle.cycle_number,p_first_ayah_key,p_last_ayah_key,p_run_id);
  return pg_catalog.jsonb_build_object(
    'state','CLAIMED','ayah_key',p_first_ayah_key,
    'last_ayah_key',p_last_ayah_key,'cycle_number',current_cycle.cycle_number);
end;
$claim_reviewed$;
revoke all on function public.calm_reels_v1_claim_next_reviewed_passage(integer,text,text,text)
  from public,anon,authenticated;
grant execute on function public.calm_reels_v1_claim_next_reviewed_passage(integer,text,text,text)
  to service_role;

-- Replace only the receipt verifier body without modifying its public RPC
-- signature. It retains atomic claim->proof->insert all members->confirm->count.
create or replace function public.calm_reels_v1_record_verified_facebook_post(
  p_cycle_number integer,
  p_run_id text,
  p_facebook_post_id text,
  p_platform_evidence_sha256 text,
  p_verified_at timestamptz
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $receipt$
declare
  current_cycle public.calm_reels_v1_cycle_state%rowtype;
  run_record public.run_records%rowtype;
  surah_n integer;
  first_n integer;
  last_n integer;
  ayah_keys text[];
  first_missing text;
  already_count integer;
  confirmed_count integer;
  active_claim public.calm_reels_v1_dispatch_claims%rowtype;
begin
  select * into current_cycle
  from public.calm_reels_v1_cycle_state
  where singleton=true for update;
  if not found or current_cycle.catalog_sha256 <>
    'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d'
    or current_cycle.total_ayahs <> 2211 then
    raise exception 'Verified fresh catalog unavailable';
  end if;
  if p_cycle_number is distinct from current_cycle.cycle_number
    or p_run_id is null or length(p_run_id)=0
    or p_facebook_post_id is null
    or p_facebook_post_id !~ '^[1-9][0-9]{5,24}$'
    or p_platform_evidence_sha256 is null
    or p_platform_evidence_sha256 !~ '^[a-f0-9]{64}$'
    or p_verified_at is null
    or p_verified_at < current_cycle.started_at
    or p_verified_at > now() + interval '5 minutes' then
    raise exception 'Invalid or stale primary-platform receipt';
  end if;

  select * into run_record from public.run_records where run_id=p_run_id;
  if not found or run_record.run_kind not in ('scheduled','manual_production')
    or run_record.status is distinct from 'SUCCESS'
    or run_record.final_safety_passed is distinct from true
    or run_record.media_key is null
    or length(run_record.media_key)=0
    or run_record.created_at is null
    or run_record.created_at < current_cycle.started_at
    or run_record.completed_at is null
    or run_record.completed_at < current_cycle.started_at
    or run_record.value->>'facebook_status' is distinct from 'SUCCESS'
    or run_record.value->>'facebook_media_id' is distinct from p_facebook_post_id
    or run_record.value->>'buffer_post_status' is distinct from 'sent'
    or run_record.value->>'buffer_post_id' is null
    or run_record.value->>'facebook_reel_url' is null
    or run_record.value->>'media_key' is distinct from run_record.media_key
    or run_record.value->>'run_id' is distinct from p_run_id then
    raise exception 'Publication is not an independently verified current-cycle successful Reel';
  end if;

  surah_n := (run_record.value->>'surah_number')::integer;
  first_n := (run_record.value->>'first_ayah')::integer;
  last_n := (run_record.value->>'last_ayah')::integer;
  if surah_n is null or first_n is null or last_n is null
    or surah_n not between 1 and 114 or first_n < 1
    or last_n < first_n then
    raise exception 'Owner-v1 requires a valid contiguous ayah range';
  end if;
  if last_n > first_n + 1 and not exists (
    select 1 from public.calm_reels_v1_reviewed_passages reviewed
    where reviewed.first_ayah_key = (surah_n::text||':'||first_n::text)
      and reviewed.last_ayah_key = (surah_n::text||':'||last_n::text)
  ) then
    raise exception 'Extended Reel range is not owner reviewed';
  end if;
  if not exists (select 1 from public.calm_reels_v1_dispatch_claims c
     where c.cycle_number=p_cycle_number and c.run_id=p_run_id
       and c.ayah_key=(surah_n::text||':'||first_n::text)
       and c.last_ayah_key=(surah_n::text||':'||last_n::text)) then
    raise exception 'Receipt range does not match reserved passage';
  end if;
  if run_record.value->'provenance'->'owner_v1'->>'catalog_id'
       is distinct from 'calm-reels-editorial-shortlist-v1'
     or run_record.value->'provenance'->'owner_v1'->>'ayah_key'
       is distinct from (surah_n::text||':'||first_n::text)
     or run_record.value->'provenance'->'owner_v1'->>'cycle_number'
       is distinct from current_cycle.cycle_number::text
     or run_record.value->'provenance'->'owner_v1'->>'editorial_selection_only'
       is distinct from 'true'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'schema'
       is distinct from 'owner-v1-facebook-verified-delivery-v1'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'facebook_video_id'
       is distinct from p_facebook_post_id
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'evidence_sha256'
       is distinct from p_platform_evidence_sha256
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'buffer_post_id'
       is distinct from run_record.value->>'buffer_post_id'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'buffer_post_status'
       is distinct from 'sent'
     or run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'permalink_url'
       is distinct from run_record.value->>'facebook_reel_url'
     or (run_record.value->'provenance'->'owner_v1'->'facebook_verification'->>'verified_at')::timestamptz
       is distinct from p_verified_at
     or (run_record.value->>'facebook_published_at')::timestamptz
       is distinct from p_verified_at then
    raise exception 'External Facebook proof is not bound to the exact owner run';
  end if;

  select array_agg(surah_n::text||':'||g.n::text order by g.n)
  into ayah_keys
  from pg_catalog.generate_series(first_n,last_n) as g(n);
  if ayah_keys is null or exists (
    select 1 from pg_catalog.unnest(ayah_keys) as x(key)
    where not exists (
      select 1 from public.calm_reels_v1_catalog_members m where m.ayah_key=x.key
    )
  ) then
    raise exception 'Reel range crosses the owner frozen list';
  end if;
  if last_n <> first_n and run_record.value#>'{provenance,owner_v1,ayah_keys}'
      is distinct from pg_catalog.to_jsonb(ayah_keys) then
    raise exception 'Exact complete passage source identity changed';
  end if;
  if last_n > first_n + 1 and exists (
    select 1 from pg_catalog.unnest(ayah_keys) as member(key)
    where coalesce((run_record.value#>'{provenance,owner_v1,source_sha256_by_key}'->>member.key), '')
      !~ '^[a-f0-9]{64}$'
  ) then
    raise exception 'Extended passage missing per-ayah source hashes';
  end if;

  -- Idempotent repeat of an EXACT already-recorded complete Reel is allowed.
  select count(*) into already_count
  from public.calm_reels_v1_confirmed_posts existing
  where existing.cycle_number=current_cycle.cycle_number
    and existing.ayah_key = any(ayah_keys)
    and existing.platform='facebook'
    and existing.run_id=p_run_id
    and existing.external_post_id=p_facebook_post_id
    and existing.media_key=run_record.media_key
    and existing.evidence_sha256=p_platform_evidence_sha256;
  if already_count=pg_catalog.cardinality(ayah_keys) then
    return pg_catalog.jsonb_build_object(
      'state','ALREADY_RECORDED','cycle_number',current_cycle.cycle_number,
      'recorded_ayah_count',pg_catalog.cardinality(ayah_keys),
      'confirmed_ayahs',(select count(*) from public.calm_reels_v1_confirmed_posts
        where cycle_number=current_cycle.cycle_number)
    );
  end if;
  if already_count<>0 then
    raise exception 'Partial prior delivery evidence requires manual reconciliation';
  end if;

  select * into active_claim
  from public.calm_reels_v1_dispatch_claims c
  where c.cycle_number=current_cycle.cycle_number
    and c.ayah_key=ayah_keys[1]
    and c.run_id=p_run_id and c.state='CLAIMED';
  if not found then
    raise exception 'Facebook receipt requires matching pre-dispatch claim';
  end if;

  select m.ayah_key into first_missing
  from public.calm_reels_v1_catalog_members m
  where not exists (
    select 1 from public.calm_reels_v1_confirmed_posts posted
    where posted.cycle_number=current_cycle.cycle_number
      and posted.ayah_key=m.ayah_key and posted.platform='facebook'
  )
  order by m.ordinal limit 1;
  if first_missing is distinct from ayah_keys[1] then
    raise exception 'Out-of-order or duplicate Reel: first missing ayah mismatch';
  end if;

  -- All ayahs of one exact approved Reel are committed atomically.
  insert into public.calm_reels_v1_confirmed_posts
    (cycle_number,ayah_key,run_id,media_key,platform,external_post_id,
     evidence_sha256,verified_at)
  select current_cycle.cycle_number,x.key,p_run_id,run_record.media_key,
         'facebook',p_facebook_post_id,p_platform_evidence_sha256,p_verified_at
  from pg_catalog.unnest(ayah_keys) as x(key);

  update public.calm_reels_v1_dispatch_claims
    set state='CONFIRMED',confirmed_at=p_verified_at
  where cycle_number=current_cycle.cycle_number
    and ayah_key=ayah_keys[1] and run_id=p_run_id and state='CLAIMED';
  if not found then
    raise exception 'Dispatch claim transition failed';
  end if;

  select count(*) into confirmed_count
  from public.calm_reels_v1_confirmed_posts
  where cycle_number=current_cycle.cycle_number and platform='facebook';
  if confirmed_count>2211 then
    raise exception 'More verified posts than owner catalogue';
  end if;
  if confirmed_count=2211 then
    -- Atomic rollover; preserves previous-cycle receipts and every video.
    insert into public.calm_reels_v1_completed_cycles
      (cycle_number,catalog_sha256,confirmed_ayahs)
    values (current_cycle.cycle_number,current_cycle.catalog_sha256,2211);
    update public.calm_reels_v1_cycle_state
      set cycle_number=current_cycle.cycle_number+1,started_at=now()
      where singleton=true;
    return pg_catalog.jsonb_build_object(
      'state','ROLLED_OVER','cycle_number',current_cycle.cycle_number+1,
      'confirmed_ayahs',0,'total_ayahs',2211,
      'recorded_ayah_count',pg_catalog.cardinality(ayah_keys));
  end if;
  return pg_catalog.jsonb_build_object(
    'state','RECORDED','cycle_number',current_cycle.cycle_number,
    'confirmed_ayahs',confirmed_count,'total_ayahs',2211,
    'recorded_ayah_count',pg_catalog.cardinality(ayah_keys));
end;
$receipt$;

revoke all on function public.calm_reels_v1_record_verified_facebook_post(
  integer,text,text,text,timestamptz) from public,anon,authenticated;
grant execute on function public.calm_reels_v1_record_verified_facebook_post(
  integer,text,text,text,timestamptz) to service_role;
