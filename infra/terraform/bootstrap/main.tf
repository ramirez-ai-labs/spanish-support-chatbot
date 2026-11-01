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

  # Only jobs on main that run in the approval-gated `prod` GitHub environment may act as the
  # apply account. pull_request runs get refs/pull/N/merge, other branches their own ref, and
  # jobs without the environment get "none", so none of them match.
  apply_gates = [
    "refs/heads/main@push@prod",
    "refs/heads/main@workflow_dispatch@prod",
  ]

  # What the apply account needs for the gcp stack (Cloud Run, Artifact Registry, Firestore,
  # Secret Manager, BigQuery) instead of roles/editor.
  apply_roles = [
    "roles/run.admin",
    "roles/artifactregistry.admin",
    "roles/datastore.owner",
    "roles/secretmanager.admin",
    "roles/bigquery.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/iam.serviceAccountAdmin", # create runtime service accounts
    "roles/iam.serviceAccountUser",  # deploy Cloud Run revisions as those accounts
  ]

  # The only project roles the apply account may grant (to runtime service accounts). It can
  # never grant owner, editor or IAM admin, including to itself.
  grantable_roles = [
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/monitoring.metricWriter",
    "roles/datastore.user",
    "roles/secretmanager.secretAccessor",
    "roles/bigquery.dataEditor",
    "roles/bigquery.jobUser",
    "roles/artifactregistry.reader",
    "roles/run.invoker",
  ]

  # Data Access audit logs for the APIs CI uses to log in, so every token exchange is recorded.
  # iamcredentials.googleapis.com rejects service-level audit config; its logs are controlled
  # through iam.googleapis.com.
  audited_services = ["sts.googleapis.com", "iam.googleapis.com"]
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
    "attribute.gate"                = "assertion.ref + '@' + assertion.event_name + '@' + ('environment' in assertion ? assertion.environment : 'none')"
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
  for_each = toset(local.apply_gates)

  service_account_id = google_service_account.apply.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.gate/${each.value}"
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

resource "google_project_iam_member" "apply" {
  for_each = toset(local.apply_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.apply.email}"
}

# IAM admin limited by condition: the apply account can add or remove only grantable_roles.
resource "google_project_iam_member" "apply_grant_runtime_roles" {
  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.apply.email}"

  condition {
    title       = "runtime-roles-only"
    description = "May grant only the runtime roles listed in the bootstrap stack"
    expression  = "api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly([${join(", ", formatlist("'%s'", local.grantable_roles))}])"
  }
}

# --- Audit -----------------------------------------------------------------------------------------

# Admin Activity logs are always on; these add Data Access logs for CI logins.
resource "google_project_iam_audit_config" "ci_login" {
  for_each = toset(local.audited_services)

  project = var.project_id
  service = each.value

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
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
