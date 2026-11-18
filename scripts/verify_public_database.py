"""Rehearse every public migration on a fresh, socket-only Supabase container.

Requires a running local Docker engine. Pulling the pinned public image is the
only external access needed. The container receives no project credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
# Upstream self-host Compose release and its Docker Hub multi-architecture index.
IMAGE = "supabase/postgres:17.6.1.136@sha256:f371b5f3f2ac0a05703f33d6e6134515fb2498cab708fb948a0aeb7481467c00"
ROLES = ("anon", "authenticated", "service_role")
# The pinned entrypoint uses 5432 for initdb's temporary socket. Only the
# final server uses this port, still with TCP disabled.
FINAL_SOCKET_PORT = "5433"


def docker(*args, **kwargs):
    sockets = (Path("/var/run/docker.sock"), Path.home() / ".docker/run/docker.sock")
    local = next((path for path in sockets if path.is_socket()), None)
    if local is None:
        raise RuntimeError("A running local Docker engine is required.")
    return subprocess.run(
        ["docker", "--host", f"unix://{local}", *args], check=True, text=True, **kwargs
    )


def sql(container, statement, *, role="postgres"):
    command = ["exec", "-i", "--user", "postgres", container, "psql", "-XqAt",
               "-v", "ON_ERROR_STOP=1", "-h", "/var/run/postgresql", "-U", role,
               "-p", FINAL_SOCKET_PORT, "-d", "postgres", "--single-transaction", "-f", "-"]
    return docker(*command, input=statement, stdout=subprocess.PIPE).stdout.strip()


def await_database(container):
    until = time.monotonic() + 180
    while time.monotonic() < until:
        status = docker("inspect", "--format", "{{.State.Status}}", container,
                        stdout=subprocess.PIPE).stdout.strip()
        if status != "running":
            raise RuntimeError(f"Supabase database container entered state {status}.")
        try:
            if sql(container, "select current_user;") == "postgres":
                return
        except subprocess.CalledProcessError:
            pass
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for the final Supabase socket on port {FINAL_SOCKET_PORT}.")


def verify(container):
    if sql(container, """select current_setting('listen_addresses') = '' and inet_server_addr() is null
        and current_setting('server_version_num')::int / 10000 = 17
        and not (select rolsuper from pg_roles where rolname=current_user)
        and (select rolbypassrls from pg_roles where rolname='service_role')
        and to_regclass('public.calm_reels_v1_cycle_state') is null;""") != "t":
        raise RuntimeError("Expected the fresh socket-only PostgreSQL 17 database and Supabase roles.")

    # Exercise the actual image's default grants before application migrations.
    grants = sql(container, """create table public.ci_default_acl_probe(id int);
        select bool_and(has_table_privilege(r,'public.ci_default_acl_probe','INSERT'))
        from unnest(array['anon','authenticated','service_role']) r;
        drop table public.ci_default_acl_probe;""")
    if grants != "t":
        raise RuntimeError("The Supabase role default table grants are missing.")

    migrations = sorted((ROOT / "supabase/migrations").glob("*.sql"))
    if not migrations:
        raise RuntimeError("No SQL migrations found.")
    for migration in migrations:
        contents = migration.read_bytes()
        print(f"Applying {migration.name} sha256={hashlib.sha256(contents).hexdigest()}", flush=True)
        sql(container, contents.decode("utf-8"))

    result = json.loads(sql(container, """select json_build_object(
        'extensions', (select json_object_agg(extname,extversion) from pg_extension
            where extname in ('pg_cron','pg_net','pgcrypto','supabase_vault')),
        'digest', encode(extensions.digest(convert_to('abc','UTF8'),'sha256'),'hex'),
        'jobs', (select count(*) from cron.job),
        'http_queue', (select count(*) from net.http_request_queue),
        'vault_entries', (select count(*) from vault.decrypted_secrets),
        'catalog_keys', (select json_agg(ayah_key order by ordinal) from public.calm_reels_v1_catalog_members),
        'canonical', (select count(*) from public.editorial_canonical_ayahs),
        'reviews', (select count(*) from public.calm_reels_v1_reviewed_passages),
        'receipts', (select count(*) from public.calm_reels_v1_confirmed_posts),
        'owner_claims', (select count(*) from public.calm_reels_v1_dispatch_claims),
        'buffer_claims', (select count(*) from public.buffer_publication_claims),
        'cycle', (select cycle_number from public.calm_reels_v1_cycle_state),
        'registry_rls', (select relrowsecurity from pg_class where oid='public.calm_reels_v1_reviewed_passages'::regclass)
    );""", role="supabase_admin"))
    extensions = result.pop("extensions")
    if set(extensions or {}) != {"pg_cron", "pg_net", "pgcrypto", "supabase_vault"}:
        raise RuntimeError(f"Missing Supabase extensions: {extensions}")
    catalog = json.loads((ROOT / "data/editorial/calm_reels_shortlist_v1.json").read_text())
    expected_keys = [row["key"] for row in catalog["ayahs"]]
    if len(expected_keys) != 2211 or result.pop("catalog_keys") != expected_keys:
        raise RuntimeError("Database topic catalog differs from the ordered source catalog.")
    expected = dict(digest=hashlib.sha256(b"abc").hexdigest(), jobs=0, http_queue=0, vault_entries=0,
                    canonical=6236, reviews=0, receipts=0, owner_claims=0, buffer_claims=0, cycle=1, registry_rls=True)
    if result != expected:
        raise RuntimeError(f"Unexpected fresh database state: {result}")

    for role in (*ROLES, "postgres"):
        grant = sql(container, f"select has_function_privilege('{role}','public.invoke_publishing_scheduler()','EXECUTE');")
        if grant != ("t" if role == "postgres" else "f"):
            raise RuntimeError(f"Unexpected scheduler privilege for {role}.")
    for role in ROLES:
        privileges = sql(container, f"""select p from unnest(array['SELECT','INSERT','UPDATE','DELETE',
            'TRUNCATE','REFERENCES','TRIGGER','MAINTAIN']) p where has_table_privilege(
            '{role}','public.calm_reels_v1_reviewed_passages',p);""").splitlines()
        if privileges != (["SELECT"] if role == "service_role" else []):
            raise RuntimeError(f"Unexpected review registry table privileges for {role}.")
        writable = sql(container, f"""select has_any_column_privilege('{role}',
            'public.calm_reels_v1_reviewed_passages','INSERT,UPDATE,REFERENCES');""")
        if writable != "f":
            raise RuntimeError(f"Unexpected review registry column privileges for {role}.")
    print(f"Passed {len(migrations)} migrations on {sql(container, 'show server_version;')}.")
    print(json.dumps({"extensions": extensions, "catalog_size": len(expected_keys), **result}, sort_keys=True))


def main():
    docker("pull", IMAGE)
    # Pass the single-use database password by environment name, never as a
    # command-line argument. The container has no published ports or host mounts.
    environment = {**os.environ, "POSTGRES_PASSWORD": secrets.token_hex(32)}
    container = docker("create", "--network", "none", "--no-healthcheck", "--env", "POSTGRES_PASSWORD",
                       IMAGE, "postgres", "-p", FINAL_SOCKET_PORT, "-c", "config_file=/etc/postgresql/postgresql.conf",
                       "-c", "listen_addresses=", "-c", "cron.launch_active_jobs=off",
                       env=environment, stdout=subprocess.PIPE).stdout.strip()
    try:
        docker("start", container)
        await_database(container)
        verify(container)
    finally:
        docker("rm", "--force", "--volumes", container)


if __name__ == "__main__":
    main()
