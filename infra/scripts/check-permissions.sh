#!/usr/bin/env bash
# Checks what the CI service accounts can and cannot do, using Policy Troubleshooter (read-only).
# Run from the repo root after any change to infra/terraform/bootstrap:
#   bash infra/scripts/check-permissions.sh
# Needs: gcloud logged in as the project owner, .env with GCP_PROJECT_ID and TF_STATE_BUCKET, and
# policytroubleshooter.googleapis.com enabled. Exits non-zero if any check fails.
set -uo pipefail

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env}" "${TF_STATE_BUCKET:?set TF_STATE_BUCKET in .env}"

PROJECT="//cloudresourcemanager.googleapis.com/projects/$GCP_PROJECT_ID"
BUCKET="//storage.googleapis.com/projects/_/buckets/$TF_STATE_BUCKET"
failures=0

# check ACCOUNT PERMISSION EXPECTED(yes|no|cond) RESOURCE
check() {
  local got status attempt
  for attempt in 1 2 3; do  # the API rate-limits bursts; retry transient errors
    got=$(gcloud policy-intelligence troubleshoot-policy iam "$4" \
      --principal-email="$1@$GCP_PROJECT_ID.iam.gserviceaccount.com" \
      --permission="$2" --format='value(overallAccessState)' 2>/dev/null | tail -1)
    [[ -n "$got" ]] && break
    sleep $((attempt * 5))
  done
  case "$3:$got" in
    yes:CAN_ACCESS | no:CANNOT_ACCESS | cond:UNKNOWN_CONDITIONAL) status=PASS ;;
    *) status=FAIL; failures=$((failures + 1)) ;;
  esac
  printf '%-4s %-9s %-42s want=%-4s got=%s\n' "$status" "$1" "$2" "$3" "${got:-error}"
}

echo "== tf-apply: what the gcp stack needs"
for p in artifactregistry.repositories.create run.services.create run.services.setIamPolicy \
  iam.serviceAccounts.create iam.serviceAccounts.actAs datastore.databases.create \
  secretmanager.secrets.create secretmanager.secrets.setIamPolicy bigquery.datasets.create \
  serviceusage.services.enable; do
  check tf-apply "$p" yes "$PROJECT"
done
check tf-apply resourcemanager.projects.setIamPolicy cond "$PROJECT"
check tf-apply storage.objects.create yes "$BUCKET"

echo "== tf-apply: must never have"
for p in resourcemanager.projects.delete iam.serviceAccountKeys.create \
  secretmanager.versions.access secretmanager.versions.add iam.workloadIdentityPools.update \
  storage.buckets.delete; do
  check tf-apply "$p" no "$PROJECT"
done

echo "== tf-plan: read-only"
for p in run.services.get resourcemanager.projects.getIamPolicy; do
  check tf-plan "$p" yes "$PROJECT"
done
for p in run.services.create artifactregistry.repositories.create \
  resourcemanager.projects.setIamPolicy iam.serviceAccounts.actAs secretmanager.versions.access; do
  check tf-plan "$p" no "$PROJECT"
done
check tf-plan storage.objects.get yes "$BUCKET"
check tf-plan storage.objects.create no "$BUCKET"

echo
if ((failures)); then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
