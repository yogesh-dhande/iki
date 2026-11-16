terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.73, < 7.0"
    }
  }

  # The state lives in the bootstrap's bucket, locked with S3's own lock file. Its bucket
  # and region come from backend.hcl: tofu init -backend-config=backend.hcl.
  backend "s3" {
    key          = "iki/production.tfstate"
    use_lockfile = true
    encrypt      = true
  }

  # And it's encrypted by OpenTofu before it's written, with the bootstrap's key: the
  # bucket's own encryption doesn't stop anyone who can read the bucket.
  encryption {
    key_provider "aws_kms" "state" {
      kms_key_id = var.state_kms_key_arn
      region     = var.region
      key_spec   = "AES_256"
    }
    method "aes_gcm" "state" {
      keys = key_provider.aws_kms.state
    }
    state {
      method   = method.aes_gcm.state
      enforced = true
    }
    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = var.tags
  }
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region
  default_tags {
    tags = var.tags
  }
}
