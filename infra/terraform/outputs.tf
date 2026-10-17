output "service_url" {
  value = google_cloud_run_v2_service.homestead.uri
}

output "image_repository" {
  description = "Set as GitHub variable IMAGE_REPO."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "workload_identity_provider" {
  description = "Set as GitHub variable WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  description = "Set as GitHub variable DEPLOYER_SA."
  value       = google_service_account.deployer.email
}
