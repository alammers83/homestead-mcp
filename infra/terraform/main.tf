locals {
  apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ]
  labels = { app = "homestead", managed-by = "terraform" }

  # Optional third-party API keys: secret suffix -> env var the app reads.
  api_keys = {
    airnow = "HOMESTEAD_AIRNOW_API_KEY"
    pollen = "HOMESTEAD_POLLEN_API_KEY"
    tmdb   = "HOMESTEAD_TMDB_TOKEN"
  }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- image registry

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.service_name
  format        = "DOCKER"
  labels        = local.labels

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- runtime identity (least privilege)

resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-runtime"
  display_name = "Homestead MCP runtime"
}

# ---------------------------------------------------------------- state: audit log, approvals, care log

resource "google_storage_bucket" "state" {
  name                        = "${var.project_id}-${var.service_name}-state"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true # the audit log can't be silently rewritten without leaving a prior version
  }
}

resource "google_storage_bucket_iam_member" "runtime_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = google_service_account.runtime.member
}

# ---------------------------------------------------------------- config + secrets

# The real household.yaml never goes in git. Upload it as a secret version:
#   gcloud secrets versions add homestead-household-config --data-file=config/household.yaml
resource "google_secret_manager_secret" "household_config" {
  secret_id = "${var.service_name}-household-config"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "sitter_webhook" {
  secret_id = "${var.service_name}-sitter-webhook"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# API keys: create a version for each one you use, then list it in var.enabled_api_keys.
#   printf %s "$KEY" | gcloud secrets versions add homestead-airnow-key --data-file=-
resource "google_secret_manager_secret" "api_keys" {
  for_each  = local.api_keys
  secret_id = "${var.service_name}-${each.key}-key"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_api_keys" {
  for_each  = google_secret_manager_secret.api_keys
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

resource "google_secret_manager_secret_iam_member" "runtime_config" {
  secret_id = google_secret_manager_secret.household_config.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

resource "google_secret_manager_secret_iam_member" "runtime_webhook" {
  secret_id = google_secret_manager_secret.sitter_webhook.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

# ---------------------------------------------------------------- Cloud Run

resource "google_cloud_run_v2_service" "homestead" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # reachable, but IAM-gated: no allUsers invoker anywhere
  labels   = local.labels

  template {
    service_account       = google_service_account.runtime.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2" # required for the GCS volume

    scaling {
      min_instance_count = 0
      max_instance_count = 1 # single writer for the JSONL logs on the GCS mount
    }

    containers {
      image = var.bootstrap_image

      ports {
        container_port = 8080
      }

      env {
        name  = "HOMESTEAD_CONFIG"
        value = "/config/household.yaml"
      }
      env {
        name  = "HOMESTEAD_STATE_DIR"
        value = "/state"
      }

      dynamic "env" {
        for_each = var.sitter_webhook_enabled ? [1] : []
        content {
          name = "HOMESTEAD_SITTER_WEBHOOK"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.sitter_webhook.secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "env" {
        for_each = toset(var.enabled_api_keys)
        content {
          name = local.api_keys[env.value]
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.api_keys[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      volume_mounts {
        name       = "config"
        mount_path = "/config"
      }
      volume_mounts {
        name       = "state"
        mount_path = "/state"
      }
    }

    volumes {
      name = "config"
      secret {
        secret = google_secret_manager_secret.household_config.secret_id
        items {
          version = "latest"
          path    = "household.yaml"
        }
      }
    }

    volumes {
      name = "state"
      gcs {
        bucket    = google_storage_bucket.state.name
        read_only = false
      }
    }
  }

  lifecycle {
    # CI owns the running image; Terraform owns everything else.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.runtime_config,
    google_secret_manager_secret_iam_member.runtime_api_keys,
    google_storage_bucket_iam_member.runtime_state,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = toset(var.invoker_members)
  name     = google_cloud_run_v2_service.homestead.name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value
}

# ---------------------------------------------------------------- GitHub Actions deploys via Workload Identity Federation
# No service-account keys exist anywhere. Only workflows in var.github_repo can impersonate the deployer.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Only this repo, and only the main branch, can get credentials.
  attribute_condition = "assertion.repository == \"${var.github_repo}\" && assertion.ref == \"refs/heads/main\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  account_id   = "${var.service_name}-deployer"
  display_name = "Homestead GitHub deployer"
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

resource "google_cloud_run_v2_service_iam_member" "deployer_run" {
  name     = google_cloud_run_v2_service.homestead.name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}
