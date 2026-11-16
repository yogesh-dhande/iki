terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.73, < 7.0"
      # aws.dr is the disaster-recovery region, where backups are copied.
      configuration_aliases = [aws.dr]
    }
  }
}
