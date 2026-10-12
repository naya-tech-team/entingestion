# producer stack, dev. Code: terraform/stacks/producer. Usage: make plan ENV=dev STACK=producer

env            = "dev"
region         = "us-west-2"
aws_account_id = "106751264162"

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "2026-10-10"
  owner                = "naveen"
  internal_client_code = "TBD" # placeholder: replace with the real value
  client               = "TBD" # placeholder: replace with the real value
  project_name         = "entingestion"
  division             = "TBD" # placeholder: replace with the real value
}

# dev shares the account with prod, so its tables are prefixed.
table_prefix = "dev-"

# Browser uploads: the upload UI's origin comes from SSM (ui stack), plus its local Vite dev
# server (make ui-dev).
ui_extra_origins = ["http://localhost:5173"]

# Built by services/producer/scripts/build_lambda.sh (relative to terraform/stacks/producer).
lambda_zip_path = "../../../services/producer/dist/producer_lambda.zip"

log_retention_days = 30
log_level          = "DEBUG"

# The account already has an API Gateway CloudWatch role (hr-apigw-cloudwatch-dev); don't replace it.
manage_apigw_account = false

# WAF: the stack creates one ACL per environment (set waf_acl_arn to use a shared ACL instead).
waf_rate_limit = 2000

# Alarm notifications; each address must confirm the SNS subscription email.
alarm_emails = []

# AWS Backup and cross-region copies are prod-only (not on the free plan).
backup_enabled = false

# Who may use which source (review C3). One tenant in dev; the tools run as bedrock-role (until
# the deploy roles are in use) or as the dev deploy role.
admin_principals = ["arn:aws:iam::106751264162:role/entingestion-dev-deploy"]
source_access = {
  for source, tenant in {
    acceptance    = "internal"
    bruno         = "internal"
    gate          = "internal"
    gate1k        = "internal"
    smoke         = "internal"
    "tenant-test" = "t-small" # per-tenant model test: ingestion maps t-small to titan2_512
    } : source => {
    tenant_id = tenant
    writers = [
      "arn:aws:iam::106751264162:user/bedrock-role",
      "arn:aws:iam::106751264162:role/entingestion-dev-deploy",
      "arn:aws:iam::106751264162:role/ui-dev-bff", # the upload UI's BFF (terraform/stacks/ui)
    ]
  }
}

# Raw object versions: dev keeps noncurrent versions only as long as Glacier IR's minimum needs.
raw_noncurrent_expiration_days = 120


# The workload boundary policy comes with the bootstrap apply (terraform/bootstrap/access.tf),
# which waits for `aws login`. Until then dev roles are created without it; set true after.
workload_boundary_enabled = false
