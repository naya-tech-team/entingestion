terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
      # aws.copy: the region the backups are copied to (disaster recovery).
      configuration_aliases = [aws.copy]
    }
  }
}
