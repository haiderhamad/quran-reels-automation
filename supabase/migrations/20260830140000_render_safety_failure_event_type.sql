-- Migration: 20260830140000_render_safety_failure_event_type.sql
-- Description: Allow render_safety_failure in background_events event_type check constraint.
-- Phase 6A: Support runtime retry telemetry in background_events.

alter table public.background_events
  drop constraint if exists background_events_event_type_check;

alter table public.background_events
  add constraint background_events_event_type_check
  check (event_type in (
    'safety_rejected',
    'metadata_ineligible',
    'transient_failure',
    'stale',
    'usage',
    'render_safety_failure'
  ));
