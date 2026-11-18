-- Private, preliminary one-pass ChatGPT editorial opinions.
--
-- These rows are deliberately not screening evidence. They cannot satisfy an
-- ingest call, cannot trigger automatic production authorization, and retain
-- the original contextual note until authenticated source evidence and a
-- genuinely independent second assessment exist.

create table if not exists public.editorial_chat_review_drafts (
  policy_version text not null default 'calm-inviting-v1',
  surah_number integer not null,
  ayah_number integer not null,
  preliminary_assessment text not null,
  assessment_rationale text not null,
  draft_batch text not null,
  review_origin text not null default 'chatgpt_single_contextual_pass',
  source_integrity_state text not null default 'UNVERIFIED',
  second_pass_state text not null default 'NOT_PERFORMED',
  publication_permission text not null default 'NONE_FROM_THIS_DRAFT',
  provenance_url text not null,
  documented_on date not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint editorial_chat_review_drafts_pk
    primary key (policy_version, surah_number, ayah_number),
  constraint editorial_chat_review_drafts_ledger_fk
    foreign key (policy_version, surah_number, ayah_number)
    references public.editorial_ayah_reviews(policy_version, surah_number, ayah_number),
  constraint editorial_chat_review_drafts_preliminary_assessment_check
    check (preliminary_assessment in (
      'ELIGIBLE_CANDIDATE', 'INELIGIBLE_CANDIDATE',
      'AMBIGUOUS_REVIEW_REQUIRED'
    )),
  constraint editorial_chat_review_drafts_assessment_rationale_check
    check (length(btrim(assessment_rationale)) >= 12),
  constraint editorial_chat_review_drafts_draft_batch_check
    check (length(btrim(draft_batch)) >= 5),
  constraint editorial_chat_review_drafts_review_origin_check
    check (review_origin = 'chatgpt_single_contextual_pass'),
  constraint editorial_chat_review_drafts_source_integrity_state_check
    check (source_integrity_state = 'UNVERIFIED'),
  constraint editorial_chat_review_drafts_second_pass_state_check
    check (second_pass_state = 'NOT_PERFORMED'),
  constraint editorial_chat_review_drafts_publication_permission_check
    check (publication_permission = 'NONE_FROM_THIS_DRAFT'),
  constraint editorial_chat_review_drafts_provenance_url_check
    check (provenance_url ~ '^https://www[.]notion[.]so/')
);

comment on table public.editorial_chat_review_drafts is
  'Private, preliminary one-pass ChatGPT editorial opinions only. Not a screening result, source attestation, second assessment, or production authorization. Not read by automatic publishing triggers.';

alter table public.editorial_chat_review_drafts enable row level security;
revoke all on table public.editorial_chat_review_drafts from public, anon, authenticated;
grant select, insert, update, delete on table public.editorial_chat_review_drafts to service_role;
