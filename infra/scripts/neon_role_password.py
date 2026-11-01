"""Sets a Neon role's password and prints that role's connection string, for piping only.

Reads the owner connection string on stdin, generates a random password and sets it over the
owner's TLS connection. Neon requires the plain password, not a SCRAM hash: its control plane
intercepts ALTER ROLE ... PASSWORD to sync the role, and rejects pre-hashed values. Prints the
role's connection string on stdout, and refuses to run if stdout is a terminal. Use it through
infra/scripts/neon-role-password.sh, which pipes the result straight into Secret Manager.
"""

import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

ROLES = {"usage_writer"}  # roles created by migrations; app_gcp and indexer join in M2


def main() -> int:
    role = sys.argv[1] if len(sys.argv) == 2 else ""
    if role not in ROLES:
        print(f"usage: neon_role_password.py <{'|'.join(sorted(ROLES))}>", file=sys.stderr)
        return 2
    if sys.stdout.isatty():
        print("Refusing to print a connection string to the terminal; pipe it.", file=sys.stderr)
        return 2

    owner_url = sys.stdin.read().strip()
    password = secrets.token_urlsafe(32)
    with psycopg.connect(owner_url, connect_timeout=30, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
        if exists is None:
            print(f"Role {role} doesn't exist yet: run the migrations first.", file=sys.stderr)
            return 1
        conn.execute(
            sql.SQL("alter role {} with login password {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )

    parts = urlsplit(owner_url)
    host = parts.netloc.rsplit("@", 1)[-1]
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    sys.stdout.write(urlunsplit(parts._replace(netloc=netloc)))
    print(f"Set a new password for {role}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
