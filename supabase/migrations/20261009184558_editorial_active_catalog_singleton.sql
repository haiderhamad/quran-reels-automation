-- One global active production catalog prevents stale policy workers from continuing.
create unique index editorial_active_catalog_singleton_idx on public.editorial_active_catalog ((true));
