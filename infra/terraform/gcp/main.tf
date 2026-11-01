# App infrastructure on GCP, planned on every PR and applied from main after approval
# (.github/workflows/terraform.yml). Resources are added one milestone slice at a time; each must
# fit an always-free tier before it is added.

resource "google_project_service" "artifactregistry" {
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

# Container images for Cloud Run. Always free up to 0.5 GB of storage, so only the newest images
# are kept; pulls from Cloud Run in the same region are free.
resource "google_artifact_registry_repository" "app" {
  repository_id = "app"
  location      = var.region
  format        = "DOCKER"
  description   = "spanish-support-chatbot images, deployed to Cloud Run"
  labels        = var.labels

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-latest-3"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }

  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      older_than = "86400s"
    }
  }

  depends_on = [google_project_service.artifactregistry]
}

# --- Gemini key and the app's runtime identity -----------------------------------------------------

resource "google_project_service" "secretmanager" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

# Container only: the value is added by hand (infra/BOOTSTRAP.md, section 6), never by Terraform,
# because state is readable by the plan account. Always free up to 6 active versions and 10,000
# accesses a month; one replica in the app's region keeps it to one billable location.
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "gemini-api-key"
  labels    = var.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.secretmanager]
}

# Identity the API runs as on Cloud Run, instead of the default compute account (which has editor).
resource "google_service_account" "run" {
  account_id   = "chatbot-run"
  display_name = "Chatbot API on Cloud Run (runtime)"
}

# Read access to this one secret only, never project-wide.
resource "google_secret_manager_secret_iam_member" "run_gemini_api_key" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# Project roles for the runtime account. Each must be in the bootstrap stack's grantable_roles, or
# the apply is refused; tracing and metrics are added with the slices that use them.
resource "google_project_iam_member" "run" {
  for_each = toset(["roles/logging.logWriter"])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.run.email}"
}
