-- Authoritative application state for Quran Reels. B2 remains a media-only
-- store after the explicitly approved B2-to-Supabase migration.
-- No browser role receives privileges on these tables.

create extension if not exists pgcrypto;

create table if not exists public.app_config (
  singleton boolean primary key default true check (singleton),
  schema_version integer not null,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.coverage_state (
  singleton boolean primary key default true check (singleton),
  schema_version integer not null,
  cycle_number integer not null check (cycle_number >= 1),
  total_ayahs integer not null check (total_ayahs = 6236),
  covered_count integer not null check (covered_count between 0 and 6236),
  value jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.coverage_cycles (
  cycle_number integer primary key check (cycle_number >= 1),
  value jsonb not null,
  completed_at timestamptz not null
);

create table if not exists public.run_records (
  run_id text primary key,
  run_kind text not null check (run_kind in ('scheduled', 'manual_production', 'test')),
  berlin_date date not null,
  schedule_slot_id text,
  status text not null check (status in ('STARTED', 'SUCCESS', 'FAILED')),
  completed_at timestamptz,
  final_safety_passed boolean,
  media_key text,
  value jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (berlin_date, schedule_slot_id)
);
create index if not exists run_records_recent_idx on public.run_records (created_at desc);
create index if not exists run_records_archive_idx on public.run_records (run_kind, status, completed_at) where media_key is not null;

create table if not exists public.scheduler_claims (
  berlin_date date not null,
  slot_id text not null,
  run_id text not null,
  status text not null check (status in ('CLAIMED', 'DISPATCHED', 'FAILED')),
  value jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (berlin_date, slot_id)
);

create table if not exists public.background_assets (
  provider text not null check (provider in ('pexels', 'pixabay')),
  provider_asset_id text not null,
  provider_variant_id text not null,
  policy_fingerprint text not null,
  content_sha256 text not null,
  status text not null check (status in ('verified', 'stale')),
  verified_at timestamptz,
  value jsonb not null,
  primary key (provider, provider_asset_id, provider_variant_id, policy_fingerprint, content_sha256)
);
create index if not exists background_assets_current_idx on public.background_assets (policy_fingerprint, status, verified_at);

create table if not exists public.background_events (
  event_id text primary key,
  event_type text not null check (event_type in ('safety_rejected', 'metadata_ineligible', 'transient_failure', 'stale', 'usage')),
  provider text not null check (provider in ('pexels', 'pixabay')),
  provider_asset_id text not null,
  provider_variant_id text not null,
  fingerprint text,
  occurred_at timestamptz not null,
  value jsonb not null
);
create index if not exists background_events_identity_idx on public.background_events (event_type, provider, provider_asset_id, provider_variant_id);

create table if not exists public.background_builder_runs (
  builder_run_id text primary key,
  status text not null,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
create table if not exists public.background_query_metrics (
  builder_run_id text not null references public.background_builder_runs(builder_run_id) on delete cascade,
  provider text not null check (provider in ('pexels', 'pixabay')),
  query text not null,
  value jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (builder_run_id, provider, query)
);

create table if not exists public.meta_credentials (
  singleton boolean primary key default true check (singleton),
  encrypted_envelope text not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.archive_jobs (
  archive_job_id uuid primary key default gen_random_uuid(),
  archive_month date not null check (extract(day from archive_month) = 1),
  archive_kind text not null check (archive_kind = 'production'),
  status text not null check (status in ('PENDING', 'BUILDING', 'UPLOADING', 'VERIFYING', 'VERIFIED', 'CLEANUP_PENDING', 'COMPLETE', 'FAILED')),
  archive_path text,
  archive_sha256 text,
  archive_size bigint,
  value jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (archive_month, archive_kind)
);
create table if not exists public.archive_items (
  archive_job_id uuid not null references public.archive_jobs(archive_job_id) on delete cascade,
  run_id text not null references public.run_records(run_id),
  media_key text not null,
  media_size bigint,
  media_sha256 text,
  archived_at timestamptz,
  b2_deleted_at timestamptz,
  primary key (archive_job_id, run_id)
);
create index if not exists archive_items_cleanup_idx on public.archive_items (b2_deleted_at) where b2_deleted_at is null;

create table if not exists public.storage_migrations (
  migration_id text primary key,
  source text not null,
  status text not null check (status in ('DRY_RUN', 'MIGRATED', 'VERIFIED', 'FAILED')),
  report jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.app_config enable row level security;
alter table public.coverage_state enable row level security;
alter table public.coverage_cycles enable row level security;
alter table public.run_records enable row level security;
alter table public.scheduler_claims enable row level security;
alter table public.background_assets enable row level security;
alter table public.background_events enable row level security;
alter table public.background_builder_runs enable row level security;
alter table public.background_query_metrics enable row level security;
alter table public.meta_credentials enable row level security;
alter table public.archive_jobs enable row level security;
alter table public.archive_items enable row level security;
alter table public.storage_migrations enable row level security;

-- Deliberately no anon/authenticated policies. Server-only secret/service-role
-- credentials bypass RLS; browser code has no database credentials or routes.
