terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.11, < 9.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.14, < 1.0"
    }
  }
}
