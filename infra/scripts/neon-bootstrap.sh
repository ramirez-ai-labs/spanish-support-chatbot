#!/usr/bin/env bash
# Creates the Neon project once, outside Terraform (ADR 0006), and stores the owner's connection
# string in Secret Manager without ever printing it. Safe to rerun: an existing project is kept,
# and the secret is only filled when it has no enabled version.
#
# Run from the repo root, after the apply that creates the `neon-owner-url` secret container:
#   bash infra/scripts/neon-bootstrap.sh
# Needs: .env with NEON_API_KEY (an organization key for this project's own Neon organization) and
# GCP_PROJECT_ID; gcloud logged in as the project owner; curl and python3.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${NEON_API_KEY:?set NEON_API_KEY in .env}" "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

API="https://console.neon.tech/api/v2"
NAME="spanish-support-chatbot"
REGION="aws-us-east-1" # same region as the plan: a few ms from Cloud Run us-east4 (ADR 0002)
DATABASE="chatbot"
OWNER="chatbot_owner"
SECRET="neon-owner-url"

neon() {
  curl -sS --fail-with-body -H "Authorization: Bearer $NEON_API_KEY" \
    -H "Accept: application/json" -H "Content-Type: application/json" "$@"
}

# Read one field from JSON on stdin by a dotted path (e.g. project.id, connection_uris.0.uri).
# It prints only that value, so a secret goes straight into the next command, never to the screen.
field() {
  python3 -c '
import json, sys
value = json.load(sys.stdin)
for key in sys.argv[1].split("."):
    value = value[int(key)] if isinstance(value, list) else value[key]
print(value)' "$1"
}

if ! gcloud secrets describe "$SECRET" --project "$GCP_PROJECT_ID" --format="value(name)" >/dev/null; then
  echo "The $SECRET secret container doesn't exist yet: merge and apply the gcp stack first." >&2
  exit 1
fi

project_id="$(neon "$API/projects?search=$NAME" | python3 -c '
import json, sys
matches = [p["id"] for p in json.load(sys.stdin)["projects"] if p["name"] == sys.argv[1]]
print(matches[0] if matches else "")' "$NAME")"

owner_url=""
if [ -z "$project_id" ]; then
  echo "Creating Neon project $NAME in $REGION..."
  # 0.25 CU fixed: the free plan's 100 CU-hours a month then last 400 compute hours, and the
  # compute still scales to zero when idle.
  response="$(neon -X POST "$API/projects" -d "{
    \"project\": {
      \"name\": \"$NAME\",
      \"region_id\": \"$REGION\",
      \"pg_version\": 17,
      \"default_endpoint_settings\": {
        \"autoscaling_limit_min_cu\": 0.25,
        \"autoscaling_limit_max_cu\": 0.25
      },
      \"branch\": {\"name\": \"main\", \"database_name\": \"$DATABASE\", \"role_name\": \"$OWNER\"}
    }
  }")"
  project_id="$(field project.id <<<"$response")"
  owner_url="$(field connection_uris.0.connection_uri <<<"$response")"
  unset response
  echo "Created project $project_id."
else
  echo "Neon project $NAME already exists ($project_id); leaving it as it is."
fi

enabled="$(gcloud secrets versions list "$SECRET" --project "$GCP_PROJECT_ID" \
  --filter="state:ENABLED" --format="value(name)" | wc -l | tr -d ' ')"
if [ "$enabled" = "0" ]; then
  if [ -z "$owner_url" ]; then
    owner_url="$(neon "$API/projects/$project_id/connection_uri?database_name=$DATABASE&role_name=$OWNER" |
      field uri)"
  fi
  printf %s "$owner_url" | gcloud secrets versions add "$SECRET" --project "$GCP_PROJECT_ID" \
    --data-file=- --quiet >/dev/null
  echo "Stored the owner connection string in Secret Manager ($SECRET)."
else
  echo "$SECRET already has an enabled version; not changed."
fi
unset owner_url
