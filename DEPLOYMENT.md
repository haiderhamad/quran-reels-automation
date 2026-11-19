# Database and deployment

Use a separate private GitHub repository, a dedicated Supabase project, your own B2 buckets and a server-side dashboard deployment. The web app lives in `apps/dashboard`. Use that directory as the Vercel project root, configure access to the repository's shared `data/` directory when building, and set the dashboard runtime variables only on your own host.

## Create your first admin login

From `apps/dashboard`, after `npm ci`, create the local settings file. If it already exists, edit it in place. On macOS or Linux, use the following commands in a private terminal with Apache's `htpasswd` and OpenSSL installed.

```sh
(umask 077; test -e .env.local || cp .env.example .env.local)
htpasswd -nBC 12 admin
openssl rand -hex 32
```

1. Enter your chosen admin password at the hidden prompts. Copy the hash after `admin:` into `ADMIN_PASSWORD_HASH` in `.env.local`. Prefix **each** dollar sign in the hash with a backslash (`\$`) so Next.js reads it literally. When entering the hash directly in a hosting provider's environment settings, use the original unescaped hash.
2. Put the random value from OpenSSL in `SESSION_SECRET` in the same file. Keep both values on the server. The [environment template](apps/dashboard/.env.example) shows the other fields.
3. Start or restart `npm run dev`, open [the local login page](http://127.0.0.1:3000/login), and enter your password. A successful login opens `/dashboard`. To save settings, first connect and initialize Supabase as described below.

## Fresh database

The ordered SQL in `supabase/migrations/` targets a fresh Supabase PostgreSQL 17 or later installation. It uses the Supabase roles, pgcrypto, pg_cron, pg_net and Vault. For a local rehearsal, use a disposable checkout, the Supabase CLI and a running Docker engine. From the repository root, run `supabase init` if `supabase/config.toml` is absent, then `supabase start` to initialize the fresh local stack.

To rehearse on GitHub, open **Actions → Public database migration acceptance → Run workflow**. The runner starts an isolated Supabase PostgreSQL container, applies the ordered migrations, checks the initial state and permissions, then removes the container. It needs no account credentials.

For a new private installation, review the migration plan and apply this baseline only to its own empty schema. Existing installations need a separate migration reconciliation. The two baselines have different bootstrap semantics. The review ledger starts with 6,236 canonical identities marked unreviewed, an empty reviewed-passage registry, and an ordered 2,211-member channel catalog. The shortlist establishes membership. A mixed passage still needs the installation's own recorded review. Do not import an operator's historical approvals, counts, receipts or permissions.

For a **new, empty hosted project**, use the following Supabase CLI 3.0 commands from the repository root. Initialize the local CLI config as above. Replace `YOUR_PROJECT_REF` with the reference shown in your own Supabase project. These steps let you review the target and the pending SQL before applying it.

```sh
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase migration list --remote
supabase db push --remote --skip-vault --dry-run
```

Compare the plan with the files in `supabase/migrations/`. Once you have verified the target and approved that plan, apply it with `supabase db push --remote --skip-vault`. Run `supabase migration list --remote` again and check that the local and remote version columns match.

Set `STATE_AUTHORITY=supabase`, `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` on the dashboard server. Restart the local server after editing its environment. Sign in, open **Settings**, leave automation off, save and reload the page to confirm the configuration persists. For dashboard-triggered workflows, set the GitHub fields listed in the [private installation guide](docs/PUBLIC_SETUP.md#create-the-private-installation).

## Scheduler

The baseline creates `public.invoke_publishing_scheduler()`. It leaves heartbeat registration to the operator. Set up the scheduler after verifying one manual delivery.

| Caller | Dashboard route | How the dashboard authenticates it |
| --- | --- | --- |
| Supabase heartbeat | `GET /api/cron/publishing-scheduler` | Set the dashboard host's server-only `CRON_SECRET` to the same value as `cron_secret` in your Supabase Vault. Store your dashboard's HTTPS origin in Vault as `scheduler_origin`. |
| Manual tick client | `POST /api/scheduler/tick` | Supply the server-only `SCHEDULER_SECRET` as a bearer token when using this route. |

Review the saved dashboard automation setting, Berlin publishing slots and worker publication gates before registering a single pg_cron heartbeat for `select public.invoke_publishing_scheduler();`. Keep one recurring scheduler. The manual GitHub scheduler is available for recovery.

## Storage and delivery

The default Rowwad downloads use public HTTPS. If you choose B2 for your English reference, give it a separate read-only key scoped to that reference. Configure your own authorized HTTPS media delivery origin and Buffer organization/channel IDs. Account connection, source rights and channel selection are prerequisites. Reconcile uncertain Buffer outcomes with saved claims and platform readback before attempting another send.

The [setup guide](docs/PUBLIC_SETUP.md) describes provider settings, pinned public downloads, optional source packaging and reviewed panel manifests.
