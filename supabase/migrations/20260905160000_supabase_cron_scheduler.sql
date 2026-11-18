-- Migration: Phase 8I — Supabase pg_cron Primary Scheduler Heartbeat
-- Sets up pg_cron, pg_net, and vault integration to invoke Vercel
-- /api/cron/publishing-scheduler every 10 minutes (* /10 * * * *).

-- 1. Ensure required extensions exist
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;
create extension if not exists supabase_vault with schema vault;

-- 2. Update source check constraint on scheduler_heartbeats table if exists
do $$
begin
  if exists (
    select 1 from pg_constraint where conname = 'scheduler_heartbeats_source_check'
  ) then
    alter table public.scheduler_heartbeats drop constraint scheduler_heartbeats_source_check;
    alter table public.scheduler_heartbeats add constraint scheduler_heartbeats_source_check
      check (source in ('supabase_cron', 'vercel_cron', 'github_cron', 'dashboard_save'));
  end if;
end $$;

-- 3. Dedicated Invocation Function
create or replace function public.invoke_publishing_scheduler()
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  cron_token text;
  scheduler_origin text;
  request_id bigint;
begin
  -- Securely retrieve bearer secret from Supabase Vault
  select decrypted_secret into cron_token
  from vault.decrypted_secrets
  where name = 'cron_secret'
  limit 1;

  if cron_token is null or length(cron_token) = 0 then
    raise exception 'CRON_SECRET not configured in vault.decrypted_secrets';
  end if;

  select decrypted_secret into scheduler_origin
  from vault.decrypted_secrets where name = 'scheduler_origin';
  if scheduler_origin is null or scheduler_origin !~ '^https://[a-zA-Z0-9.-]+$' then
    raise exception 'Configure the installation-owned HTTPS scheduler_origin in Vault';
  end if;

  -- Asynchronously invoke Vercel Cron endpoint via pg_net
  select net.http_get(
    url := scheduler_origin || '/api/cron/publishing-scheduler',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || cron_token,
      'User-Agent', 'Supabase-pg_cron/1.0'
    ),
    timeout_milliseconds := 15000
  ) into request_id;

  return request_id;
end;
$$;

-- Restrict execution permissions: only postgres can invoke this function
revoke all on function public.invoke_publishing_scheduler() from public, anon, authenticated, service_role;
grant execute on function public.invoke_publishing_scheduler() to postgres;
