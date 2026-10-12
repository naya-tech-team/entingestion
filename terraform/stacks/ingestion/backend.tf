# Partial configuration: the state location comes from envs/<env>/ingestion.s3.tfbackend.
# Use the Makefile (make plan ENV=<env> STACK=ingestion), which always pairs backend and tfvars.
terraform {
  backend "s3" {}
}
