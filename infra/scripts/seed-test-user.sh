#!/usr/bin/env bash
# Gives a test user synthetic cards in Neon's bank.cards (ADR 0008), found by their email: looks up
# their Identity Platform user ID (the token's `sub`), then runs migrations/seed_bank.py on `main`.
# The owner connection string and the full user ID never reach the terminal.
#
# Run from the repo root, after migration 0005, once the user has signed in on the chat page:
#   bash infra/scripts/seed-test-user.sh <email> [market]    # market defaults to US
# Rerunning adds nothing. Needs: .env with GCP_PROJECT_ID; gcloud logged in as the project owner;
# python3; uv.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

email="${1:?usage: seed-test-user.sh <email> [market], e.g. US}"
market="${2:-US}"
JSON="python3 infra/scripts/neon_secret_json.py"

# The admin lookup by email; the quota project header is required with a user's own credentials.
token="$(gcloud auth print-access-token)"
response="$(curl -sS --fail-with-body -X POST \
  -H "Authorization: Bearer $token" \
  -H "x-goog-user-project: $GCP_PROJECT_ID" \
  -H "Content-Type: application/json" \
  --data "$(python3 -c 'import json, sys; print(json.dumps({"email": [sys.argv[1]]}))' "$email")" \
  "https://identitytoolkit.googleapis.com/v1/projects/$GCP_PROJECT_ID/accounts:lookup")" || {
  echo "The Identity Platform lookup failed:" >&2
  echo "$response" | head -5 >&2
  exit 1
}

sub="$(printf '%s' "$response" | python3 -c '
import json, sys
users = json.load(sys.stdin).get("users", [])
print(users[0]["localId"] if len(users) == 1 else "")
')"
if [ -z "$sub" ]; then
  echo "No user with that email. Sign in on the chat page by email link first, then rerun." >&2
  exit 1
fi
echo "Found the user (ID ${sub:0:4}…); seeding $market cards on main."

# Captured, not piped on: a failed read stops here instead of seeding with an empty URL.
owner_url="$(gcloud secrets versions access latest --secret neon-owner-urls --project "$GCP_PROJECT_ID" |
  $JSON get main)"
MIGRATIONS_DATABASE_URL="$owner_url" uv run --quiet python -m migrations.seed_bank \
  --market "$market" "$sub"
