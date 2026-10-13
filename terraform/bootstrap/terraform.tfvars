# The region comes from the environment, not this file: export TF_VAR_region=$AWS_REGION
# (the stacks' Makefile finds the state bucket in STATE_REGION, which defaults to AWS_REGION).

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "2026-10-10"
  owner                = "naveen"
  internal_client_code = "TBD" # placeholder: replace with the real value
  client               = "TBD" # placeholder: replace with the real value
  project_name         = "entingestion"
  division             = "TBD" # placeholder: replace with the real value
}

# The people who may assume the deploy roles come from the environment, not this file:
#   export TF_VAR_deploy_principal_arns='["user/<you>"]'
