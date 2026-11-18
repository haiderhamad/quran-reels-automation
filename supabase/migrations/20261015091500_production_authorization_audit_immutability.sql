-- Harden the automatic production-authorization audit record without
-- changing any historical authorization or screening decision.

create or replace function public.editorial_validate_production_authorization()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $editorial_validate_authorization$
begin
  if not exists (
    select 1 from public.editorial_canonical_ayahs
    where surah_number = new.surah_number and ayah_number = new.ayah_number
  ) then
    raise exception 'Production authorization identity is not canonical: %:%', new.surah_number, new.ayah_number;
  end if;

  if not exists (
    select 1 from public.editorial_canonical_ayahs
    where surah_number = new.surah_number and ayah_number = new.passage_first_ayah
  ) or not exists (
    select 1 from public.editorial_canonical_ayahs
    where surah_number = new.surah_number and ayah_number = new.passage_last_ayah
  ) then
    raise exception 'Production authorization passage bounds are not canonical: %:%-%', new.surah_number, new.passage_first_ayah, new.passage_last_ayah;
  end if;

  if tg_op = 'UPDATE' then
    if (
      new.policy_version,
      new.surah_number,
      new.ayah_number,
      new.passage_first_ayah,
      new.passage_last_ayah,
      new.approval_basis,
      new.assessment_version,
      new.source_hash,
      new.translation_hash,
      new.evidence_fingerprint,
      new.screening_run_id,
      new.authorization_reason,
      new.authorized_at,
      new.created_at
    ) is distinct from (
      old.policy_version,
      old.surah_number,
      old.ayah_number,
      old.passage_first_ayah,
      old.passage_last_ayah,
      old.approval_basis,
      old.assessment_version,
      old.source_hash,
      old.translation_hash,
      old.evidence_fingerprint,
      old.screening_run_id,
      old.authorization_reason,
      old.authorized_at,
      old.created_at
    ) then
      raise exception 'Production authorization evidence is immutable';
    end if;

    if old.authorization_state = 'REVOKED' then
      raise exception 'A revoked production authorization cannot be changed';
    end if;

    if new.authorization_state is distinct from old.authorization_state
       and not (old.authorization_state = 'ACTIVE' and new.authorization_state = 'REVOKED') then
      raise exception 'Production authorization state may only transition from ACTIVE to REVOKED';
    end if;

    if new.authorization_state = 'ACTIVE' and (
      new.revoked_at is distinct from old.revoked_at
      or new.revoked_by is distinct from old.revoked_by
      or new.revocation_reason is distinct from old.revocation_reason
    ) then
      raise exception 'An active production authorization cannot have revocation metadata';
    end if;
  end if;

  new.updated_at := now();
  return new;
end;
$editorial_validate_authorization$;

create or replace function public.editorial_prevent_production_authorization_event_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $editorial_prevent_authorization_event_mutation$
begin
  raise exception 'Production authorization events are immutable';
end;
$editorial_prevent_authorization_event_mutation$;

drop trigger if exists editorial_prevent_production_authorization_event_mutation_trigger
  on public.editorial_production_authorization_events;
create trigger editorial_prevent_production_authorization_event_mutation_trigger
before update or delete on public.editorial_production_authorization_events
for each row execute function public.editorial_prevent_production_authorization_event_mutation();

-- Reviews are normally pre-created and updated by the ingestion RPC. Include
-- INSERT as well so a future canonical-ledger migration cannot accidentally
-- bypass the automatic policy decision.
drop trigger if exists editorial_auto_authorize_eligible_screening_trigger
  on public.editorial_ayah_reviews;
create trigger editorial_auto_authorize_eligible_screening_trigger
after insert or update of screening_status, semantic_assessment, assessment_version, source_hash,
  translation_hash, evidence_fingerprint, screening_run_id
on public.editorial_ayah_reviews
for each row execute function public.editorial_auto_authorize_eligible_screening();

revoke all on function public.editorial_prevent_production_authorization_event_mutation()
  from public, anon, authenticated;
