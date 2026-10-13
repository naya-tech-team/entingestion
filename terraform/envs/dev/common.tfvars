# Shared by every stack in dev (the Makefile passes it before envs/dev/<stack>.tfvars, which may
# override a value). Usage: make plan ENV=dev STACK=<stack>

# Where the stacks deploy. The Terraform state bucket's region is separate: STATE_REGION
# (default AWS_REGION), the region terraform/bootstrap was applied in.
region = "us-east-1"

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "2026-10-10"
  owner                = "naveen"
  internal_client_code = "TBD" # placeholder: replace with the real value
  client               = "TBD" # placeholder: replace with the real value
  project_name         = "entingestion"
  division             = "TBD" # placeholder: replace with the real value
}
