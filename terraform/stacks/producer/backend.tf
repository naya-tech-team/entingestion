# Partial configuration: the state location comes from envs/<env>/producer.s3.tfbackend.
#   terraform init -backend-config=../../envs/<env>/producer.s3.tfbackend
# Use the Makefile (make plan ENV=<env> STACK=producer), which always pairs backend and tfvars.
terraform {
  backend "s3" {}
}
