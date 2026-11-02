terraform {
  # Same version as .terraform-version; CI and every laptop must match.
  required_version = "~> 1.17.1"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.9"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.13"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Reads GITHUB_TOKEN from the environment: your own `gh auth token`, used only for this local apply.
# CI never manages GitHub settings, so no repo-admin token ever reaches it.
provider "github" {
  owner = var.github_owner
}
