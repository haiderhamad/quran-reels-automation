-- Run on an isolated test database as service_role after applying the migrations.
do $$
declare
  slow_id uuid;
  next_id uuid;
  fresh_id uuid;
  original_lease uuid;
  claimed public.background_candidates;
begin
  insert into public.background_candidates(provider, provider_asset_id, provider_variant_id, status, discovered_at)
  values ('pixabay', 'qualification-timeout-fixture', 'test', 'PENDING', now() - interval '2 minutes')
  returning candidate_id into slow_id;

  insert into public.background_candidates(provider, provider_asset_id, provider_variant_id, status, discovered_at)
  values ('pexels', 'qualification-next-fixture', 'test', 'PENDING', now() - interval '1 minute')
  returning candidate_id into next_id;

  claimed := public.claim_background_candidate('qualification-test');
  if claimed.candidate_id is distinct from slow_id then
    raise exception 'Expected the oldest candidate to be claimed first';
  end if;
  original_lease := claimed.lease_token;
  perform public.complete_background_candidate(slow_id, original_lease, 'TRANSIENT_FAILURE',
    'CANDIDATE_PROCESSING_TIMEOUT');

  claimed := public.claim_background_candidate('qualification-test');
  if claimed.candidate_id is distinct from next_id then
    raise exception 'Expected the next candidate after a timeout';
  end if;
  perform public.complete_background_candidate(next_id, claimed.lease_token, 'VERIFIED');

  claimed := public.claim_background_candidate('another-worker');
  if claimed.candidate_id is not null then
    raise exception 'Expected a timed-out candidate to be deferred for every worker';
  end if;

  update public.background_candidates
     set completed_at = now() - interval '16 minutes'
   where candidate_id = slow_id;
  insert into public.background_candidates(provider, provider_asset_id, provider_variant_id, status)
  values ('pexels', 'qualification-fresh-fixture', 'test', 'PENDING')
  returning candidate_id into fresh_id;

  claimed := public.claim_background_candidate('qualification-test');
  if claimed.candidate_id is distinct from fresh_id then
    raise exception 'Expected fresh work before older timed-out retries';
  end if;
  perform public.complete_background_candidate(fresh_id, claimed.lease_token, 'VERIFIED');

  claimed := public.claim_background_candidate('qualification-test');
  if claimed.candidate_id is distinct from slow_id
     or claimed.lease_token is not distinct from original_lease
     or claimed.attempt_count <> 2 then
    raise exception 'Expected the deferred candidate to become eligible with a new lease';
  end if;
  if (public.complete_background_candidate(slow_id, original_lease, 'VERIFIED')).candidate_id is not null then
    raise exception 'An earlier lease must not be able to complete the new attempt';
  end if;
  perform public.complete_background_candidate(slow_id, claimed.lease_token, 'TRANSIENT_FAILURE', 'NetworkError');
  claimed := public.claim_background_candidate('qualification-test');
  if claimed.candidate_id is distinct from slow_id then
    raise exception 'Expected existing retry behavior for failures other than timeouts';
  end if;

  delete from public.background_candidates where candidate_id in (slow_id, next_id, fresh_id);
end;
$$;
