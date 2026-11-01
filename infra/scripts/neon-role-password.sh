#!/usr/bin/env bash
# Sets (or rotates) a Neon app role's password and stores its connection string in Secret Manager,
# without it ever reaching the terminal, a file or shell history (ADR 0006). Older versions of the
# secret are destroyed, so only the new password is ever readable.
#
# Run from the repo root, after the migrations that create the role:
#   bash infra/scripts/neon-role-password.sh usage_writer
# Needs: .env with GCP_PROJECT_ID; gcloud logged in as the project owner; uv.
# Rotating breaks new connections that still use the old password, so redeploy right after.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

role="${1:?usage: neon-role-password.sh <role>, e.g. usage_writer}"
secret="neon-${role//_/-}-url" # usage_writer -> neon-usage-writer-url

if ! gcloud secrets describe "$secret" --project "$GCP_PROJECT_ID" --format="value(name)" >/dev/null; then
  echo "The $secret secret container doesn't exist yet: merge and apply the gcp stack first." >&2
  exit 1
fi

old_versions="$(gcloud secrets versions list "$secret" --project "$GCP_PROJECT_ID" \
  --filter="state:ENABLED" --format="value(name)" 2>/dev/null)"

# Captured, not piped straight on: if setting the password fails, nothing is stored.
role_url="$(gcloud secrets versions access latest --secret neon-owner-url --project "$GCP_PROJECT_ID" |
  uv run --quiet python infra/scripts/neon_role_password.py "$role")"
printf %s "$role_url" | gcloud secrets versions add "$secret" --project "$GCP_PROJECT_ID" \
  --data-file=- --quiet >/dev/null
unset role_url
echo "Stored the $role connection string in Secret Manager ($secret)."

for version in $old_versions; do
  gcloud secrets versions destroy "$version" --secret "$secret" --project "$GCP_PROJECT_ID" --quiet >/dev/null
  echo "Destroyed the old version $version."
done
