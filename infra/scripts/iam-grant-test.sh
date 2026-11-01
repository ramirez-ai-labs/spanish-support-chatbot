#!/usr/bin/env bash
# Layer 3: proves the apply account can grant only the runtime roles in the bootstrap stack's
# grantable_roles. Policy Troubleshooter cannot evaluate that condition (it answers "conditional"),
# so this makes real grants to a throwaway service account and expects the forbidden ones refused.
#
# Runs in CI as tf-apply: .github/workflows/iam-grant-test.yml. It refuses to run as anyone else,
# because the project owner is not limited by the condition and every grant would succeed.
# Grants only ever go to the throwaway account, which is deleted at the end. Exits non-zero if
# any check fails or is inconclusive.
set -uo pipefail

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}" "${RUN_ID:?set RUN_ID}"

# google-github-actions/auth writes a credential file that impersonates the account it logged in as.
if ! grep -q 'serviceAccounts/tf-apply@' "${CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE:-/dev/null}"; then
  echo "::error::Not logged in as tf-apply. Run this from the iam-grant-test workflow, not locally."
  exit 1
fi

SA="iam-grant-test-$RUN_ID@$GCP_PROJECT_ID.iam.gserviceaccount.com"
MEMBER="serviceAccount:$SA"
ALLOWED=roles/logging.logWriter # in grantable_roles
FORBIDDEN=(
  roles/owner
  roles/editor
  roles/resourcemanager.projectIamAdmin # would let it rewrite its own limit
  roles/iam.serviceAccountTokenCreator  # would let it impersonate other accounts
  roles/secretmanager.secretAccessor    # secrets are granted per secret, never project-wide
)
failures=0

grant() {
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="$MEMBER" --role="$1" \
    --condition=None --format=none --quiet 2>&1
}

revoke() {
  gcloud projects remove-iam-policy-binding "$GCP_PROJECT_ID" --member="$MEMBER" --role="$1" \
    --condition=None --format=none --quiet 2>&1
}

roles_held() {
  gcloud projects get-iam-policy "$GCP_PROJECT_ID" --flatten='bindings[].members' \
    --filter="bindings.members=\"$MEMBER\"" --format='value(bindings.role)'
}

is_denied() { grep -qiE 'PERMISSION_DENIED|does not have permission|access denied' <<<"$1"; }

# record STATUS WHAT WANT GOT
record() {
  [[ "$1" == PASS ]] || failures=$((failures + 1))
  printf '%-4s %-45s want=%-8s got=%s\n' "$1" "$2" "$3" "$4"
}

cleanup() {
  local role
  for role in $(roles_held); do
    revoke "$role" >/dev/null || echo "::error::Could not remove $role from the test account; remove it by hand."
  done
  gcloud iam service-accounts delete "$SA" --project="$GCP_PROJECT_ID" --quiet >/dev/null 2>&1 ||
    echo "::warning::Could not delete the test account; delete iam-grant-test-$RUN_ID by hand."
}

if ! gcloud iam service-accounts create "iam-grant-test-$RUN_ID" --project="$GCP_PROJECT_ID" \
  --display-name="IAM grant test (deleted by the run)" --format=none --quiet; then
  echo "::error::Could not create the test account."
  exit 1
fi
trap cleanup EXIT

echo "== allowed: a runtime role"
# A new account takes a few seconds to become visible to IAM; retry until then.
for attempt in 1 2 3 4 5 6; do
  if out=$(grant "$ALLOWED"); then
    record PASS "grant $ALLOWED" granted granted
    break
  fi
  if is_denied "$out" || ((attempt == 6)); then
    record FAIL "grant $ALLOWED" granted "$(head -1 <<<"$out")"
    break
  fi
  sleep 10
done

echo "== forbidden: must be refused"
for role in "${FORBIDDEN[@]}"; do
  if out=$(grant "$role"); then
    record FAIL "grant $role" denied GRANTED
    echo "::error::tf-apply granted $role. The grant limit is broken; reverting."
    revoke "$role" >/dev/null || echo "::error::Revert failed; remove $role from the test account by hand."
  elif is_denied "$out"; then
    record PASS "grant $role" denied denied
  else
    record FAIL "grant $role" denied "inconclusive: $(head -1 <<<"$out")"
  fi
done

echo "== allowed: removing the runtime role"
if out=$(revoke "$ALLOWED"); then
  record PASS "remove $ALLOWED" removed removed
else
  record FAIL "remove $ALLOWED" removed "$(head -1 <<<"$out")"
fi

held=$(roles_held | tr '\n' ' ')
if [[ -z "$held" ]]; then
  record PASS "test account holds no roles" none none
else
  record FAIL "test account holds no roles" none "$held"
fi

echo
if ((failures)); then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
