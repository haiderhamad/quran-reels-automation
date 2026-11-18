-- Migration: Scheduler Heartbeats
-- Durable observability for scheduler heartbeats triggered by Vercel Cron,
-- GitHub Actions scheduler workflow, and Dashboard settings saves.

create table if not exists public.scheduler_heartbeats (
  heartbeat_id text primary key,
  source text not null check (source in ('supabase_cron', 'vercel_cron', 'github_cron', 'dashboard_save')),
  occurred_at timestamptz not null default now(),
  due_slot_count integer not null default 0,
  claim_count integer not null default 0,
  dispatch_count integer not null default 0,
  result jsonb not null default '{}'::jsonb
);

create index if not exists scheduler_heartbeats_occurred_at_idx
  on public.scheduler_heartbeats (occurred_at desc);
