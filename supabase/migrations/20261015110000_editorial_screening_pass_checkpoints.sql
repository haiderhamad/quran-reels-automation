-- Preserve successful independent passes across provider failures and restarts.
create table if not exists public.editorial_screening_pass_checkpoints (
  policy_version text not null check (policy_version = 'calm-inviting-v1'),
  assessment_version text not null check (assessment_version = 'calm-inviting-screening-v2'),
  surah_number integer not null check (surah_number between 1 and 114),
  ayah_number integer not null check (ayah_number between 1 and 286),
  pass_number integer not null check (pass_number in (1, 2)),
  source_snapshot_sha256 text not null check (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  evaluator_version text not null check (length(evaluator_version) > 0),
  instruction_sha256 text not null check (instruction_sha256 ~ '^[0-9a-f]{64}$'),
  prompt_sha256 text not null check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  assessment jsonb not null check (jsonb_typeof(assessment) = 'object'),
  check (assessment ?& array['pass_number', 'semantic_assessment', 'evaluator_version', 'instruction_sha256', 'prompt_sha256', 'assessment_rationale', 'context_notes']),
  check ((assessment->>'pass_number')::integer = pass_number
    and assessment->>'evaluator_version' = evaluator_version
    and assessment->>'instruction_sha256' = instruction_sha256
    and assessment->>'prompt_sha256' = prompt_sha256
    and assessment->>'semantic_assessment' in ('ELIGIBLE_CANDIDATE', 'INELIGIBLE_CANDIDATE', 'AMBIGUOUS_REVIEW_REQUIRED')),
  created_at timestamptz not null default now(),
  foreign key (policy_version, surah_number, ayah_number)
    references public.editorial_ayah_reviews (policy_version, surah_number, ayah_number),
  primary key (policy_version, assessment_version, surah_number, ayah_number,
    pass_number, source_snapshot_sha256, evaluator_version, instruction_sha256, prompt_sha256)
);
alter table public.editorial_screening_pass_checkpoints enable row level security;
revoke all on public.editorial_screening_pass_checkpoints from public, anon, authenticated;
grant select, insert on public.editorial_screening_pass_checkpoints to service_role;

create or replace function public.preserve_editorial_pass_checkpoint()
returns trigger language plpgsql set search_path = pg_catalog, public as $$
begin
  raise exception 'editorial screening pass checkpoints are immutable';
end;
$$;
revoke all on function public.preserve_editorial_pass_checkpoint() from public, anon, authenticated;
drop trigger if exists preserve_editorial_pass_checkpoint on public.editorial_screening_pass_checkpoints;
create trigger preserve_editorial_pass_checkpoint
before update or delete on public.editorial_screening_pass_checkpoints
for each row execute function public.preserve_editorial_pass_checkpoint();

-- Lease validation, evidence ingestion, and lease removal share one transaction.
-- Existing completed batches use the original RPC's payload-hash replay check.
create or replace function public.ingest_editorial_screening_claimed_batch(
  p_run_id text, p_batch_id text, p_rows jsonb, p_worker_id text, p_lease_tokens jsonb
) returns boolean language plpgsql set search_path = pg_catalog, public as $$
declare
  v_row jsonb;
  v_lease public.editorial_screening_leases%rowtype;
  v_key text;
  v_seen text[] := '{}';
begin
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) < 1
     or jsonb_array_length(p_rows) > 50 or jsonb_typeof(p_lease_tokens) is distinct from 'object' then
    raise exception 'invalid claimed screening batch';
  end if;
  if exists (select 1 from public.editorial_screening_batches b
             where b.run_id = p_run_id and b.batch_id = p_batch_id) then
    perform public.ingest_editorial_screening_batch(p_run_id, p_batch_id, p_rows);
    return true;
  end if;
  for v_row in select value from jsonb_array_elements(p_rows)
    order by (value->>'surah_number')::integer, (value->>'ayah_number')::integer
  loop
    v_key := (v_row->>'surah_number') || ':' || (v_row->>'ayah_number');
    if v_key = any(v_seen) then
      raise exception 'duplicate claimed screening identity';
    end if;
    v_seen := array_append(v_seen, v_key);
    select * into v_lease from public.editorial_screening_leases
      where policy_version = 'calm-inviting-v1'
        and surah_number = (v_row->>'surah_number')::integer
        and ayah_number = (v_row->>'ayah_number')::integer for update;
    if not found or v_lease.worker_id is distinct from p_worker_id
       or v_lease.run_id is distinct from p_run_id
       or v_lease.lease_token::text is distinct from (p_lease_tokens->>v_key)
       or v_lease.lease_expires_at <= clock_timestamp() then
      raise exception 'missing, expired, or foreign editorial screening lease: %', v_key;
    end if;
  end loop;
  if (select count(*) from jsonb_object_keys(p_lease_tokens)) <> cardinality(v_seen) then
    raise exception 'lease token identities do not match screening rows';
  end if;
  perform public.ingest_editorial_screening_batch(p_run_id, p_batch_id, p_rows);
  delete from public.editorial_screening_leases
    where policy_version = 'calm-inviting-v1' and worker_id = p_worker_id and run_id = p_run_id
      and (surah_number::text || ':' || ayah_number::text) = any(v_seen);
  return true;
end;
$$;
revoke all on function public.ingest_editorial_screening_claimed_batch(text, text, jsonb, text, jsonb) from public, anon, authenticated;
grant execute on function public.ingest_editorial_screening_claimed_batch(text, text, jsonb, text, jsonb) to service_role;
