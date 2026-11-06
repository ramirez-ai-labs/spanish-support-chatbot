"""Reads and updates the JSON held by a merged Neon secret, for piping only (ADR 0006).

Some connection strings share one secret version, to stay inside Secret Manager's free tier. The
current JSON comes in on stdin:

    get <key>   prints that value
    has <key>   exits 0 if the key is there, 1 if not; prints nothing
    set <key>   prints the JSON with that key set to the NEW_VALUE environment variable

The value comes from the environment because arguments are visible to other processes. `get` and
`set` refuse to print to a terminal, and no error message includes a value.
"""

import json
import os
import sys

COMMANDS = {"get", "has", "set"}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in COMMANDS:
        print("usage: neon_secret_json.py <get|has|set> <key>", file=sys.stderr)
        return 2
    command, key = sys.argv[1:]
    if command != "has" and sys.stdout.isatty():
        print("Refusing to print a secret to the terminal; pipe it.", file=sys.stderr)
        return 2

    try:
        urls = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("The secret's value isn't JSON.", file=sys.stderr)
        return 1
    if not isinstance(urls, dict):
        print("The secret's value isn't a JSON object.", file=sys.stderr)
        return 1

    if command == "has":
        return 0 if urls.get(key) else 1
    if command == "get":
        if not urls.get(key):
            print(f"The secret has no value for {key}.", file=sys.stderr)
            return 1
        sys.stdout.write(urls[key])
        return 0
    value = os.environ.get("NEW_VALUE", "")
    if not value:
        print(f"NEW_VALUE is empty; {key} wasn't changed.", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps({**urls, key: value}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
