alter function public.editorial_validate_screening_run_update() set search_path = pg_catalog;
alter function public.editorial_screening_audit_current() set search_path = pg_catalog;
alter function public.editorial_candidate_insert_guard() set search_path = pg_catalog;
alter function public.editorial_record_catalog_activation() set search_path = pg_catalog;
alter function public.editorial_prevent_immutable_change() set search_path = pg_catalog;
alter function public.editorial_normalized_text_sha256(text) set search_path = pg_catalog;
alter function public.ingest_editorial_screening_batch(text,text,jsonb) set search_path = pg_catalog;
