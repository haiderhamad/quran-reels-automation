-- Fresh owner-requested publication epoch for Calm Quran Reels v1.
-- Existing run_records, vetted media, Buffer claims and historic coverage
-- are PRESERVED. This is a new zero-based publishing ledger, not a deletion.
-- Service role only; no client SELECT/INSERT/UPDATE/DELETE.
create table public.calm_reels_v1_cycle_state (
  singleton boolean primary key default true check (singleton),
  catalog_id text not null
    check (catalog_id = 'calm-reels-editorial-shortlist-v1'),
  catalog_sha256 text not null
    check (catalog_sha256 = 'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d'),
  total_ayahs integer not null check (total_ayahs = 2211),
  cycle_number integer not null default 1 check (cycle_number >= 1),
  started_at timestamptz not null default now()
);
create table public.calm_reels_v1_confirmed_posts (
  cycle_number integer not null check (cycle_number >= 1),
  ayah_key text not null check (ayah_key ~ '^[1-9][0-9]*:[1-9][0-9]*$'),
  run_id text not null references public.run_records(run_id) on delete restrict,
  media_key text not null check (length(media_key) > 0),
  platform text not null check (platform = 'facebook'),
  external_post_id text not null check (length(external_post_id) > 0),
  evidence_sha256 text not null check (evidence_sha256 ~ '^[a-f0-9]{64}$'),
  verified_at timestamptz not null,
  recorded_at timestamptz not null default now(),
  primary key (cycle_number, ayah_key, platform),
  unique (platform, external_post_id)
);
-- A published result is immutable audit evidence; never reset by deleting it.
create function public.calm_reels_v1_reject_post_mutations()
returns trigger language plpgsql security invoker
set search_path = ''
as $$
begin
  raise exception 'Confirmed publication evidence is immutable';
end;
$$;
create trigger calm_reels_v1_confirmed_post_immutable
before update or delete on public.calm_reels_v1_confirmed_posts
for each row execute function public.calm_reels_v1_reject_post_mutations();

-- Only the server role can read or record trusted platform evidence.
alter table public.calm_reels_v1_cycle_state enable row level security;
alter table public.calm_reels_v1_confirmed_posts enable row level security;
revoke all on public.calm_reels_v1_cycle_state,
 public.calm_reels_v1_confirmed_posts from public, anon, authenticated;
revoke all on function public.calm_reels_v1_reject_post_mutations()
 from public, anon, authenticated;
grant select, insert, update on public.calm_reels_v1_cycle_state to service_role;
grant select, insert on public.calm_reels_v1_confirmed_posts to service_role;

-- One initial cycle, no old-platform publication imported. This is a
-- fresh-start ZERO based on the verified frozen repository JSON.
insert into public.calm_reels_v1_cycle_state
  (singleton, catalog_id, catalog_sha256, total_ayahs, cycle_number)
values (true, 'calm-reels-editorial-shortlist-v1',
  'df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d',
  2211, 1);
