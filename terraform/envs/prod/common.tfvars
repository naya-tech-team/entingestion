# Shared by every stack in prod (the Makefile passes it before envs/prod/<stack>.tfvars, which may
# override a value). Usage: make plan ENV=prod STACK=<stack>

# Where the stacks deploy. The Terraform state bucket's region is separate: STATE_REGION
# (default AWS_REGION), the region terraform/bootstrap was applied in.
region = "us-west-2"

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "" # required: the date of the first prod apply
  owner                = "naveen"
  internal_client_code = "" # required: fill in
  client               = "" # required: fill in
  project_name         = "entingestion"
  division             = "" # required: fill in
}
