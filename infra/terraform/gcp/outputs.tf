output "image_repository" {
  description = "Docker repository path for images, e.g. <this>/api:<sha>."
  value       = "${google_artifact_registry_repository.app.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}

output "gemini_secret" {
  description = "Secret Manager secret for the Gemini API key. Add its value by hand: infra/BOOTSTRAP.md, section 6."
  value       = google_secret_manager_secret.gemini_api_key.secret_id
}

output "runtime_service_account" {
  description = "Service account the API runs as on Cloud Run."
  value       = google_service_account.run.email
}

output "api_service" {
  description = "Cloud Run service name the deploy step updates."
  value       = google_cloud_run_v2_service.api.name
}

output "sign_in_api_key" {
  description = "The chat page's browser key for sign-in. Public by design but restricted; marked sensitive only to keep it out of logs."
  value       = google_apikeys_key.chat_page.key_string
  sensitive   = true
}
