-- Phase 8B: metadata-only background discovery queue shared by GitHub and
-- optional local workers.  Verified safety evidence remains canonical in
-- background_assets/background_events; this table never stores video bytes.

create table if not exists public.background_candidates (
  candidate_id uuid primary key default gen_random_uuid(),
  provider text not null check (provider in ('pexels', 'pixabay')),
  provider_asset_id text not null,
  provider_variant_id text not null,
  status text not null check (status in ('PENDING', 'PROCESSING', 'VERIFIED', 'SAFETY_REJECTED', 'METADATA_INELIGIBLE', 'TRANSIENT_FAILURE')),
  search_query text,
  metadata jsonb not null default '{}'::jsonb,
  discovery_count integer not null default 1 check (discovery_count >= 1),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  worker_id text,
  lease_token uuid,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  completed_at timestamptz,
  last_error_code text,
  value jsonb not null default '{}'::jsonb,
  discovered_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (provider, provider_asset_id, provider_variant_id)
);

create index if not exists background_candidates_claim_idx
  on public.background_candidates (status, lease_expires_at, discovered_at);
create index if not exists background_candidates_activity_idx
  on public.background_candidates (updated_at desc);

alter table public.background_candidates enable row level security;
-- No anon/authenticated policy: workers use the server-only service role.

create or replace function public.enqueue_background_candidate(
  p_provider text,
  p_provider_asset_id text,
  p_provider_variant_id text,
  p_search_query text,
  p_metadata jsonb,
  p_status text default 'PENDING'
) returns public.background_candidates
language plpgsql security invoker set search_path = public
as $$
declare
  result public.background_candidates;
begin
  if p_provider not in ('pexels', 'pixabay') or p_status not in ('PENDING', 'METADATA_INELIGIBLE') then
    raise exception 'invalid background candidate input';
  end if;
  insert into public.background_candidates (
    provider, provider_asset_id, provider_variant_id, status, search_query,
    metadata, value
  ) values (
    p_provider, p_provider_asset_id, p_provider_variant_id, p_status,
    nullif(p_search_query, ''), coalesce(p_metadata, '{}'::jsonb),
    jsonb_build_object('discovery_status', p_status)
  )
  on conflict (provider, provider_asset_id, provider_variant_id) do update
    set discovery_count = public.background_candidates.discovery_count + 1,
        search_query = coalesce(excluded.search_query, public.background_candidates.search_query),
        metadata = public.background_candidates.metadata || excluded.metadata,
        updated_at = now()
  returning * into result;
  return result;
end;
$$;

create or replace function public.claim_background_candidate(
  p_worker_id text,
  p_lease_seconds integer default 900
) returns public.background_candidates
language plpgsql security invoker set search_path = public
as $$
declare
  result public.background_candidates;
begin
  if coalesce(length(trim(p_worker_id)), 0) = 0 or p_lease_seconds not between 60 and 3600 then
    raise exception 'invalid background candidate lease input';
  end if;
  with candidate as (
    select candidate_id
      from public.background_candidates
     where status in ('PENDING', 'TRANSIENT_FAILURE')
        or (status = 'PROCESSING' and lease_expires_at < now())
     order by discovered_at asc, candidate_id asc
     for update skip locked
     limit 1
  )
  update public.background_candidates q
     set status = 'PROCESSING', worker_id = p_worker_id,
         lease_token = gen_random_uuid(), claimed_at = now(),
         lease_expires_at = now() + make_interval(secs => p_lease_seconds),
         attempt_count = q.attempt_count + 1, updated_at = now()
    from candidate
   where q.candidate_id = candidate.candidate_id
  returning q.* into result;
  return result;
end;
$$;

create or replace function public.complete_background_candidate(
  p_candidate_id uuid,
  p_lease_token uuid,
  p_status text,
  p_error_code text default null,
  p_value jsonb default '{}'::jsonb
) returns public.background_candidates
language plpgsql security invoker set search_path = public
as $$
declare
  result public.background_candidates;
begin
  if p_status not in ('VERIFIED', 'SAFETY_REJECTED', 'METADATA_INELIGIBLE', 'TRANSIENT_FAILURE') then
    raise exception 'invalid terminal background candidate status';
  end if;
  update public.background_candidates
     set status = p_status, completed_at = now(), lease_expires_at = null,
         worker_id = null, lease_token = null, last_error_code = nullif(p_error_code, ''),
         value = coalesce(p_value, '{}'::jsonb), updated_at = now()
   where candidate_id = p_candidate_id and lease_token = p_lease_token
  returning * into result;
  return result;
end;
$$;

revoke all on function public.enqueue_background_candidate(text, text, text, text, jsonb, text) from public, anon, authenticated;
revoke all on function public.claim_background_candidate(text, integer) from public, anon, authenticated;
revoke all on function public.complete_background_candidate(uuid, uuid, text, text, jsonb) from public, anon, authenticated;
grant execute on function public.enqueue_background_candidate(text, text, text, text, jsonb, text) to service_role;
grant execute on function public.claim_background_candidate(text, integer) to service_role;
grant execute on function public.complete_background_candidate(uuid, uuid, text, text, jsonb) to service_role;
