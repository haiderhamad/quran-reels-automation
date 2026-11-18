-- REVIEW-ONLY DDL PROPOSAL, NOT A SUPABASE MIGRATION.
-- DO NOT APPLY to production. Requires isolated Postgres tests, a catalog freeze
-- validator, trusted publication-proof adapter, and explicitly approved rollout.
-- Zero payments, credentials, scheduled jobs, or public grants are involved.
--
-- Existing coverage_state / coverage_cycles track generation/upload and are
-- NEVER modified here; publication delivery is a separate evidence boundary.

create table if not exists public.publication_catalog_versions (
  catalog_id uuid primary key default gen_random_uuid(),
  catalog_version text not null unique
    check (catalog_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'),
  manifest_sha256 text not null unique
    check (manifest_sha256 ~ '^[a-f0-9]{64}$'),
  policy_version text not null,
  arabic_source_version text not null,
  translation_source_version text not null,
  approved_ayah_count integer not null check (approved_ayah_count between 1 and 6236),
  approved_surah_count integer not null check (approved_surah_count between 1 and 114),
  state text not null default 'DRAFT' check (state in ('DRAFT', 'FROZEN')),
  frozen_at timestamptz,
  created_at timestamptz not null default now(),
  check ((state = 'FROZEN') = (frozen_at is not null))
);

create table if not exists public.publication_catalog_ayahs (
  catalog_id uuid not null references public.publication_catalog_versions(catalog_id) on delete restrict,
  surah_number integer not null check (surah_number between 1 and 114),
  ayah_number integer not null check (ayah_number between 1 and 286),
  canonical_key text not null,
  arabic_sha256 text not null check (arabic_sha256 ~ '^[a-f0-9]{64}$'),
  translation_sha256 text not null check (translation_sha256 ~ '^[a-f0-9]{64}$'),
  primary key (catalog_id, canonical_key),
  unique (catalog_id, surah_number, ayah_number),
  constraint publication_catalog_ayah_key_check
    check (canonical_key = (surah_number::text || ':' || ayah_number::text))
);

create table if not exists public.publication_catalog_passages (
  catalog_id uuid not null references public.publication_catalog_versions(catalog_id) on delete restrict,
  passage_id text not null check (passage_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'),
  surah_number integer not null check (surah_number between 1 and 114),
  first_ayah integer not null check (first_ayah between 1 and 286),
  last_ayah integer not null check (last_ayah between 1 and 286),
  authorization_fingerprint text not null check (authorization_fingerprint ~ '^[a-f0-9]{32,64}$'),
  primary key (catalog_id, passage_id),
  unique (catalog_id, surah_number, first_ayah, last_ayah),
  check (last_ayah >= first_ayah)
);

create table if not exists public.publication_catalog_passage_ayahs (
  catalog_id uuid not null,
  passage_id text not null,
  canonical_key text not null,
  primary key (catalog_id, passage_id, canonical_key),
  foreign key (catalog_id, passage_id)
    references public.publication_catalog_passages(catalog_id, passage_id) on delete restrict,
  foreign key (catalog_id, canonical_key)
    references public.publication_catalog_ayahs(catalog_id, canonical_key) on delete restrict
);
create index if not exists publication_passage_members_by_ayah_idx
  on public.publication_catalog_passage_ayahs (catalog_id, canonical_key);

create table if not exists public.publication_cycles (
  cycle_number bigint primary key check (cycle_number >= 1),
  catalog_id uuid not null references public.publication_catalog_versions(catalog_id) on delete restrict,
  state text not null check (state in ('IN_PROGRESS', 'BLOCKED', 'COMPLETED')),
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (cycle_number, catalog_id),
  check ((state = 'COMPLETED') = (completed_at is not null))
);
-- Only one current publication cycle, including a blocked current cycle.
create unique index if not exists publication_single_active_cycle_idx
  on public.publication_cycles ((true))
  where state in ('IN_PROGRESS', 'BLOCKED');

create table if not exists public.publication_cycle_attempts (
  cycle_number bigint not null,
  catalog_id uuid not null,
  attempt_id uuid not null default gen_random_uuid(),
  passage_id text not null,
  run_id text not null references public.run_records(run_id) on delete restrict,
  state text not null check (state in ('RESERVED', 'UNKNOWN_OUTCOME', 'FAILED', 'CONFIRMED')),
  created_at timestamptz not null default now(),
  resolved_at timestamptz,
  primary key (cycle_number, attempt_id),
  unique (cycle_number, catalog_id, attempt_id),
  unique (cycle_number, run_id),
  foreign key (cycle_number, catalog_id)
    references public.publication_cycles(cycle_number, catalog_id) on delete restrict,
  foreign key (catalog_id, passage_id)
    references public.publication_catalog_passages(catalog_id, passage_id) on delete restrict,
  check ((state in ('FAILED', 'CONFIRMED')) = (resolved_at is not null))
);
-- A passage cannot be concurrently outstanding or confirmed twice.
create unique index if not exists publication_passage_reserved_or_delivered_idx
  on public.publication_cycle_attempts (cycle_number, passage_id)
  where state in ('RESERVED', 'UNKNOWN_OUTCOME', 'CONFIRMED');

-- A per-cycle ayah reservation / delivery projection. The unique key also
-- rejects overlapping DIFFERENT passage windows; an atomic service-role RPC
-- must write all constituent ayahs together under FOR UPDATE cycle row lock.
-- Failed attempts may release RESERVED rows; their attempt history persists.
create table if not exists public.publication_cycle_ayah_claims (
  cycle_number bigint not null,
  catalog_id uuid not null,
  canonical_key text not null,
  attempt_id uuid not null,
  stage text not null check (stage in ('RESERVED', 'DELIVERED')),
  created_at timestamptz not null default now(),
  confirmed_at timestamptz,
  primary key (cycle_number, canonical_key),
  foreign key (cycle_number, catalog_id)
    references public.publication_cycles(cycle_number, catalog_id) on delete restrict,
  foreign key (catalog_id, canonical_key)
    references public.publication_catalog_ayahs(catalog_id, canonical_key) on delete restrict,
  foreign key (cycle_number, catalog_id, attempt_id)
    references public.publication_cycle_attempts(cycle_number, catalog_id, attempt_id) on delete restrict,
  check ((stage = 'DELIVERED') = (confirmed_at is not null))
);

-- Append-only platform evidence. A trusted backend must independently verify
-- the Buffer/Meta platform-post status and exact run/source authorization
-- BEFORE inserting; the presence of this row alone does not prove authenticity.
create table if not exists public.publication_cycle_deliveries (
  delivery_id bigint generated always as identity primary key,
  cycle_number bigint not null,
  catalog_id uuid not null,
  attempt_id uuid not null,
  platform text not null check (platform in ('facebook', 'instagram')),
  publisher_backend text not null check (publisher_backend in ('buffer', 'meta')),
  external_post_id text not null check (length(external_post_id) between 1 and 255),
  external_evidence_sha256 text not null check (external_evidence_sha256 ~ '^[a-f0-9]{64}$'),
  verified_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (cycle_number, attempt_id, platform),
  unique (platform, publisher_backend, external_post_id),
  foreign key (cycle_number, catalog_id, attempt_id)
    references public.publication_cycle_attempts(cycle_number, catalog_id, attempt_id) on delete restrict
);
create index if not exists publication_cycle_delivery_lookup_idx
  on public.publication_cycle_deliveries (cycle_number, platform, verified_at);

-- Freeze is a one-way DRAFT -> FROZEN transition. The parent catalog row
-- is row-locked by UPDATE, and every member INSERT takes that SAME row lock
-- before checking state. This serializes concurrent freeze and member writes.
-- It does not prove semantic reviewer authenticity or check the manifest
-- digest against externally signed source bytes: those are explicit release
-- gates in the trusted application and audit evidence.
create or replace function public.guard_publication_catalog_version()
returns trigger language plpgsql security invoker
set search_path = ''
as $$
declare
  member_ayah_count bigint;
  member_surah_count bigint;
  member_passage_count bigint;
begin
  if TG_OP = 'INSERT' then
    if NEW.state <> 'DRAFT' or NEW.frozen_at is not null then
      raise exception 'catalog must be created as DRAFT';
    end if;
    return NEW;
  end if;
  if TG_OP = 'DELETE' then
    raise exception 'catalog versions cannot be deleted';
  end if;
  if OLD.state <> 'DRAFT' then
    raise exception 'frozen catalog cannot be changed';
  end if;
  if NEW.state <> 'FROZEN' or NEW.frozen_at is null
    or (to_jsonb(NEW) - 'state' - 'frozen_at')
       is distinct from (to_jsonb(OLD) - 'state' - 'frozen_at') then
    raise exception 'only DRAFT to FROZEN transition is allowed';
  end if;

  select count(*), count(distinct surah_number)
    into member_ayah_count, member_surah_count
    from public.publication_catalog_ayahs
    where catalog_id = OLD.catalog_id;
  select count(*) into member_passage_count
    from public.publication_catalog_passages
    where catalog_id = OLD.catalog_id;
  if member_ayah_count <> NEW.approved_ayah_count
     or member_surah_count <> NEW.approved_surah_count
     or member_passage_count < 1 then
    raise exception 'incomplete frozen catalog membership';
  end if;

  -- Every passage has EXACT contiguous, in-surah membership; a count alone
  -- is not sufficient without checking all member rows' true ayah numbers.
  if exists (
    select 1 from public.publication_catalog_passages p
    where p.catalog_id = OLD.catalog_id and (
      (select count(*) from public.publication_catalog_passage_ayahs m
        where m.catalog_id = p.catalog_id and m.passage_id = p.passage_id)
          <> p.last_ayah - p.first_ayah + 1
      or exists (
        select 1
        from public.publication_catalog_passage_ayahs m
        join public.publication_catalog_ayahs a
          on a.catalog_id = m.catalog_id and a.canonical_key = m.canonical_key
        where m.catalog_id = p.catalog_id and m.passage_id = p.passage_id
          and (a.surah_number <> p.surah_number
            or a.ayah_number < p.first_ayah or a.ayah_number > p.last_ayah)
      )
    )
  ) then
    raise exception 'noncontiguous or incorrectly linked frozen passage';
  end if;

  if exists (
    select 1 from public.publication_catalog_ayahs a
    where a.catalog_id = OLD.catalog_id and not exists (
      select 1 from public.publication_catalog_passage_ayahs m
      where m.catalog_id = a.catalog_id and m.canonical_key = a.canonical_key
    )
  ) then
    raise exception 'approved ayah has no production-authorized passage';
  end if;
  return NEW;
end;
$$;

create trigger publication_catalog_versions_guard
  before insert or update or delete on public.publication_catalog_versions
  for each row execute function public.guard_publication_catalog_version();

create or replace function public.guard_publication_catalog_member_insert()
returns trigger language plpgsql security invoker
set search_path = ''
as $$
declare
  parent_state text;
begin
  select state into parent_state
    from public.publication_catalog_versions
    where catalog_id = NEW.catalog_id
    for update;
  if parent_state is distinct from 'DRAFT' then
    raise exception 'cannot insert members into a frozen or missing catalog';
  end if;
  return NEW;
end;
$$;

create trigger publication_catalog_ayahs_insert_guard
  before insert on public.publication_catalog_ayahs
  for each row execute function public.guard_publication_catalog_member_insert();
create trigger publication_catalog_passages_insert_guard
  before insert on public.publication_catalog_passages
  for each row execute function public.guard_publication_catalog_member_insert();
create trigger publication_catalog_passage_ayahs_insert_guard
  before insert on public.publication_catalog_passage_ayahs
  for each row execute function public.guard_publication_catalog_member_insert();

create or replace function public.guard_publication_cycle_catalog()
returns trigger language plpgsql security invoker
set search_path = ''
as $$
declare
  parent_state text;
begin
  if TG_OP = 'UPDATE' and NEW.catalog_id is distinct from OLD.catalog_id then
    raise exception 'active publication cycle catalog cannot be reassigned';
  end if;
  select state into parent_state
    from public.publication_catalog_versions
    where catalog_id = NEW.catalog_id for share;
  if parent_state is distinct from 'FROZEN' then
    raise exception 'publication cycles require a FROZEN catalog';
  end if;
  return NEW;
end;
$$;

create trigger publication_cycles_frozen_catalog_guard
  before insert or update on public.publication_cycles
  for each row execute function public.guard_publication_cycle_catalog();

-- Prevent accidental mutation of frozen evidence and confirmed receipts.
-- No SECURITY DEFINER helper, no browser-exposed RPC, no automatic rollover.
create or replace function public.reject_publication_immutable_mutation()
returns trigger language plpgsql security invoker
set search_path = ''
as $$
begin
  raise exception 'immutable publication catalog / delivery evidence cannot be modified';
end;
$$;

create trigger publication_catalog_ayahs_immutable
  before update or delete on public.publication_catalog_ayahs
  for each row execute function public.reject_publication_immutable_mutation();
create trigger publication_catalog_passages_immutable
  before update or delete on public.publication_catalog_passages
  for each row execute function public.reject_publication_immutable_mutation();
create trigger publication_catalog_passage_ayahs_immutable
  before update or delete on public.publication_catalog_passage_ayahs
  for each row execute function public.reject_publication_immutable_mutation();
create trigger publication_cycle_deliveries_immutable
  before update or delete on public.publication_cycle_deliveries
  for each row execute function public.reject_publication_immutable_mutation();

-- RLS with no policies for browser roles. Service role remains a privileged
-- server-only actor; NEVER expose service_role key in the client bundle.
alter table public.publication_catalog_versions enable row level security;
alter table public.publication_catalog_ayahs enable row level security;
alter table public.publication_catalog_passages enable row level security;
alter table public.publication_catalog_passage_ayahs enable row level security;
alter table public.publication_cycles enable row level security;
alter table public.publication_cycle_attempts enable row level security;
alter table public.publication_cycle_ayah_claims enable row level security;
alter table public.publication_cycle_deliveries enable row level security;

revoke all on public.publication_catalog_versions,
  public.publication_catalog_ayahs,
  public.publication_catalog_passages,
  public.publication_catalog_passage_ayahs,
  public.publication_cycles,
  public.publication_cycle_attempts,
  public.publication_cycle_ayah_claims,
  public.publication_cycle_deliveries
from public, anon, authenticated;

revoke all on function public.reject_publication_immutable_mutation()
from public, anon, authenticated;
revoke all on function public.guard_publication_catalog_version()
from public, anon, authenticated;
revoke all on function public.guard_publication_catalog_member_insert()
from public, anon, authenticated;
revoke all on function public.guard_publication_cycle_catalog()
from public, anon, authenticated;

grant select, insert on public.publication_catalog_versions,
  public.publication_catalog_ayahs,
  public.publication_catalog_passages,
  public.publication_catalog_passage_ayahs,
  public.publication_cycle_deliveries
to service_role;

grant select, update on public.publication_catalog_versions to service_role;
grant select, insert, update on public.publication_cycles,
  public.publication_cycle_attempts,
  public.publication_cycle_ayah_claims
to service_role;

grant delete on public.publication_cycle_ayah_claims to service_role;
grant usage, select on sequence public.publication_cycle_deliveries_delivery_id_seq to service_role;

-- ROLLOUT GATES — INTENTIONALLY NOT PROVIDED IN THIS SCHEMA-ONLY PROPOSAL:
-- * Trusted one-time freeze validator must authenticate Quran source, exact
--   policy/RPC authorization, passage membership and actual manifest SHA-256.
-- * Transactional service-role reservation/confirm/release/rollover RPCs must
--   use a cycle-row FOR UPDATE lock and re-verify live run + platform evidence.
-- * A cycle is completed only when ALL distinct approved ayahs have confirmed
--   primary FACEBOOK delivery and no reservations/UNKNOWN outcomes remain.
-- * Never backfill an old generation/upload coverage cycle as a posted cycle.
-- * SQL must be parsed and executed in a disposable scratch Postgres before
--   any approved migration; this proposal has NOT been executed anywhere.
