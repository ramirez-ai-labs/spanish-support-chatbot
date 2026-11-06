"""Prints what each app role may do, to check least privilege after a migration (read-only).

Also the pgvector version and the indexes on `chunks`, since 0004 adds HNSW only on 0.8.0+.

For each app role (usage_writer, indexer, app_gcp): its attributes, its role memberships
and its table privileges. An app role should show no attributes, no memberships, and only the
grants its migration gave it. Runs with the owner connection, like migrate.py:

    MIGRATIONS_DATABASE_URL="$(gcloud secrets versions access latest --secret neon-owner-urls \\
        --project "$GCP_PROJECT_ID" | python3 infra/scripts/neon_secret_json.py get main)" \
        uv run python migrations/grants.py
"""

import os
import sys

import psycopg

APP_ROLES = ("usage_writer", "indexer", "app_gcp")


def main() -> int:
    url = os.environ.get("MIGRATIONS_DATABASE_URL")
    if not url:
        print("Set MIGRATIONS_DATABASE_URL (see the docstring).", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=30) as conn:
        pgvector = conn.execute("select extversion from pg_extension where extname = 'vector'")
        indexes = conn.execute("select indexname from pg_indexes where tablename = 'chunks'")
        print(f"pgvector {(pgvector.fetchone() or ['missing'])[0]}")
        print(f"chunks indexes: {', '.join(row[0] for row in indexes) or 'none'}")
        for role in APP_ROLES:
            found = conn.execute(
                "select rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication,"
                " rolcanlogin from pg_roles where rolname = %s",
                (role,),
            ).fetchone()
            if found is None:
                print(f"{role}: missing")
                continue
            names = ("superuser", "createdb", "createrole", "bypassrls", "replication")
            attributes = [name for name, value in zip(names, found[:5], strict=True) if value]
            member_of = [
                row[0]
                for row in conn.execute(
                    "select r.rolname from pg_auth_members m join pg_roles r on r.oid = m.roleid"
                    " join pg_roles u on u.oid = m.member where u.rolname = %s",
                    (role,),
                )
            ]
            grants = [
                f"{row[0]} {row[1]}"
                for row in conn.execute(
                    "select table_name, string_agg(privilege_type, ',' order by privilege_type)"
                    " from information_schema.role_table_grants where grantee = %s"
                    " group by table_name order by table_name",
                    (role,),
                )
            ]
            print(
                f"{role}: login={'yes' if found[5] else 'no'}"
                f" attributes={attributes or 'none'} member_of={member_of or 'none'}"
                f" grants={grants or 'none'}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
