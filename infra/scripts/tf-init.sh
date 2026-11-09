#!/usr/bin/env bash
# `terraform init` for one stack, retried: providers download from GitHub releases, whose 504s
# failed CI and the prod apply. Uses the provider cache that CI restores, when TF_PLUGIN_CACHE_DIR
# is set; init still checks every provider against the stack's lock file.
#
#   bash infra/scripts/tf-init.sh <stack dir> [extra init arguments...]
set -euo pipefail

dir="$1"
shift
if [ -n "${TF_PLUGIN_CACHE_DIR:-}" ]; then mkdir -p "$TF_PLUGIN_CACHE_DIR"; fi

for attempt in 1 2 3 4; do
  if terraform -chdir="$dir" init -input=false -lockfile=readonly "$@"; then exit 0; fi
  if [ "$attempt" = 4 ]; then
    echo "::error::terraform init of $dir failed 4 times."
    exit 1
  fi
  echo "init of $dir failed (attempt $attempt); retrying in $((attempt * 15))s"
  sleep $((attempt * 15))
done
