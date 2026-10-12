# ui stack, dev. Code: terraform/stacks/ui. Usage: make plan ENV=dev STACK=ui
# First time: apply producer, then ingestion, then ui, then producer again (docs/UI.md).

env            = "dev"
region         = "us-west-2"
aws_account_id = "106751264162"

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "2026-10-11"
  owner                = "naveen"
  internal_client_code = "TBD" # placeholder: replace with the real value
  client               = "TBD" # placeholder: replace with the real value
  project_name         = "entingestion"
  division             = "TBD" # placeholder: replace with the real value
}

# Built by services/ui/bff/scripts/build_lambda.sh (relative to terraform/stacks/ui).
lambda_zip_path = "../../../services/ui/bff/dist/bff_lambda.zip"

log_retention_days = 30
log_level          = "DEBUG"

# North America and Europe edges only: the cheapest class.
price_class = "PriceClass_100"

# The workload boundary policy comes with the bootstrap apply (terraform/bootstrap/access.tf),
# which waits for `aws login`. Until then dev roles are created without it; set true after.
workload_boundary_enabled = false
