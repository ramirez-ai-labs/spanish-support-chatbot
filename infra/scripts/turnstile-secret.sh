#!/usr/bin/env bash
# Rotates the chat widget's Turnstile secret through Cloudflare's API and stores the new one in
# Secret Manager, without it ever reaching the terminal, a file or shell history. The old secret
# stops working immediately, and older versions of the secret are destroyed.
#
# Run from the repo root, after the apply that creates the `turnstile-secret` container:
#   bash infra/scripts/turnstile-secret.sh
# Needs: .env with CLOUDFLARE_API_TOKEN (Turnstile: Edit), CLOUDFLARE_ACCOUNT_ID and GCP_PROJECT_ID;
# gcloud logged in as the project owner; curl and python3. Redeploy afterwards: instances read the
# secret when they start.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN in .env}"
: "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID in .env}"
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

API="https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/challenges/widgets"
WIDGET="spanish-support-chatbot chat" # the name the shared stack gives it
SECRET="turnstile-secret"

cf() {
  curl -sS --fail-with-body -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/json" "$@"
}

if ! gcloud secrets describe "$SECRET" --project "$GCP_PROJECT_ID" --format="value(name)" >/dev/null; then
  echo "The $SECRET secret container doesn't exist yet: merge and apply the gcp stack first." >&2
  exit 1
fi

sitekey="$(cf "$API" | python3 -c '
import json, sys
matches = [w["sitekey"] for w in json.load(sys.stdin)["result"] if w["name"] == sys.argv[1]]
print(matches[0] if len(matches) == 1 else "")' "$WIDGET")"
if [ -z "$sitekey" ]; then
  echo "Expected exactly one Turnstile widget named \"$WIDGET\"." >&2
  exit 1
fi

old_versions="$(gcloud secrets versions list "$SECRET" --project "$GCP_PROJECT_ID" \
  --filter="state:ENABLED" --format="value(name)" 2>/dev/null)"

# Captured, not piped straight on: if the rotation fails, nothing is stored.
new_secret="$(cf -X POST "$API/$sitekey/rotate_secret" -d '{"invalidate_immediately": true}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["secret"])')"
echo "Rotated the Turnstile secret; the old one no longer works."
printf %s "$new_secret" | gcloud secrets versions add "$SECRET" --project "$GCP_PROJECT_ID" \
  --data-file=- --quiet >/dev/null
unset new_secret
echo "Stored it in Secret Manager ($SECRET)."

for version in $old_versions; do
  gcloud secrets versions destroy "$version" --secret "$SECRET" --project "$GCP_PROJECT_ID" --quiet >/dev/null 2>&1
  echo "Destroyed the old version $version."
done
