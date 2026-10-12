# Partial configuration: the state location comes from envs/<env>/ui.s3.tfbackend.
#   terraform init -backend-config=../../envs/<env>/ui.s3.tfbackend
# Use the Makefile (make plan ENV=<env> STACK=ui), which always pairs backend and tfvars.
terraform {
  backend "s3" {}
}
