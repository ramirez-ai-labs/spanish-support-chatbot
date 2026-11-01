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

resource "google_project_service" "cloudtrace" {
  service            = "cloudtrace.googleapis.com"
  disable_on_destroy = false
}

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
# the apply is refused; metrics are added with the slice that uses them.
resource "google_project_iam_member" "run" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/cloudtrace.agent", # write spans; always free up to 2.5M spans a month
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.run.email}"
}

# --- Cloud Run -------------------------------------------------------------------------------------

resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

# The API. Terraform owns the service and its settings; CI owns the image (app rollouts are not
# Terraform), so the image starts as Google's placeholder and later changes to it are ignored.
# Always free on request-based billing: 2M requests, 180k vCPU-seconds and 360k GiB-seconds a
# month. One instance at most and scale to zero bound both cost and blast radius.
resource "google_cloud_run_v2_service" "api" {
  name                = "chatbot-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = var.labels
  deletion_protection = false # recreated from code in minutes; nothing stateful lives here

  template {
    service_account                  = google_service_account.run.email
    timeout                          = "300s" # long enough for a streamed answer
    max_instance_request_concurrency = 20
    labels                           = var.labels

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true # CPU only while handling requests: request-based billing
        startup_cpu_boost = false
      }

      env {
        name  = "LLM_PROVIDER"
        value = "gemini"
      }
      env {
        name  = "GEMINI_MODEL"
        value = "gemini-3.5-flash-lite"
      }
      env {
        name  = "TRACE_EXPORTER"
        value = "gcp"
      }
      env {
        name  = "TRACE_SAMPLE_RATIO"
        value = "1.0" # every request at demo traffic; 2 spans each stays far inside the free tier
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image, # set by the deploy step
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.run,
    google_project_service.cloudtrace,
    google_secret_manager_secret_iam_member.run_gemini_api_key,
  ]
}

# Public: the chat is for anonymous visitors too. Sign-in tiers are enforced in the app (#18).
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
