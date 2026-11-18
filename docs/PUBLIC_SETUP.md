# Self-hosting Quran Reels

Use the public repository for development and secret-free CI. For provider access create a private GitHub installation, review its default branch, protect workflow changes and use installation-owned credentials. Credentialed jobs require private repository visibility, the default branch, and their respective opt-in. Secrets from one installation never transfer to another.

## Create the private installation

1. Create an independent private repository under your own GitHub account or organization. You can use GitHub's **Import repository** with the public source URL and choose **Private** for the destination. Check that its default branch contains `.github/workflows/generate-reel.yml` and that Actions is enabled.
2. Put dashboard values on the server running Next.js. During local development use `apps/dashboard/.env.local`. For dashboard dispatch, set `GITHUB_OWNER` and `GITHUB_REPO` to the owner and repository name separately, `GITHUB_WORKFLOW_REF` to the private default branch, and `GITHUB_WORKFLOW_ID=generate-reel.yml`. Use a repository-scoped token with Actions write access for `GITHUB_ACTIONS_TOKEN`.
3. Configure hosted workers separately under **Settings → Secrets and variables → Actions** in that private repository. The [Generate Reel workflow](../.github/workflows/generate-reel.yml) shows the exact mappings: `secrets.*` are repository secrets and `vars.*` are repository variables. A GitHub runner starts with a fresh checkout and cannot read your local `.env.local`.

Start with the [admin login and database setup](../DEPLOYMENT.md). Add the accounts below as you reach the corresponding step. Keep the publishing variables off while testing the first private MP4.

## Accounts and opt-ins

| Component | Configuration |
| --- | --- |
| Dashboard | Set the server-only fields in `apps/dashboard/.env.example` on your own host. Choose your admin password hash, session secret, GitHub repository, branch and Actions token. |
| Supabase | Set `STATE_AUTHORITY=supabase` on the server and local workers. Provide `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` to the relevant workers and dashboard. Initialize the empty database using the [deployment guide](../DEPLOYMENT.md). |
| Quran Foundation | Configure `QF_CLIENT_ID` and `QF_CLIENT_SECRET` for prelive testing. The production worker reads `QF_PRODUCTION_CLIENT_ID` and `QF_PRODUCTION_CLIENT_SECRET`. |
| Nature footage | Set your own `PEXELS_API_KEY` and `PIXABAY_API_KEY`. `QURAN_REELS_BACKGROUND_ENABLED=true` enables the two scheduled screening workers, with per-job opt-outs. Discovery writes nominations and qualification writes safety evidence. |
| B2 final-media storage | Set `B2_ENDPOINT`, `B2_REGION`, `B2_BUCKET_NAME`, `B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY` for your private media bucket. The optional archive uses separate Google Drive credentials. |
| Buffer | Configure your Buffer API key, organization, channels and delivery origin. `BUFFER_PUBLISHING_ENABLED`, `BUFFER_FACEBOOK_ENABLED` and `BUFFER_INSTAGRAM_ENABLED` default to false. The separate production gate is `QURAN_REELS_PRODUCTION_ENABLED`. Owner-mode production defaults to preview and has its own publish gate. |
| MP3Quran | Verify permission for the intended recitation, reuse and platforms before setting `RECITER_REUSE_CONFIRMED`. The timing and audio checks still apply. |

The Buffer caption credits Quran Foundation and names Rowwad Translation Center, QuranEnc.com and the validated translation edition(s) carried by the run. The worker's test mode may upload to B2. Keep release and delivery switches off during setup. For offline development use the tests in the [README](../README.md).

## English Rowwad 1.0.19

You need lawful access to the exact original English translation, footnotes and metadata. An API key or public access to a newer version cannot establish this. QuranEnc's republication conditions require updates to the latest source edition. Before publishing an older pinned edition, obtain an applicable permission or adopt a newly reviewed source revision and revalidate the dependent manifests. The independent canonical SHA-256 pinned in the engine is `c3369ca614d376b6f5f81fd4273a74e7379d9c5fc8beccc99b1820206a796dd6`. It covers 114 surahs and 6,236 ayahs. If the original version is unavailable, keep the worker disabled.

If you possess authorized original QuranEnc API responses, place the translation list response in `metadata.json` and the surah response objects in `001.json` through `114.json` in a private directory. Package and check them locally. The packaging command refuses a source mismatch and an existing output file.

```sh
python -m scripts.package_rowwad_reference --inputs /secure/rowwad-responses --output /secure/rowwad-1.0.19.json.gz
python -m scripts.verify_operator_sources --reference /secure/rowwad-1.0.19.json.gz
```

If you already have a lawful schema-1 JSON/gzip in this project's validated cache format, you can use the second command directly. These commands perform the Rowwad validation offline. The verifier prints only identity, counts and hashes. Keep the source outside the public repository.

Place the verified gzip in the operator's own restricted B2 location. Create a read-only key scoped to that object or its dedicated bucket. Configure the private installation's Actions secrets `ROWWAD_REFERENCE_B2_ENDPOINT`, `ROWWAD_REFERENCE_B2_REGION`, `ROWWAD_REFERENCE_B2_BUCKET`, `ROWWAD_REFERENCE_B2_KEY_ID`, `ROWWAD_REFERENCE_B2_KEY` and `ROWWAD_REFERENCE_OBJECT_KEY`. Set the Actions variable `ROWWAD_REFERENCE_GZIP_SHA256` to the verified transport digest. The worker checks the bounded download against that digest and independently validates the canonical corpus before exporting a temporary cache path. A newer translation cannot be relabeled as 1.0.19.

## Reviewed panels and exact passages

Public source includes the manifest parser, validation, renderer and synthetic test vectors. An operator must supply any rights-cleared reviewed manifests needed by their private workflow. Provision them at `alignments/english_rwwad/v1/SSS/AAA.json` in the private worker checkout. Quran Foundation restricts storage of regular API content beyond one week and specifies Content Sync for eligible offline resources. Private Git history is persistent storage. Use a rights-cleared source and retention/sync method for your manifests. [Quran Foundation Developer Terms](https://api-docs.quran.com/legal/developer-terms/) Examples for 2:20 and 2:25 use `002/020.json` and `002/025.json`. The public ignore rules exclude this directory. For hosted runners, provision the manifest files into the private checkout after checkout and before the worker starts, using an authenticated read from your own rights-cleared storage. A private deployment may version approved source manifests only when its permissions explicitly cover retention there. Never put source-text manifests in a public pull request.

A reviewer must check the semantic boundaries against the entire authorized Arabic and English ayah, including the treatment of footnotes, the recitation and the planned display. Use the exact schema in `engine/alignments.py`. It records the two source hashes, source/version, tokenization, review date/note, timing strategy and ordered panels with zero-based half-open word/token spans and their exact original slices. The status field records a review that already took place. It cannot substitute for one.

For local CLI verification, export your own **production** credentials as `QF_CLIENT_ID` and `QF_CLIENT_SECRET` in the current process. The GitHub worker maps its repository secrets `QF_PRODUCTION_CLIENT_ID` and `QF_PRODUCTION_CLIENT_SECRET` to those two names. Each command checks the trusted sources and exact reconstruction. Neither command generates media.

```sh
python -m scripts.verify_operator_sources --reference /secure/rowwad-1.0.19.json.gz --verse 2:20
python -m scripts.verify_operator_sources --reference /secure/rowwad-1.0.19.json.gz --verse 2:25
```

A missing or invalid manifest fails this explicit verification. During production the whole-ayah fallback still has to pass timing and fit. When it cannot, the verse stays pending. A source check alone cannot establish authentic timing, readability, final video safety, posting rights or verified delivery.

The private database review registry governs joining reviewed multi-ayah ranges. It starts empty. For an actual review, preserve the exact bounds, date and a `private-owner-` reference to evidence held by that installation. The service role has read-only access to the registry. An ambiguous delivery or failed source check must not advance the cycle.

### Record a reviewed multi-ayah passage

A single-ayah panel manifest describes display boundaries within one verse. To combine several complete ayahs, the private operator independently reviews the exact contiguous passage and retains evidence under a stable reference. Every member must be in the frozen catalog in the same order, without gaps, overlaps or a cross-surah boundary. Inspect the existing registry and the catalog ordinals before authorizing the range. Keep the worker and scheduler disabled during initial setup.

Using an administrative connection to the operator's own database, bind the four parameters below through a PostgreSQL client that supports parameterized statements. They represent the first key, last key, private evidence reference and actual review date. The reference must start with `private-owner-` and the date must not be in the future. Never grant registry writes to the worker role.

```sql
INSERT INTO public.calm_reels_v1_reviewed_passages
    (first_ayah_key, last_ayah_key, approval_reference, reviewed_at)
VALUES ($1, $2, $3, $4::date)
RETURNING first_ayah_key, last_ayah_key, approval_reference, reviewed_at;
```

Compare all four returned fields to the signed-off evidence before committing the administrative transaction. Read the same row on a fresh connection using all four original parameters. Require exactly one row with the approved values.

```sql
SELECT first_ayah_key, last_ayah_key, approval_reference, reviewed_at
FROM public.calm_reels_v1_reviewed_passages
WHERE first_ayah_key = $1 AND last_ayah_key = $2
  AND approval_reference = $3 AND reviewed_at = $4::date;
```

The production worker separately binds that approval identity to its claim, rendered passage and receipt. Adding a row alone cannot change the 2,211-member catalog or advance a delivery counter.

## Build a background pool

**On GitHub Actions**, use your private default branch. Set the repository variable `QURAN_REELS_BACKGROUND_ENABLED=true` after configuring Pexels, Pixabay and Supabase. **Discover Background Candidates** records provider metadata. **Qualify Background Candidates** downloads candidates, screens the full video and records the result. You can run these workflows manually or let their included schedules fill the pool. Setting `BACKGROUND_DISCOVERY_ENABLED=false` or `BACKGROUND_QUALIFICATION_ENABLED=false` pauses the corresponding job.

**On a Mac**, use the Python environment and pinned models from the [developer checks](../README.md#run-the-developer-checks). Set `STATE_AUTHORITY=supabase`, your Supabase URL and service-role key, and both provider keys in the local environment. From the repository root run:

```sh
python -m scripts.local_background_session --duration-minutes 30 --max-jobs 3
```

This session can write nominations and screening results to the same Supabase queue used by Actions. The limit is three qualification jobs. Rejected candidates do not become available for rendering. Check the pool for at least one eligible background before trying the Reel worker.

## From the first MP4 to Buffer

In the private repository, open **Settings → Secrets and variables → Actions**. Put worker credentials under **Secrets** and the workflow switches under **Variables**. The switches are repository-wide. In **Actions → Run workflow**, choose the private default branch for each run.

1. **Configure the private installation.** Initialize Supabase and save the first dashboard settings with automation off. Provide B2 media storage, Quran Foundation credentials for the relevant environment, a verified Rowwad reference, and the reviewed material needed for your selected verses. Set the repository variable `QURAN_REELS_PRODUCTION_ENABLED=true` to allow the hosted worker to start on the default branch. Leave the Buffer flags and `QURAN_REELS_V1_PUBLISH_ENABLED` off for the test.
2. **Render one private Reel.** In **Actions → Generate Reel → Run workflow**, select the default branch and `run_kind=test`. Leave the schedule slot and optional verse key blank for automatic test selection. Test runs use the prelive Quran Foundation credentials. On success, open the MP4 identified by the run's B2 media key and review Arabic, translation, footnotes when present, background and audio timing. The result reports `NOT_ATTEMPTED` for both social channels.
3. **Inspect the next production selection.** With `QURAN_REELS_V1_MODE=preview`, choose `run_kind=manual_production` and leave the schedule slot blank. A successful selection check reports `SKIPPED` with the ayah and confirmed count. This mode stops before rendering. A job skipped before its first step instead means the private-repository, default-branch or workflow gate was unmet.
4. **Check the Buffer connection.** Configure the worker secret `BUFFER_API_KEY` and repository variables `BUFFER_ORGANIZATION_ID`, `BUFFER_FACEBOOK_CHANNEL_ID`, `BUFFER_INSTAGRAM_CHANNEL_ID` and `PUBLIC_MEDIA_DELIVERY_ORIGIN`. Run **Buffer Readiness Check**. Verify the intended Facebook Page and Instagram account both report `CONNECTED`. Buffer must also be able to fetch the finished MP4 from your installation's HTTPS media delivery route.
5. **Publish once when ready.** Save both platform selections and confirmed recitation reuse in the dashboard. Set `RECITER_REUSE_CONFIRMED=true` only after obtaining permission. Under **Settings → Secrets and variables → Actions → Variables**, set `PUBLISHER_BACKEND=buffer` and `QURAN_REELS_V1_MODE=publish`. Set `QURAN_REELS_V1_PUBLISH_ENABLED`, `BUFFER_PUBLISHING_ENABLED`, `BUFFER_FACEBOOK_ENABLED` and `BUFFER_INSTAGRAM_ENABLED` to `true`. In **Actions → Generate Reel → Run workflow**, choose `manual_production`, select both platform targets and leave `force` off. This requests a real post on both channels.
6. **Verify delivery before enabling a schedule.** Inspect each channel's run record and Buffer outcome. A confirmed Facebook receipt advances the catalog. If a result is `UNKNOWN_OUTCOME`, reconcile the existing send with Buffer and the destination before retrying. After verifying the manual flow, configure a single heartbeat using the [scheduler guide](../DEPLOYMENT.md#scheduler).
