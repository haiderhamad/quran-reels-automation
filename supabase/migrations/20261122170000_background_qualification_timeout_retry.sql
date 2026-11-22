-- Defer timed-out candidates beyond the current 15-minute qualification job.
-- Keep the existing RPC signature, privileges, and atomic lease claim.

create or replace function public.claim_background_candidate(
  p_worker_id text,
  p_lease_seconds integer default 900
) returns public.background_candidates
language plpgsql security invoker set search_path = public
as $$
declare
  result public.background_candidates;
begin
  if coalesce(length(trim(p_worker_id)), 0) = 0 or p_lease_seconds not between 60 and 3600 then
    raise exception 'invalid background candidate lease input';
  end if;
  with candidate as (
    select candidate_id
      from public.background_candidates
     where status = 'PENDING'
        or (status = 'TRANSIENT_FAILURE' and (
          last_error_code is distinct from 'CANDIDATE_PROCESSING_TIMEOUT'
          or completed_at <= now() - interval '15 minutes'
        ))
        or (status = 'PROCESSING' and lease_expires_at < now())
     order by (status = 'TRANSIENT_FAILURE'
                 and last_error_code is not distinct from 'CANDIDATE_PROCESSING_TIMEOUT') asc,
              discovered_at asc, candidate_id asc
     for update skip locked
     limit 1
  )
  update public.background_candidates q
     set status = 'PROCESSING', worker_id = p_worker_id,
         lease_token = gen_random_uuid(), claimed_at = now(),
         lease_expires_at = now() + make_interval(secs => p_lease_seconds),
         attempt_count = q.attempt_count + 1, updated_at = now()
    from candidate
   where q.candidate_id = candidate.candidate_id
  returning q.* into result;
  return result;
end;
$$;
