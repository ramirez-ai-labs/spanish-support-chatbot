# Store these as GitHub Actions variables (not secrets; they grant nothing on their own) and keep
# them out of committed files: infra/BOOTSTRAP.md, section 5.

output "state_bucket" {
  description = "GCS bucket holding Terraform state for every GCP-backed stack. Actions variable: TF_STATE_BUCKET."
  value       = google_storage_bucket.tfstate.name
}

output "workload_identity_provider" {
  description = "Full provider name for google-github-actions/auth. Actions variable: GCP_WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "plan_service_account" {
  description = "Read-only account for PR plans. Actions variable: GCP_PLAN_SA."
  value       = google_service_account.plan.email
}

output "apply_service_account" {
  description = "Account for applies and deploys from main. Actions variable: GCP_APPLY_SA."
  value       = google_service_account.apply.email
}
