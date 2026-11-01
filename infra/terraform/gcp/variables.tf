variable "project_id" {
  description = "GCP project for the app (the billed chatbot-demo project). Set via TF_VAR_project_id."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run and the image registry (same region keeps image pulls free)."
  type        = string
  default     = "us-east4"
}

variable "labels" {
  description = "Labels applied to every resource that supports them."
  type        = map(string)
  default     = { project = "showcase" }
}
