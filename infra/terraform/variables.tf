variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and the state bucket."
  type        = string
  default     = "us-east4" # Northern Virginia
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "homestead"
}

variable "github_repo" {
  description = "GitHub repo allowed to deploy, as owner/name. Only this repo's workflows can impersonate the deployer."
  type        = string
}

variable "invoker_members" {
  description = "Principals allowed to call the service, e.g. [\"user:you@example.com\"]. The service is never public."
  type        = list(string)
}

variable "bootstrap_image" {
  description = "Placeholder image for the first apply. CI deploys the real image afterwards; Terraform ignores image drift."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "sitter_webhook_enabled" {
  description = "Wire HOMESTEAD_SITTER_WEBHOOK from Secret Manager. Add a secret version before enabling."
  type        = bool
  default     = false
}
