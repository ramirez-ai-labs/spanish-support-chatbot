#!/usr/bin/env bash
# Generates the master key for the sessions' placeholder HMACs (ADR 0008) and stores it in Secret
# Manager, without it ever reaching the terminal, a file or shell history.
#
# Run from the repo root, after the apply that creates the `session-hmac-key` container:
#   bash infra/scripts/session-hmac-key.sh            # refuses if a value already exists
#   bash infra/scripts/session-hmac-key.sh --rotate   # replaces it and destroys older versions
# Needs: .env with GCP_PROJECT_ID; gcloud logged in as the project owner; python3. Redeploy after
# rotating: instances read the secret when they start.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

SECRET="session-hmac-key"

if ! gcloud secrets describe "$SECRET" --project "$GCP_PROJECT_ID" --format="value(name)" >/dev/null; then
  echo "The $SECRET secret container doesn't exist yet: merge and apply the gcp stack first." >&2
  exit 1
fi

# Enabled and disabled versions both bill, so a rotation leaves only the new one.
old_versions="$(gcloud secrets versions list "$SECRET" --project "$GCP_PROJECT_ID" \
  --filter="state:ENABLED OR state:DISABLED" --format="value(name)")"
if [ -n "$old_versions" ] && [ "${1:-}" != "--rotate" ]; then
  echo "$SECRET already has a value. Pass --rotate to replace it." >&2
  exit 1
fi

python3 -c 'import secrets; print(secrets.token_hex(32), end="")' |
  gcloud secrets versions add "$SECRET" --project "$GCP_PROJECT_ID" --data-file=- --quiet >/dev/null
echo "Stored a new key in Secret Manager ($SECRET)."

for version in $old_versions; do
  gcloud secrets versions destroy "$version" --secret "$SECRET" --project "$GCP_PROJECT_ID" --quiet >/dev/null
  echo "Destroyed the old version $version."
done
