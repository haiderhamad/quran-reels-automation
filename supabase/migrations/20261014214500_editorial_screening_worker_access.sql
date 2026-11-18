-- Worker operational state is service-role only.  RLS already blocks rows,
-- and explicit privilege revocation prevents a future policy from widening
-- access to lease ownership or evaluator failures by accident.

revoke all on table public.editorial_screening_leases from anon, authenticated;
revoke all on table public.editorial_screening_worker_jobs from anon, authenticated;
