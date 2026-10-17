terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.8"
    }
  }

  # Remote state. Create the bucket once by hand, then uncomment:
  #   gcloud storage buckets create gs://<project>-tfstate --location=us-east4 \
  #     --uniform-bucket-level-access --public-access-prevention
  # backend "gcs" {
  #   bucket = "<project>-tfstate"
  #   prefix = "homestead"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
