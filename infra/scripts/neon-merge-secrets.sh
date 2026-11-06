#!/usr/bin/env bash
# One-time step: copies the Neon connection strings from their separate secrets into the merged ones
# (ADR 0006), without any of them reaching the terminal, a file or shell history. No password
# changes, and the old secrets are left as they are, so nothing that reads them is affected.
#
# Run from the repo root, after the apply that creates the merged secrets' containers:
#   bash infra/scripts/neon-merge-secrets.sh
# Safe to rerun: a merged secret that already has a value is left alone.
# Needs: .env with GCP_PROJECT_ID; gcloud logged in as the project owner; python3.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}"

JSON="python3 infra/scripts/neon_secret_json.py"

access() {
  gcloud secrets versions access latest --secret "$1" --project "$GCP_PROJECT_ID"
}

# merge <merged secret> <key=old secret>...
merge() {
  local target="$1" merged='{}' pair key source value
  shift

  if ! gcloud secrets describe "$target" --project "$GCP_PROJECT_ID" --format="value(name)" >/dev/null; then
    echo "The $target secret container doesn't exist yet: merge and apply the gcp stack first." >&2
    exit 1
  fi
  if [ -n "$(gcloud secrets versions list "$target" --project "$GCP_PROJECT_ID" \
    --filter="state:ENABLED" --format="value(name)")" ]; then
    echo "$target already has a value; not changed."
    return
  fi

  for pair in "$@"; do
    key="${pair%%=*}"
    source="${pair#*=}"
    value="$(access "$source")"
    merged="$(printf %s "$merged" | NEW_VALUE="$value" $JSON set "$key")"
  done
  printf %s "$merged" | gcloud secrets versions add "$target" --project "$GCP_PROJECT_ID" \
    --data-file=- --quiet >/dev/null

  # Read it back, so a bad copy is caught while the old secrets still serve everything.
  for pair in "$@"; do
    key="${pair%%=*}"
    source="${pair#*=}"
    if [ "$(access "$target" | $JSON get "$key")" != "$(access "$source")" ]; then
      echo "$target's $key doesn't match $source." >&2
      exit 1
    fi
  done
  echo "Stored and checked $target ($# values)."
}

merge neon-owner-urls main=neon-owner-url dev=neon-owner-dev-url
merge neon-indexer-urls main=neon-indexer-url dev=neon-indexer-dev-url
merge neon-api-urls usage_writer=neon-usage-writer-url app_gcp=neon-app-gcp-url
