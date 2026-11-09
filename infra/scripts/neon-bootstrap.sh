#!/usr/bin/env bash
# Creates the Neon project once, outside Terraform (ADR 0006), and stores the owner's connection
# string in Secret Manager without ever printing it. Safe to rerun: an existing project is kept,
# and a stored connection string isn't replaced.
#
# It also creates the `dev` branch, which the indexing workflow fills and checks before `main`
# (ADR 0007), and stores that branch's owner connection string beside `main`'s in `neon-owner-urls`.
# The branch is copied from `main`, so run this after the migrations that create the app roles.
#
# Run from the repo root, after the apply that creates the `neon-owner-urls` secret container:
#   bash infra/scripts/neon-bootstrap.sh
# To rotate the owner password on `main` (e.g. after it was exposed), which also destroys older
# versions (`dev` keeps its own password, copied when the branch was created):
#   bash infra/scripts/neon-bootstrap.sh --rotate-owner
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
SECRET="neon-owner-urls"
JSON="python3 infra/scripts/neon_secret_json.py"

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

# The ID of the branch with this name, or nothing.
branch_id() {
  neon "$API/projects/$project_id/branches" | python3 -c '
import json, sys
print(next((b["id"] for b in json.load(sys.stdin)["branches"] if b["name"] == sys.argv[1]), ""))' "$1"
}

# The secret's enabled versions.
enabled_versions() {
  gcloud secrets versions list "$SECRET" --project "$GCP_PROJECT_ID" \
    --filter="state:ENABLED" --format="value(name)" 2>/dev/null
}

# The secret's JSON, or an empty object before the first value.
current_urls() {
  if [ -n "$(enabled_versions)" ]; then
    gcloud secrets versions access latest --secret "$SECRET" --project "$GCP_PROJECT_ID"
  else
    printf '{}'
  fi
}

# Whether a branch's connection string is stored.
has_url() {
  current_urls | $JSON has "$1"
}

# Stores a branch's connection string beside the other one, and destroys the older versions, which
# would otherwise count against the free tier.
store_url() {
  local old_versions version current merged
  old_versions="$(enabled_versions)"
  # Captured, not piped straight on: a pipeline's stages run at once, so `versions add` would store
  # an empty version, which becomes `latest`, if reading or updating the JSON failed.
  current="$(current_urls)"
  merged="$(printf %s "$current" | NEW_VALUE="$2" $JSON set "$1")"
  printf %s "$merged" | gcloud secrets versions add "$SECRET" --project "$GCP_PROJECT_ID" \
    --data-file=- --quiet >/dev/null
  for version in $old_versions; do
    gcloud secrets versions destroy "$version" --secret "$SECRET" --project "$GCP_PROJECT_ID" --quiet >/dev/null
    echo "Destroyed the old version $version."
  done
}

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

rotate="${1:-}"
if [ "$rotate" = "--rotate-owner" ]; then
  main_id="$(branch_id main)"
  neon -X POST "$API/projects/$project_id/branches/$main_id/roles/$OWNER/reset_password" >/dev/null
  echo "Reset the $OWNER password."
  sleep 5 # the reset is applied asynchronously
  owner_url="$(neon "$API/projects/$project_id/connection_uri?database_name=$DATABASE&role_name=$OWNER" |
    field uri)"
  store_url main "$owner_url"
  unset owner_url
  echo "Stored the new owner connection string in Secret Manager ($SECRET)."
  exit 0
fi

if ! has_url main; then
  if [ -z "$owner_url" ]; then
    owner_url="$(neon "$API/projects/$project_id/connection_uri?database_name=$DATABASE&role_name=$OWNER" |
      field uri)"
  fi
  store_url main "$owner_url"
  echo "Stored the owner connection string in Secret Manager ($SECRET)."
else
  echo "$SECRET already has main's connection string; not changed."
fi
unset owner_url

# The dev branch: a copy of main with its own compute, fixed at 0.25 CU like main's. It runs only
# while the indexing workflow does and then scales to zero, so it uses a few minutes of the free
# compute hours per run. Indexing is idempotent, so dev never needs resetting (which would need
# this API key in CI).
# Its connection string is always asked for separately: unlike creating a project, creating a
# branch doesn't return one.
dev_id="$(branch_id dev)"
if [ -z "$dev_id" ]; then
  echo "Creating the dev branch from main..."
  dev_id="$(neon -X POST "$API/projects/$project_id/branches" -d "{
    \"branch\": {\"name\": \"dev\", \"parent_id\": \"$(branch_id main)\"},
    \"endpoints\": [{
      \"type\": \"read_write\",
      \"autoscaling_limit_min_cu\": 0.25,
      \"autoscaling_limit_max_cu\": 0.25
    }]
  }" | field branch.id)"
  echo "Created the dev branch ($dev_id)."
else
  echo "The dev branch already exists ($dev_id); leaving it as it is."
fi

if ! has_url dev; then
  dev_url="$(neon "$API/projects/$project_id/connection_uri?branch_id=$dev_id&database_name=$DATABASE&role_name=$OWNER" |
    field uri)"
  store_url dev "$dev_url"
  echo "Stored the dev owner connection string in Secret Manager ($SECRET)."
else
  echo "$SECRET already has dev's connection string; not changed."
fi
unset dev_url
