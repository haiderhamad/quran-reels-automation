-- One authenticated Facebook Reel can contain several ayahs.
-- Deduplicate the same platform/post/ayah across ALL cycles, while allowing
-- each distinct ayah in that ONE clip to have its own cycle membership row.
-- No existing confirmed v1 rows at the fresh baseline; history untouched.
alter table public.calm_reels_v1_confirmed_posts
  drop constraint calm_reels_v1_confirmed_posts_platform_external_post_id_key;
alter table public.calm_reels_v1_confirmed_posts
  add constraint calm_reels_v1_post_ayah_globally_unique
  unique (platform, external_post_id, ayah_key);
