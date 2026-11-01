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
