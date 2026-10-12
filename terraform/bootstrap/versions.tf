terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # This stack creates the state bucket, so its first apply keeps local state. After that, move
  # the state into the bucket (README.md "Move this stack's state into S3"): uncomment the block
  # below and run `terraform init -migrate-state -backend-config=bootstrap.s3.tfbackend`.
  # Until then keep terraform.tfstate out of git; re-create it with `terraform import` if lost.
  #
  # backend "s3" {}
}
