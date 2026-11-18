create table public.editorial_canonical_ayahs (
  surah_number integer not null check (surah_number between 1 and 114),
  ayah_number integer not null check (ayah_number between 1 and 286),
  primary key (surah_number, ayah_number)
);
with surah_counts(surah_number, ayah_count) as (
 values (1,7),(2,286),(3,200),(4,176),(5,120),(6,165),(7,206),(8,75),(9,129),(10,109),(11,123),(12,111),(13,43),(14,52),(15,99),(16,128),(17,111),(18,110),(19,98),(20,135),(21,112),(22,78),(23,118),(24,64),(25,77),(26,227),(27,93),(28,88),(29,69),(30,60),(31,34),(32,30),(33,73),(34,54),(35,45),(36,83),(37,182),(38,88),(39,75),(40,85),(41,54),(42,53),(43,89),(44,59),(45,37),(46,35),(47,38),(48,29),(49,18),(50,45),(51,60),(52,49),(53,62),(54,55),(55,78),(56,96),(57,29),(58,22),(59,24),(60,13),(61,14),(62,11),(63,11),(64,18),(65,12),(66,12),(67,30),(68,52),(69,52),(70,44),(71,28),(72,28),(73,20),(74,56),(75,40),(76,31),(77,50),(78,40),(79,46),(80,42),(81,29),(82,19),(83,36),(84,25),(85,22),(86,17),(87,19),(88,26),(89,30),(90,20),(91,15),(92,21),(93,11),(94,8),(95,8),(96,19),(97,5),(98,8),(99,8),(100,11),(101,11),(102,8),(103,3),(104,9),(105,5),(106,4),(107,7),(108,3),(109,6),(110,3),(111,5),(112,4),(113,5),(114,6)
)
insert into public.editorial_canonical_ayahs
select s.surah_number, n.ayah_number from surah_counts s
cross join lateral generate_series(1,s.ayah_count) as n(ayah_number);

alter table public.editorial_ayah_reviews
 add constraint editorial_reviews_canonical_fkey foreign key (surah_number,ayah_number)
 references public.editorial_canonical_ayahs(surah_number,ayah_number);
alter table public.editorial_ayah_reviews
 add column screening_status text not null default 'NOT_SCREENED'
   check (screening_status in ('NOT_SCREENED','SCREENING','SCREENED','SOURCE_BLOCKED','SCREENING_FAILED')),
 add column semantic_assessment text
   check (semantic_assessment in ('ELIGIBLE_CANDIDATE','INELIGIBLE_CANDIDATE','AMBIGUOUS_REVIEW_REQUIRED')),
 add column screening_theme text,
 add column assessment_rationale text,
 add column context_notes text,
 add column source_hash text check (source_hash ~ '^[0-9a-f]{64}$'),
 add column translation_hash text check (translation_hash ~ '^[0-9a-f]{64}$'),
 add column assessment_version text,
 add column evaluator_version text,
 add column screening_run_id text,
 add column screening_failure_reason text,
 add column evidence_fingerprint text check (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
 add column screened_at timestamptz,
 add constraint editorial_screened_evidence_check check (
   (screening_status = 'SCREENED' and semantic_assessment is not null
    and source_hash is not null and translation_hash is not null
    and assessment_version is not null and evaluator_version is not null
    and evidence_fingerprint is not null and screened_at is not null)
   or (screening_status <> 'SCREENED' and semantic_assessment is null)
 );

-- Server-only, immutable source snapshots. Exact source text is kept out of Git.
create table public.editorial_source_evidence (
 policy_version text not null,
 surah_number integer not null,
 ayah_number integer not null,
 source_hash text not null check (source_hash ~ '^[0-9a-f]{64}$'),
 translation_hash text not null check (translation_hash ~ '^[0-9a-f]{64}$'),
 footnotes_hash text not null check (footnotes_hash ~ '^[0-9a-f]{64}$'),
 arabic_text text not null,
 translation_text text not null,
 footnotes text not null default '',
 arabic_source_provider text not null,
 arabic_source_version text not null,
 translation_provider text not null,
 translation_version text not null,
 translation_language text not null,
 retrieved_at timestamptz not null,
 evidence_fingerprint text not null check (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
 recorded_at timestamptz not null default now(),
 primary key (policy_version,surah_number,ayah_number,source_hash,translation_hash,footnotes_hash),
 foreign key (surah_number,ayah_number) references public.editorial_canonical_ayahs,
 foreign key (policy_version,surah_number,ayah_number)
   references public.editorial_ayah_reviews(policy_version,surah_number,ayah_number)
);

create table public.editorial_screening_runs (
 run_id text primary key,
 policy_version text not null,
 assessment_version text not null,
 evaluator_version text not null,
 arabic_source_version text not null,
 translation_version text not null,
 translation_language text not null,
 source_snapshot_sha256 text not null check (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
 evaluator_instruction_sha256 text not null check (evaluator_instruction_sha256 ~ '^[0-9a-f]{64}$'),
 status text not null check (status in ('STARTED','RUNNING','PAUSED','COMPLETED','FAILED')),
 canonical_total integer not null check (canonical_total = 6236),
 started_at timestamptz not null default now(),
 completed_at timestamptz,
 updated_at timestamptz not null default now()
);
create index editorial_screening_runs_recent_idx
 on public.editorial_screening_runs(policy_version,started_at desc);
alter table public.editorial_ayah_reviews
 add constraint editorial_review_screening_run_fkey foreign key (screening_run_id)
 references public.editorial_screening_runs(run_id);

create table public.editorial_screening_batches (
 run_id text not null references public.editorial_screening_runs(run_id),
 batch_id text not null,
 payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
 item_count integer not null check (item_count between 1 and 50),
 status text not null check (status = 'COMPLETED'),
 completed_at timestamptz not null default now(),
 primary key (run_id,batch_id)
);

-- These two-pass records are append-only and cannot be changed by the worker.
create table public.editorial_ayah_assessments (
 run_id text not null references public.editorial_screening_runs(run_id),
 policy_version text not null,
 surah_number integer not null,
 ayah_number integer not null,
 pass_number smallint not null check (pass_number in (1,2)),
 semantic_assessment text not null
  check (semantic_assessment in ('ELIGIBLE_CANDIDATE','INELIGIBLE_CANDIDATE','AMBIGUOUS_REVIEW_REQUIRED')),
 candidate_theme text not null,
 assessment_rationale text not null,
 context_notes text not null,
 source_hash text not null,
 translation_hash text not null,
 footnotes_hash text not null,
 evaluator_version text not null,
 evidence jsonb not null,
 evidence_fingerprint text not null check (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
 created_at timestamptz not null default now(),
 primary key (run_id,policy_version,surah_number,ayah_number,pass_number),
 foreign key (surah_number,ayah_number) references public.editorial_canonical_ayahs,
 foreign key (policy_version,surah_number,ayah_number,source_hash,translation_hash,footnotes_hash)
  references public.editorial_source_evidence(policy_version,surah_number,ayah_number,source_hash,translation_hash,footnotes_hash)
);
create index editorial_ayah_assessments_key_idx
 on public.editorial_ayah_assessments(policy_version,surah_number,ayah_number);

create function public.editorial_validate_screening_run_update()
returns trigger language plpgsql security invoker as $$
declare
 v_screened integer;
 v_passes integer;
begin
 if tg_op='INSERT' then
  if new.status='COMPLETED' or new.completed_at is not null then
   raise exception 'Screening run must start incomplete';
  end if;
  return new;
 end if;
 if (old.run_id,old.policy_version,old.assessment_version,old.evaluator_version,
     old.arabic_source_version,old.translation_version,old.translation_language,
     old.source_snapshot_sha256,old.evaluator_instruction_sha256,old.canonical_total,
     old.started_at)
    is distinct from
    (new.run_id,new.policy_version,new.assessment_version,new.evaluator_version,
     new.arabic_source_version,new.translation_version,new.translation_language,
     new.source_snapshot_sha256,new.evaluator_instruction_sha256,new.canonical_total,
     new.started_at) then
  raise exception 'Screening run provenance is immutable';
 end if;
 if old.status='COMPLETED' and new.status<>'COMPLETED' then
  raise exception 'Completed screening run cannot be reopened';
 end if;
 if old.status='COMPLETED' and new.completed_at is distinct from old.completed_at then
  raise exception 'Completed screening timestamp is immutable';
 end if;
 if (new.status='COMPLETED') <> (new.completed_at is not null) then
  raise exception 'Completion status and completed_at must agree';
 end if;
 if new.status='COMPLETED' and old.status<>'COMPLETED' then
  select count(*) into v_screened from public.editorial_ayah_reviews
   where policy_version=new.policy_version and screening_run_id=new.run_id
    and screening_status='SCREENED' and semantic_assessment is not null
    and source_hash is not null and translation_hash is not null
    and evidence_fingerprint is not null;
  if v_screened<>6236 then
   raise exception 'Cannot complete screening run with % of 6236 screened ayahs',v_screened;
  end if;
  select count(*) into v_passes from public.editorial_ayah_assessments
   where run_id=new.run_id and policy_version=new.policy_version;
  if v_passes<>12472 then
   raise exception 'Cannot complete screening run with % of 12472 pass records',v_passes;
  end if;
  if exists (
   select 1 from public.editorial_ayah_reviews r
   where r.policy_version=new.policy_version and r.screening_run_id=new.run_id
    and r.screening_status='SCREENED'
    and (select count(*) from public.editorial_ayah_assessments a
      where a.run_id=new.run_id and a.policy_version=new.policy_version
       and a.surah_number=r.surah_number and a.ayah_number=r.ayah_number
       and a.source_hash=r.source_hash and a.translation_hash=r.translation_hash)<>2
  ) then
   raise exception 'Screened ayah lacks two source-matched independent passes';
  end if;
 end if;
 return new;
end $$;
create trigger editorial_validate_screening_run_update_trigger
before insert or update on public.editorial_screening_runs
for each row execute function public.editorial_validate_screening_run_update();

create table public.editorial_screening_audit (
 audit_id bigint generated always as identity primary key,
 policy_version text not null,
 surah_number integer not null,
 ayah_number integer not null,
 previous_state jsonb,
 new_state jsonb not null,
 reason text not null,
 actor text not null,
 evidence_fingerprint text,
 occurred_at timestamptz not null default now(),
 foreign key (surah_number,ayah_number) references public.editorial_canonical_ayahs
);
create index editorial_screening_audit_key_idx
 on public.editorial_screening_audit(policy_version,surah_number,ayah_number,occurred_at desc);

create function public.editorial_screening_audit_current()
returns trigger language plpgsql security invoker as $$
begin
 if (to_jsonb(old) - 'updated_at') is distinct from (to_jsonb(new) - 'updated_at') then
   insert into public.editorial_screening_audit
    (policy_version,surah_number,ayah_number,previous_state,new_state,reason,actor,evidence_fingerprint)
   values (new.policy_version,new.surah_number,new.ayah_number,to_jsonb(old),to_jsonb(new),
    'SCREENING_INGEST',coalesce(nullif(current_setting('app.editorial_actor',true),''),'database'),
    new.evidence_fingerprint);
 end if;
 return new;
end $$;
create trigger editorial_screening_audit_current_trigger
after update on public.editorial_ayah_reviews
for each row execute function public.editorial_screening_audit_current();

-- Corpus proposals are never approved by the machine-screening RPC.
create table public.editorial_passage_candidates (
 candidate_id uuid primary key default gen_random_uuid(),
 run_id text not null references public.editorial_screening_runs(run_id),
 policy_version text not null,
 surah_number integer not null check (surah_number between 1 and 114),
 first_ayah integer not null,
 last_ayah integer not null,
 included_keys jsonb not null,
 theme text not null,
 context_requirements text not null,
 evidence jsonb not null,
 approval_status text not null default 'PENDING_APPROVAL'
  check (approval_status in ('PENDING_APPROVAL','APPROVED','EXCLUDED')),
 created_at timestamptz not null default now(),
 check (first_ayah > 0 and last_ayah >= first_ayah and last_ayah <= 286),
 foreign key (surah_number,first_ayah) references public.editorial_canonical_ayahs,
 foreign key (surah_number,last_ayah) references public.editorial_canonical_ayahs
);
create function public.editorial_candidate_insert_guard()
returns trigger language plpgsql security invoker as $$
begin
 if new.approval_status <> 'PENDING_APPROVAL' then
  raise exception 'Automated candidate inserts cannot grant editorial approval';
 end if;
 return new;
end $$;
create trigger editorial_candidate_insert_guard_trigger
before insert on public.editorial_passage_candidates
for each row execute function public.editorial_candidate_insert_guard();

-- A release stores the exact JSON manifest and its raw-file digest. Legacy
-- approval provenance is recorded honestly; no owner/time is fabricated.
create table public.editorial_catalog_releases (
 release_id text primary key,
 policy_version text not null,
 manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
 manifest jsonb not null,
 range_count integer not null check (range_count > 0),
 distinct_ayah_count integer not null check (distinct_ayah_count between 1 and 6236),
 approval_kind text not null check (approval_kind in ('LEGACY_CATALOG','OWNER_APPROVED')),
 approval_provenance text not null,
 approved_by text,
 approved_at timestamptz,
 recorded_at timestamptz not null default now(),
 check ((approval_kind = 'LEGACY_CATALOG' and approved_by is null and approved_at is null)
     or (approval_kind = 'OWNER_APPROVED' and nullif(btrim(approved_by),'') is not null
       and approved_at is not null)),
 check (nullif(btrim(approval_provenance),'') is not null),
 check (approval_kind <> 'LEGACY_CATALOG' or
   (release_id = 'calm-inviting-v1:legacy-75'
    and policy_version = 'calm-inviting-v1'
    and manifest_sha256 = '416751440ed0be40840bd543e6f86ce3a2baf4cceac8716bc986804fa0db2919'
    and range_count = 33 and distinct_ayah_count = 75))
);
create table public.editorial_active_catalog (
 policy_version text primary key,
 release_id text not null unique references public.editorial_catalog_releases(release_id),
 activated_at timestamptz not null default now(),
 activation_reason text not null
);
create table public.editorial_catalog_activations (
 activation_id bigint generated always as identity primary key,
 policy_version text not null,
 previous_release_id text,
 new_release_id text not null references public.editorial_catalog_releases(release_id),
 activated_at timestamptz not null,
 actor text not null,
 reason text not null
);
create function public.editorial_record_catalog_activation()
returns trigger language plpgsql security invoker as $$
begin
 if exists (select 1 from public.editorial_catalog_releases
  where release_id=new.release_id and policy_version<>new.policy_version) then
  raise exception 'Catalog release policy mismatch';
 end if;
 insert into public.editorial_catalog_activations
  (policy_version,previous_release_id,new_release_id,activated_at,actor,reason)
 values (new.policy_version,case when tg_op='UPDATE' then old.release_id else null end,
  new.release_id,new.activated_at,current_user,new.activation_reason);
 return new;
end $$;
create trigger editorial_catalog_activation_trigger
after insert or update on public.editorial_active_catalog
for each row execute function public.editorial_record_catalog_activation();
create function public.editorial_prevent_immutable_change()
returns trigger language plpgsql security invoker as $$
begin
 raise exception 'Editorial evidence is append-only';
end $$;
create trigger editorial_source_immutable
before update or delete on public.editorial_source_evidence
for each row execute function public.editorial_prevent_immutable_change();
create trigger editorial_pass_immutable
before update or delete on public.editorial_ayah_assessments
for each row execute function public.editorial_prevent_immutable_change();
create trigger editorial_audit_immutable
before update or delete on public.editorial_screening_audit
for each row execute function public.editorial_prevent_immutable_change();
create trigger editorial_catalog_release_immutable
before update or delete on public.editorial_catalog_releases
for each row execute function public.editorial_prevent_immutable_change();
create trigger editorial_catalog_activation_immutable
before update or delete on public.editorial_catalog_activations
for each row execute function public.editorial_prevent_immutable_change();

-- Protect final human decisions: the screening worker may only update the
-- named screening columns; it cannot INSERT or UPDATE review_status.
revoke insert, update, delete on public.editorial_ayah_reviews from service_role;
grant update (screening_status,semantic_assessment,screening_theme,assessment_rationale,
 context_notes,source_hash,translation_hash,assessment_version,evaluator_version,
 screening_run_id,screening_failure_reason,evidence_fingerprint,screened_at,updated_at)
 on public.editorial_ayah_reviews to service_role;

alter table public.editorial_canonical_ayahs enable row level security;
alter table public.editorial_source_evidence enable row level security;
alter table public.editorial_screening_runs enable row level security;
alter table public.editorial_screening_batches enable row level security;
alter table public.editorial_ayah_assessments enable row level security;
alter table public.editorial_screening_audit enable row level security;
alter table public.editorial_passage_candidates enable row level security;
alter table public.editorial_catalog_releases enable row level security;
alter table public.editorial_active_catalog enable row level security;
alter table public.editorial_catalog_activations enable row level security;
revoke all on public.editorial_canonical_ayahs,public.editorial_source_evidence,
 public.editorial_screening_runs,public.editorial_screening_batches,
 public.editorial_ayah_assessments,public.editorial_screening_audit,
 public.editorial_passage_candidates,public.editorial_catalog_releases,
 public.editorial_active_catalog,public.editorial_catalog_activations
 from public,anon,authenticated,service_role;
revoke all on sequence public.editorial_screening_audit_audit_id_seq,
 public.editorial_catalog_activations_activation_id_seq
 from public,anon,authenticated,service_role;
grant select on public.editorial_canonical_ayahs,public.editorial_source_evidence,
 public.editorial_screening_runs,public.editorial_screening_batches,
 public.editorial_ayah_assessments,public.editorial_screening_audit,
 public.editorial_passage_candidates,public.editorial_catalog_releases,
 public.editorial_active_catalog,public.editorial_catalog_activations to service_role;
grant insert,update on public.editorial_screening_runs to service_role;
grant insert on public.editorial_screening_batches,public.editorial_source_evidence,
 public.editorial_ayah_assessments,public.editorial_screening_audit,
 public.editorial_passage_candidates to service_role;
grant usage,select on sequence public.editorial_screening_audit_audit_id_seq to service_role;

create view public.editorial_screening_summary with (security_invoker = true) as
select policy_version,
 count(*)::integer canonical_ayahs,
 count(*) filter (where source_hash is not null and translation_hash is not null)::integer source_verified_ayahs,
 count(*) filter (where screening_status = 'SCREENED')::integer screened_ayahs,
 count(*) filter (where screening_status = 'NOT_SCREENED')::integer not_screened_ayahs,
 count(*) filter (where screening_status = 'SCREENING')::integer screening_ayahs,
 count(*) filter (where screening_status = 'SOURCE_BLOCKED')::integer source_blocked_ayahs,
 count(*) filter (where screening_status = 'SCREENING_FAILED')::integer screening_failed_ayahs,
 count(*) filter (where screening_status = 'SCREENED' and semantic_assessment = 'ELIGIBLE_CANDIDATE')::integer eligible_candidate_ayahs,
 count(*) filter (where screening_status = 'SCREENED' and semantic_assessment = 'INELIGIBLE_CANDIDATE')::integer ineligible_candidate_ayahs,
 count(*) filter (where screening_status = 'SCREENED' and semantic_assessment = 'AMBIGUOUS_REVIEW_REQUIRED')::integer ambiguous_ayahs,
 max(screened_at) last_screened_at,
 case when count(distinct assessment_version) filter (where screening_status='SCREENED') = 1
  then max(assessment_version) filter (where screening_status='SCREENED') else null end assessment_version
from public.editorial_ayah_reviews group by policy_version;
revoke all on public.editorial_screening_summary from public,anon,authenticated;
grant select on public.editorial_screening_summary to service_role;

create function public.editorial_normalized_text_sha256(p_text text)
returns text language sql immutable strict security invoker as $$
 select encode(extensions.digest(convert_to(
  btrim(regexp_replace(p_text,'[[:space:]]+',' ','g')),'UTF8'),'sha256'),'hex')
$$;
revoke all on function public.editorial_normalized_text_sha256(text)
 from public,anon,authenticated;
grant execute on function public.editorial_normalized_text_sha256(text) to service_role;

-- A single RPC makes source, both independent passes, current row and batch
-- checkpoint one transaction. It is callable only by the server-side role.
create function public.ingest_editorial_screening_batch(
 p_run_id text, p_batch_id text, p_rows jsonb
) returns jsonb language plpgsql security invoker as $$
declare
 v_run public.editorial_screening_runs%rowtype;
 v_row jsonb;
 v_existing public.editorial_ayah_reviews%rowtype;
 v_source_fingerprint text;
 v_fingerprint text;
 v_payload_sha text;
 v_source_hash text;
 v_translation_hash text;
 v_footnotes_hash text;
 v_status text;
 v_semantic text;
 v_key text;
 v_seen text[] := array[]::text[];
 v_surah integer;
 v_ayah integer;
 v_pass jsonb;
 v_pass_number integer;
 v_first jsonb;
 v_second jsonb;
 v_prior_hash text;
 v_context_key text;
 v_first_bound integer;
 v_last_bound integer;
begin
 if p_run_id is null or btrim(p_run_id) = '' or p_batch_id is null or btrim(p_batch_id) = '' then
  raise exception 'Run and batch identities are required';
 end if;
 if jsonb_typeof(p_rows) is distinct from 'array' then
  raise exception 'Batch must be a JSON array';
 end if;
 if jsonb_array_length(p_rows) not between 1 and 50 then
  raise exception 'Batch must contain 1..50 ayahs';
 end if;
 select * into v_run from public.editorial_screening_runs
  where run_id=p_run_id for update;
 if not found then raise exception 'Screening run not found'; end if;
 v_payload_sha := encode(extensions.digest(convert_to(p_rows::text,'UTF8'),'sha256'),'hex');
 select payload_sha256 into v_prior_hash from public.editorial_screening_batches
  where run_id=p_run_id and batch_id=p_batch_id;
 if found then
  if v_prior_hash <> v_payload_sha then raise exception 'Batch identity reused with different payload'; end if;
  return jsonb_build_object('run_id',p_run_id,'batch_id',p_batch_id,
   'item_count',jsonb_array_length(p_rows),'payload_sha256',v_payload_sha,'replayed',true);
 end if;
 if v_run.status not in ('STARTED','RUNNING','PAUSED') then
  raise exception 'Screening run is not active';
 end if;

 for v_row in select value from jsonb_array_elements(p_rows) loop
  if jsonb_typeof(v_row) is distinct from 'object' then
   raise exception 'Batch row must be an object';
  end if;
  v_surah := (v_row->>'surah_number')::integer;
  v_ayah := (v_row->>'ayah_number')::integer;
  v_key := v_surah::text || ':' || v_ayah::text;
  if v_key = any(v_seen) then raise exception 'Duplicate ayah in batch: %',v_key; end if;
  v_seen := array_append(v_seen,v_key);
  if not exists (select 1 from public.editorial_canonical_ayahs
    where surah_number=v_surah and ayah_number=v_ayah) then
   raise exception 'Invalid canonical ayah: %',v_key;
  end if;
  select * into v_existing from public.editorial_ayah_reviews
   where policy_version=v_run.policy_version and surah_number=v_surah and ayah_number=v_ayah
   for update;
  if not found then raise exception 'Review ledger identity missing: %',v_key; end if;
  v_status := v_row->>'screening_status';
  if v_status is null or v_status not in ('SCREENED','SOURCE_BLOCKED','SCREENING_FAILED') then
   raise exception 'Ingest requires a terminal screening status for %',v_key;
  end if;
  v_source_hash := v_row->>'source_hash';
  v_translation_hash := v_row->>'translation_hash';
  v_footnotes_hash := v_row->>'footnotes_hash';
  if v_status = 'SOURCE_BLOCKED'
     and (v_source_hash is not null or v_translation_hash is not null
       or v_footnotes_hash is not null) then
   raise exception 'Source-blocked row cannot claim verified hashes for %',v_key;
  end if;
  if (v_row->>'assessment_version') is distinct from v_run.assessment_version then
   raise exception 'Assessment version differs from run for %',v_key;
  end if;
  if v_status <> 'SOURCE_BLOCKED' then
   if v_source_hash is null or v_source_hash !~ '^[0-9a-f]{64}$' or
      v_translation_hash is null or v_translation_hash !~ '^[0-9a-f]{64}$' or
      v_footnotes_hash is null or v_footnotes_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'Source hashes missing or malformed for %',v_key;
   end if;
   if nullif(v_row->>'arabic_text','') is null or
      nullif(v_row->>'translation_text','') is null or
      nullif(v_row->>'arabic_source_provider','') is null or
      nullif(v_row->>'arabic_source_version','') is null or
      nullif(v_row->>'translation_provider','') is null or
      nullif(v_row->>'translation_version','') is null or
      nullif(v_row->>'translation_language','') is null or
      nullif(v_row->>'retrieved_at','') is null then
    raise exception 'Source provenance missing for %',v_key;
   end if;
   if v_source_hash is distinct from public.editorial_normalized_text_sha256(v_row->>'arabic_text')
      or v_translation_hash is distinct from public.editorial_normalized_text_sha256(v_row->>'translation_text')
      or v_footnotes_hash is distinct from public.editorial_normalized_text_sha256(coalesce(v_row->>'footnotes','')) then
    raise exception 'Source text/hash mismatch for %',v_key;
   end if;
   if (v_row->>'arabic_source_version') is distinct from v_run.arabic_source_version
      or (v_row->>'translation_version') is distinct from v_run.translation_version
      or (v_row->>'translation_language') is distinct from v_run.translation_language then
    raise exception 'Source version differs from run checkpoint for %',v_key;
   end if;
   v_source_fingerprint := encode(extensions.digest(convert_to(
    (v_row->'arabic_text')::text || (v_row->'translation_text')::text ||
    coalesce((v_row->'footnotes')::text,'""') ||
    (v_row->'arabic_source_provider')::text || (v_row->'arabic_source_version')::text ||
    (v_row->'translation_provider')::text || (v_row->'translation_version')::text ||
    (v_row->'translation_language')::text,
    'UTF8'),'sha256'),'hex');
   insert into public.editorial_source_evidence(
    policy_version,surah_number,ayah_number,source_hash,translation_hash,footnotes_hash,
    arabic_text,translation_text,footnotes,arabic_source_provider,arabic_source_version,
    translation_provider,translation_version,translation_language,retrieved_at,evidence_fingerprint
   ) values (
    v_run.policy_version,v_surah,v_ayah,v_source_hash,v_translation_hash,v_footnotes_hash,
    v_row->>'arabic_text',v_row->>'translation_text',coalesce(v_row->>'footnotes',''),
    v_row->>'arabic_source_provider',v_row->>'arabic_source_version',
    v_row->>'translation_provider',v_row->>'translation_version',
    v_row->>'translation_language',(v_row->>'retrieved_at')::timestamptz,v_source_fingerprint
   ) on conflict do nothing;
   if not exists (select 1 from public.editorial_source_evidence
    where policy_version=v_run.policy_version and surah_number=v_surah and ayah_number=v_ayah
     and source_hash=v_source_hash and translation_hash=v_translation_hash
     and evidence_fingerprint=v_source_fingerprint and footnotes_hash=v_footnotes_hash) then
    raise exception 'Conflicting immutable source evidence for %',v_key;
   end if;
  end if;

  v_first := v_row->'pass_1';
  v_second := v_row->'pass_2';
  if v_status = 'SCREENED' then
   if jsonb_typeof(v_first) is distinct from 'object'
      or jsonb_typeof(v_second) is distinct from 'object' then
    raise exception 'Both independent assessment passes required for %',v_key;
   end if;
   for v_pass_number in 1..2 loop
    v_pass := case when v_pass_number=1 then v_first else v_second end;
    if (v_pass->>'semantic_assessment') is null or v_pass->>'semantic_assessment' not in
       ('ELIGIBLE_CANDIDATE','INELIGIBLE_CANDIDATE','AMBIGUOUS_REVIEW_REQUIRED')
       or nullif(v_pass->>'candidate_theme','') is null
       or nullif(v_pass->>'assessment_rationale','') is null
       or nullif(v_pass->>'context_notes','') is null
       or nullif(v_pass->>'evaluator_version','') is null
       or jsonb_typeof(v_pass->'pass_number') is distinct from 'number'
       or (v_pass->>'instruction_sha256') is distinct from v_run.evaluator_instruction_sha256
       or (v_pass->>'prompt_sha256') is null
       or (v_pass->>'prompt_sha256') !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(v_pass->'context_keys') is distinct from 'array'
       or jsonb_array_length(v_pass->'context_keys') = 0 then
     raise exception 'Incomplete pass % for %',v_pass_number,v_key;
    end if;
    if (v_pass->>'pass_number')::integer <> v_pass_number then
     raise exception 'Independent pass number mismatch for %',v_key;
    end if;
    if (v_pass->>'evaluator_version') is distinct from v_run.evaluator_version then
     raise exception 'Evaluator version differs from run for %',v_key;
    end if;
    if not (v_pass->'context_keys') ? v_key then
     raise exception 'Assessment context does not include %',v_key;
    end if;
    for v_context_key in select value from jsonb_array_elements_text(v_pass->'context_keys') loop
     if v_context_key !~ ('^' || v_surah::text || ':[1-9][0-9]*$')
        or not exists (select 1 from public.editorial_canonical_ayahs
          where surah_number=v_surah and ayah_number=split_part(v_context_key,':',2)::integer) then
      raise exception 'Noncanonical or cross-surah context key % for %',v_context_key,v_key;
     end if;
    end loop;
    if v_pass->>'semantic_assessment' = 'ELIGIBLE_CANDIDATE' then
     if jsonb_typeof(v_pass->'passage_first_ayah') is distinct from 'number'
       or jsonb_typeof(v_pass->'passage_last_ayah') is distinct from 'number'
       or jsonb_typeof(v_pass->'standalone_sufficient') is distinct from 'boolean'
       or jsonb_typeof(v_pass->'exclusion_indicators') is distinct from 'array'
       or (v_pass->>'uncertainty') is distinct from 'clear' then
      raise exception 'Eligible candidate lacks clear passage evidence for %',v_key;
     end if;
     v_first_bound := (v_pass->>'passage_first_ayah')::integer;
     v_last_bound := (v_pass->>'passage_last_ayah')::integer;
     if v_first_bound > v_ayah or v_last_bound < v_ayah
       or v_first_bound > v_last_bound
       or ((v_pass->>'standalone_sufficient')::boolean
         and (v_first_bound <> v_ayah or v_last_bound <> v_ayah))
       or jsonb_array_length(v_pass->'exclusion_indicators') <> 0
       or not exists (select 1 from public.editorial_canonical_ayahs
         where surah_number=v_surah and ayah_number=v_first_bound)
       or not exists (select 1 from public.editorial_canonical_ayahs
         where surah_number=v_surah and ayah_number=v_last_bound) then
      raise exception 'Invalid eligible passage bounds or exclusion indicators for %',v_key;
     end if;
    end if;
    v_fingerprint := encode(extensions.digest(convert_to(
      v_pass::text || ':' || v_source_hash || ':' || v_translation_hash
       || ':' || v_footnotes_hash,
      'UTF8'),'sha256'),'hex');
    insert into public.editorial_ayah_assessments(
     run_id,policy_version,surah_number,ayah_number,pass_number,semantic_assessment,
     candidate_theme,assessment_rationale,context_notes,source_hash,translation_hash,footnotes_hash,
     evaluator_version,evidence,evidence_fingerprint
    ) values (
     p_run_id,v_run.policy_version,v_surah,v_ayah,v_pass_number,v_pass->>'semantic_assessment',
     v_pass->>'candidate_theme',v_pass->>'assessment_rationale',
     v_pass->>'context_notes',v_source_hash,v_translation_hash,v_footnotes_hash,
     v_pass->>'evaluator_version',v_pass,v_fingerprint
    ) on conflict do nothing;
    if not exists (select 1 from public.editorial_ayah_assessments
     where run_id=p_run_id and policy_version=v_run.policy_version
      and surah_number=v_surah and ayah_number=v_ayah and pass_number=v_pass_number
      and evidence_fingerprint=v_fingerprint) then
     raise exception 'Conflicting immutable pass evidence for % pass %',v_key,v_pass_number;
    end if;
   end loop;
   if (v_first->>'prompt_sha256') = (v_second->>'prompt_sha256') then
    raise exception 'Independent passes share one prompt for %',v_key;
   end if;
   v_semantic := case
    when v_first->>'semantic_assessment' = v_second->>'semantic_assessment'
      and v_first->>'semantic_assessment' <> 'ELIGIBLE_CANDIDATE'
      then v_first->>'semantic_assessment'
    when v_first->>'semantic_assessment' = 'ELIGIBLE_CANDIDATE'
      and v_second->>'semantic_assessment' = 'ELIGIBLE_CANDIDATE'
      and ((v_first->>'passage_first_ayah') is not distinct from (v_second->>'passage_first_ayah'))
      and ((v_first->>'passage_last_ayah') is not distinct from (v_second->>'passage_last_ayah'))
      and ((v_first->>'standalone_sufficient') is not distinct from (v_second->>'standalone_sufficient'))
      and (v_first->'context_keys') = (v_second->'context_keys')
      then 'ELIGIBLE_CANDIDATE'
    else 'AMBIGUOUS_REVIEW_REQUIRED' end;
   v_fingerprint := encode(extensions.digest(convert_to(
    v_first::text || ':' || v_second::text || ':' || v_source_fingerprint
     || ':' || v_run.assessment_version,'UTF8'),'sha256'),'hex');
  else
   if v_first is not null or v_second is not null then
    raise exception 'Non-screened row may not contain assessment passes: %',v_key;
   end if;
   if nullif(v_row->>'failure_reason','') is null then
    raise exception 'Failure reason required for %',v_key;
   end if;
   v_semantic := null;
   v_fingerprint := encode(extensions.digest(convert_to(
    v_status || ':' || v_key || ':' || v_row->>'failure_reason'
      || ':' || coalesce(v_source_hash,'') || ':' || coalesce(v_translation_hash,''),
    'UTF8'),'sha256'),'hex');
  end if;
  if v_existing.screening_status = 'SCREENED'
     and v_existing.evidence_fingerprint is distinct from v_fingerprint then
   raise exception 'Existing screened evidence cannot be overwritten for %',v_key;
  end if;
  update public.editorial_ayah_reviews set
   screening_status=v_status, semantic_assessment=v_semantic,
   screening_theme=case when v_semantic='ELIGIBLE_CANDIDATE'
    then v_first->>'candidate_theme' else null end,
   assessment_rationale=case when v_status='SCREENED'
    then 'PASS 1: ' || (v_first->>'assessment_rationale') ||
     E'\nPASS 2: ' || (v_second->>'assessment_rationale') else null end,
   context_notes=case when v_status='SCREENED'
    then 'PASS 1: ' || (v_first->>'context_notes') ||
     E'\nPASS 2: ' || (v_second->>'context_notes') else null end,
   source_hash=v_source_hash,translation_hash=v_translation_hash,
   assessment_version=v_run.assessment_version,
   evaluator_version=v_run.evaluator_version,
   screening_run_id=p_run_id,
   screening_failure_reason=case when v_status='SCREENED'
    then null else v_row->>'failure_reason' end,
   evidence_fingerprint=v_fingerprint,
   screened_at=case when v_status='SCREENED' then now() else null end,
   updated_at=now()
  where policy_version=v_run.policy_version and surah_number=v_surah and ayah_number=v_ayah
   and (evidence_fingerprint is distinct from v_fingerprint or screening_status <> v_status);
 end loop;
 insert into public.editorial_screening_batches(run_id,batch_id,payload_sha256,item_count,status)
 values(p_run_id,p_batch_id,v_payload_sha,jsonb_array_length(p_rows),'COMPLETED');
 update public.editorial_screening_runs set status='RUNNING',updated_at=now()
 where run_id=p_run_id;
 return jsonb_build_object('run_id',p_run_id,'batch_id',p_batch_id,
  'item_count',jsonb_array_length(p_rows),'payload_sha256',v_payload_sha,'replayed',false);
end $$;
revoke all on function public.ingest_editorial_screening_batch(text,text,jsonb)
 from public,anon,authenticated;
grant execute on function public.ingest_editorial_screening_batch(text,text,jsonb)
 to service_role;

do $$
begin
 if (select count(*) from public.editorial_canonical_ayahs) <> 6236 then
  raise exception 'Canonical identity count mismatch';
 end if;
 if (select count(*) from public.editorial_ayah_reviews where policy_version='calm-inviting-v1') <> 6236 then
  raise exception 'Existing review identity count mismatch';
 end if;
end $$;
