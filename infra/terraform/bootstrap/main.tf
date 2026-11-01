# Bootstrap: the pieces Terraform-in-CI needs before it can manage anything else.
# Applied locally, once (infra/BOOTSTRAP.md, section 5). Azure and AWS are added in M7 and M8.

locals {
  services = [
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # short-lived tokens for impersonation
    "sts.googleapis.com",            # exchanges GitHub OIDC tokens
    "storage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
  ]

  # Only these GitHub events on main may act as the apply account. pull_request runs get
  # refs/pull/N/merge, and other branches get their own ref, so neither matches.
  apply_ref_events = ["refs/heads/main@push", "refs/heads/main@workflow_dispatch"]
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  service            = each.value
  disable_on_destroy = false
}

# --- Terraform state -----------------------------------------------------------------------------

resource "google_storage_bucket" "tfstate" {
  name     = "${var.project_id}-tfstate"
  location = var.state_bucket_location
  labels   = var.labels

  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  # Keep recent state history for recovery without growing past the free tier.
  lifecycle_rule {
    condition {
      num_newer_versions = 10
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.this]
}

# --- Keyless GitHub Actions login (Workload Identity Federation) ---------------------------------

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "OIDC trust for ${var.github_repository} only"

  depends_on = [google_project_service.this]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions OIDC"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref_event"           = "assertion.ref + '@' + assertion.event_name"
  }

  # Pinned to numeric IDs, so a renamed, transferred or re-created repo cannot authenticate.
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
  ])
}

# --- Service accounts CI impersonates ------------------------------------------------------------

# Plan: read-only, usable from any workflow run in this repo (including PR checks).
resource "google_service_account" "plan" {
  account_id   = "tf-plan"
  display_name = "Terraform plan (CI, read-only)"
}

# Apply: changes infrastructure; usable only from pushes and manual runs on main.
resource "google_service_account" "apply" {
  account_id   = "tf-apply"
  display_name = "Terraform apply (CI, main only)"
}

resource "google_service_account_iam_member" "plan_wif" {
  service_account_id = google_service_account.plan.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_service_account_iam_member" "apply_wif" {
  for_each = toset(local.apply_ref_events)

  service_account_id = google_service_account.apply.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.ref_event/${each.value}"
}

resource "google_project_iam_member" "plan" {
  for_each = toset([
    "roles/viewer",
    "roles/iam.securityReviewer", # read IAM policies so plans can diff them
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.plan.email}"
}

# Broad while the gcp stack takes shape; narrowed to the roles it actually uses in M5.
resource "google_project_iam_member" "apply" {
  for_each = toset([
    "roles/editor",
    "roles/resourcemanager.projectIamAdmin",
    "roles/iam.serviceAccountAdmin",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.apply.email}"
}

# Plans run with -lock=false, so the plan account only reads state and a PR can never write it.
resource "google_storage_bucket_iam_member" "plan_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.plan.email}"
}

resource "google_storage_bucket_iam_member" "apply_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.apply.email}"
}
