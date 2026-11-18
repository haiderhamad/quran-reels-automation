-- Durable, mutually-exclusive work claiming for the Quran editorial screener.
--
-- This is additive to the already deployed editorial screening ledger.  It
-- neither edits historical reviews nor turns a candidate into a production
-- approval.  Workers only lease NOT_SCREENED identities and the existing
-- ingest_editorial_screening_batch RPC remains the sole writer of evidence.

create table if not exists public.editorial_screening_leases (
  policy_version text not null,
  surah_number integer not null,
  ayah_number integer not null,
  worker_id text not null,
  run_id text not null references public.editorial_screening_runs(run_id),
  lease_token uuid not null default gen_random_uuid(),
  claimed_at timestamptz not null default now(),
  lease_expires_at timestamptz not null,
  last_error text,
  primary key (policy_version, surah_number, ayah_number),
  foreign key (policy_version, surah_number, ayah_number)
    references public.editorial_ayah_reviews(policy_version, surah_number, ayah_number),
  check (lease_expires_at > claimed_at)
);

create index if not exists editorial_screening_leases_expiry_idx
  on public.editorial_screening_leases (policy_version, lease_expires_at);

create table if not exists public.editorial_screening_worker_jobs (
  job_id text primary key,
  policy_version text not null,
  assessment_version text not null,
  run_id text not null references public.editorial_screening_runs(run_id),
  worker_id text not null,
  status text not null check (status in ('STARTING', 'RUNNING', 'PAUSED', 'FAILED', 'COMPLETED')),
  claimed_ayahs integer not null default 0 check (claimed_ayahs >= 0),
  committed_ayahs integer not null default 0 check (committed_ayahs >= 0),
  failed_ayahs integer not null default 0 check (failed_ayahs >= 0),
  last_error text,
  started_at timestamptz not null default now(),
  heartbeat_at timestamptz not null default now(),
  completed_at timestamptz,
  updated_at timestamptz not null default now(),
  check ((status = 'COMPLETED') = (completed_at is not null))
);

create index if not exists editorial_screening_worker_jobs_activity_idx
  on public.editorial_screening_worker_jobs (policy_version, updated_at desc);

alter table public.editorial_screening_leases enable row level security;
alter table public.editorial_screening_worker_jobs enable row level security;

create or replace function public.claim_editorial_screening_batch(
  p_policy_version text,
  p_run_id text,
  p_worker_id text,
  p_limit integer default 5,
  p_lease_seconds integer default 1800
) returns table (surah_number integer, ayah_number integer, lease_token uuid, lease_expires_at timestamptz)
language plpgsql security invoker set search_path = public
as $$
begin
  if coalesce(length(trim(p_policy_version)), 0) = 0
     or coalesce(length(trim(p_run_id)), 0) = 0
     or coalesce(length(trim(p_worker_id)), 0) = 0
     or p_limit not between 1 and 50
     or p_lease_seconds not between 300 and 3600 then
    raise exception 'invalid editorial screening lease input';
  end if;
  if not exists (
    select 1 from public.editorial_screening_runs
    where run_id = p_run_id and policy_version = p_policy_version
      and status in ('STARTED', 'RUNNING', 'PAUSED')
  ) then
    raise exception 'screening run is not active for this policy';
  end if;

  return query
  with candidates as (
    select r.surah_number, r.ayah_number
      from public.editorial_ayah_reviews r
     where r.policy_version = p_policy_version
       and r.screening_status <> 'SCREENED'
       and not exists (
         select 1 from public.editorial_screening_leases l
          where l.policy_version = r.policy_version
            and l.surah_number = r.surah_number
            and l.ayah_number = r.ayah_number
            and l.lease_expires_at > now()
       )
     order by r.surah_number, r.ayah_number
     for update skip locked
     limit p_limit
  ), leased as (
    insert into public.editorial_screening_leases (
      policy_version, surah_number, ayah_number, worker_id, run_id, lease_expires_at
    )
    select p_policy_version, c.surah_number, c.ayah_number, p_worker_id, p_run_id,
      now() + make_interval(secs => p_lease_seconds)
    from candidates c
    on conflict (policy_version, surah_number, ayah_number) do update
      set worker_id = excluded.worker_id,
          run_id = excluded.run_id,
          lease_token = gen_random_uuid(),
          claimed_at = now(),
          lease_expires_at = excluded.lease_expires_at,
          last_error = null
      where public.editorial_screening_leases.lease_expires_at <= now()
    returning editorial_screening_leases.surah_number,
      editorial_screening_leases.ayah_number,
      editorial_screening_leases.lease_token,
      editorial_screening_leases.lease_expires_at
  ) select * from leased;
end;
$$;

create or replace function public.renew_editorial_screening_lease(
  p_policy_version text,
  p_surah_number integer,
  p_ayah_number integer,
  p_worker_id text,
  p_lease_token uuid,
  p_lease_seconds integer default 1800
) returns boolean
language plpgsql security invoker set search_path = public
as $$
begin
  if p_lease_seconds not between 300 and 3600 then
    raise exception 'invalid editorial screening lease duration';
  end if;
  update public.editorial_screening_leases
     set lease_expires_at = now() + make_interval(secs => p_lease_seconds)
   where policy_version = p_policy_version and surah_number = p_surah_number
     and ayah_number = p_ayah_number and worker_id = p_worker_id
     and lease_token = p_lease_token and lease_expires_at > now();
  return found;
end;
$$;

create or replace function public.release_editorial_screening_lease(
  p_policy_version text,
  p_surah_number integer,
  p_ayah_number integer,
  p_worker_id text,
  p_lease_token uuid,
  p_error text default null
) returns boolean
language plpgsql security invoker set search_path = public
as $$
begin
  delete from public.editorial_screening_leases
   where policy_version = p_policy_version and surah_number = p_surah_number
     and ayah_number = p_ayah_number and worker_id = p_worker_id
     and lease_token = p_lease_token;
  return found;
end;
$$;

-- Keep the existing editorial_screening_summary contract intact.  This
-- projection adds operational fields without replacing a live dashboard view.
create or replace view public.editorial_screening_worker_summary
with (security_invoker = true) as
select
  r.policy_version,
  count(*)::integer as canonical_ayahs,
  count(*) filter (where r.screening_status = 'SCREENED')::integer as screened_ayahs,
  count(*) filter (where r.screening_status <> 'SCREENED')::integer as not_screened_ayahs,
  count(*) filter (where r.semantic_assessment = 'ELIGIBLE_CANDIDATE')::integer as eligible_candidates,
  count(*) filter (where r.semantic_assessment = 'INELIGIBLE_CANDIDATE')::integer as ineligible_candidates,
  count(*) filter (where r.semantic_assessment = 'AMBIGUOUS_REVIEW_REQUIRED')::integer as ambiguous_candidates,
  count(*) filter (where r.review_status = 'APPROVED_REVIEWED')::integer as production_approved_ayahs,
  max(r.screened_at) as last_successful_screening_at
from public.editorial_ayah_reviews r
group by r.policy_version;

revoke all on function public.claim_editorial_screening_batch(text, text, text, integer, integer) from public, anon, authenticated;
revoke all on function public.renew_editorial_screening_lease(text, integer, integer, text, uuid, integer) from public, anon, authenticated;
revoke all on function public.release_editorial_screening_lease(text, integer, integer, text, uuid, text) from public, anon, authenticated;
grant execute on function public.claim_editorial_screening_batch(text, text, text, integer, integer) to service_role;
grant execute on function public.renew_editorial_screening_lease(text, integer, integer, text, uuid, integer) to service_role;
grant execute on function public.release_editorial_screening_lease(text, integer, integer, text, uuid, text) to service_role;
