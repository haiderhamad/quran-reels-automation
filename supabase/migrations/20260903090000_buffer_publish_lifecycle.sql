-- Crash-safe Buffer write lifecycles.  Public posts and temporary validation
-- drafts are intentionally separate identities and state machines.

create table if not exists public.buffer_publication_claims (
  run_id text not null references public.run_records(run_id) on delete restrict,
  platform text not null check (platform in ('facebook', 'instagram')),
  publisher_backend text not null check (publisher_backend = 'buffer'),
  channel_id text not null,
  publication_fingerprint text not null,
  state text not null check (state in ('DISPATCHING', 'SUCCESS', 'FAILED', 'UNKNOWN_OUTCOME')),
  dispatch_attempts integer not null default 1 check (dispatch_attempts between 1 and 2),
  buffer_post_id text,
  buffer_post_status text,
  external_link text,
  accepted_at timestamptz,
  last_error_code text,
  value jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (run_id, platform, publisher_backend, channel_id)
);
create index if not exists buffer_publication_claims_reconcile_idx
  on public.buffer_publication_claims (state, channel_id, updated_at desc);

create table if not exists public.buffer_draft_validations (
  validation_id text primary key,
  run_id text not null references public.run_records(run_id) on delete restrict,
  channel_id text not null,
  buffer_source text not null unique,
  state text not null check (state in ('CREATING', 'CREATED', 'CLEANUP_PENDING', 'DELETED')),
  draft_post_id text,
  last_error_code text,
  value jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists buffer_draft_validations_cleanup_idx
  on public.buffer_draft_validations (state, updated_at asc)
  where state in ('CREATED', 'CLEANUP_PENDING', 'CREATING');

alter table public.buffer_publication_claims enable row level security;
alter table public.buffer_draft_validations enable row level security;
-- No anon/authenticated policies.  The service role used by the worker bypasses
-- RLS; neither Buffer tokens nor lifecycle state are browser-accessible.
