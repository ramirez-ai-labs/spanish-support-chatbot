# Store these as GitHub Actions secrets, never variables: Actions logs are public and GitHub masks
# only secrets. Keep them out of committed files: infra/BOOTSTRAP.md, section 5.

output "state_bucket" {
  description = "GCS bucket holding Terraform state for every GCP-backed stack. Actions secret: TF_STATE_BUCKET."
  value       = google_storage_bucket.tfstate.name
}

output "workload_identity_provider" {
  description = "Full provider name for google-github-actions/auth. Actions secret: GCP_WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "plan_service_account" {
  description = "Read-only account for PR plans. Actions secret: GCP_PLAN_SA."
  value       = google_service_account.plan.email
}

output "apply_service_account" {
  description = "Account for applies and deploys from main. Actions secret: GCP_APPLY_SA."
  value       = google_service_account.apply.email
}
