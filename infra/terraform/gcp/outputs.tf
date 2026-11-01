output "image_repository" {
  description = "Docker repository path for images, e.g. <this>/api:<sha>."
  value       = "${google_artifact_registry_repository.app.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}
