#!/usr/bin/env bash
# Sets (or rotates) a Neon app role's password and stores its connection string in Secret Manager,
# without it ever reaching the terminal, a file or shell history (ADR 0006). Older versions of the
# secret are destroyed, so only the new password is ever readable.
#
# Run from the repo root, after the migrations that create the role:
#   bash infra/scripts/neon-role-password.sh usage_writer
# On the `dev` branch (created by neon-bootstrap.sh), which keeps passwords of its own:
#   bash infra/scripts/neon-role-password.sh indexer dev    # -> neon-indexer-urls
# Connection strings share secrets by who reads them (ADR 0006); the others in the secret are kept.
# Needs: .env with GCP_PROJECT_ID; gcloud logged in as the project owner; uv.
# Rotating breaks new connections that still use the old password, so redeploy right after.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

role="${1:?usage: neon-role-password.sh <role> [dev], e.g. usage_writer}"
branch="${2:-main}"
case "$role:$branch" in
  usage_writer:main | app_gcp:main) secret="neon-api-urls"; key="$role" ;;
  indexer:main | indexer:dev) secret="neon-indexer-urls"; key="$branch" ;;
  *) echo "Unknown role and branch: $role on $branch." >&2; exit 2 ;;
esac
owner_secret="neon-owner-urls"
JSON="python3 infra/scripts/neon_secret_json.py"

if ! gcloud secrets describe "$secret" --project "$GCP_PROJECT_ID" --format="value(name)" >/dev/null; then
  echo "The $secret secret container doesn't exist yet: merge and apply the gcp stack first." >&2
  exit 1
fi

old_versions="$(gcloud secrets versions list "$secret" --project "$GCP_PROJECT_ID" \
  --filter="state:ENABLED" --format="value(name)" 2>/dev/null)"

# Read before the password changes, so a failed read changes nothing.
if [ -n "$old_versions" ]; then
  current="$(gcloud secrets versions access latest --secret "$secret" --project "$GCP_PROJECT_ID")"
else
  current="{}"
fi

# Captured, not piped straight on: if setting the password fails, nothing is stored.
role_url="$(gcloud secrets versions access latest --secret "$owner_secret" --project "$GCP_PROJECT_ID" |
  $JSON get "$branch" |
  uv run --quiet python infra/scripts/neon_role_password.py "$role")"
# Also captured: `versions add` in the same pipeline would store an empty version, which becomes
# `latest`, if updating the JSON failed.
merged="$(printf %s "$current" | NEW_VALUE="$role_url" $JSON set "$key")"
printf %s "$merged" | gcloud secrets versions add "$secret" --project "$GCP_PROJECT_ID" \
  --data-file=- --quiet >/dev/null
unset role_url current merged
echo "Stored the $role connection string in Secret Manager ($secret)."

for version in $old_versions; do
  gcloud secrets versions destroy "$version" --secret "$secret" --project "$GCP_PROJECT_ID" --quiet >/dev/null
  echo "Destroyed the old version $version."
done
