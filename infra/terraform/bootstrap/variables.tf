variable "project_id" {
  description = "GCP project that hosts the app and the Terraform state (the billed chatbot-demo project)."
  type        = string
}

variable "region" {
  description = "Default region for regional resources."
  type        = string
  default     = "us-east4"
}

variable "state_bucket_location" {
  description = "Location of the state bucket. Cloud Storage's always-free tier covers us-east1, us-west1 and us-central1 only."
  type        = string
  default     = "us-east1"
}

variable "github_repository" {
  description = "owner/name of the repository whose GitHub Actions may authenticate."
  type        = string
  default     = "ramirez-ai-labs/spanish-support-chatbot"
}

variable "github_repository_id" {
  description = "Numeric GitHub repository ID. Trust is pinned to IDs, so a deleted and re-created repo with the same name cannot authenticate."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be the numeric repository ID (gh api repos/OWNER/REPO --jq .id)."
  }
}

variable "github_repository_owner_id" {
  description = "Numeric GitHub ID of the repository owner (the organization)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_owner_id))
    error_message = "github_repository_owner_id must be numeric (gh api repos/OWNER/REPO --jq .owner.id)."
  }
}

variable "labels" {
  description = "Labels applied to every resource that supports them."
  type        = map(string)
  default     = { project = "showcase" }
}
