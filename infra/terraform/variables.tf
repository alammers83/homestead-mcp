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

variable "enabled_api_keys" {
  description = "API keys to wire into the service from Secret Manager: airnow, pollen, tmdb, google-oauth. Add a secret version before listing one."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for k in var.enabled_api_keys : contains(["airnow", "pollen", "tmdb", "google-oauth"], k)])
    error_message = "enabled_api_keys may contain only airnow, pollen, tmdb, google-oauth."
  }
}

variable "public_url" {
  description = "The service's https URL (from `terraform output service_url`). Setting it turns on OAuth and opens the service to the internet, with OAuth as the gate. Empty keeps it IAM-only."
  type        = string
  default     = ""

  validation {
    condition     = var.public_url == "" || startswith(var.public_url, "https://")
    error_message = "public_url must start with https://."
  }
}

variable "google_oauth_client_id" {
  description = "OAuth client ID (Web application) from Google Cloud console > APIs & Services > Credentials."
  type        = string
  default     = ""
}

variable "allowed_emails" {
  description = "Google accounts allowed to sign in. Everyone else is refused."
  type        = list(string)
  default     = []
}
