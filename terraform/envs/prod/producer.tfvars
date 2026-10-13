# producer stack, prod. Code: terraform/stacks/producer. Usage: make plan ENV=prod STACK=producer
# Region and mandatory tags: envs/prod/common.tfvars.

env = "prod"

# prod table names are unprefixed (process, articles, producer_uploads).
table_prefix = ""

# Browser uploads: the upload UI's origin comes from SSM (ui stack); add any other web app here.
ui_extra_origins = []

# CI should switch to lambda_s3_bucket / lambda_s3_key / lambda_s3_source_code_hash.
lambda_zip_path = "../../../services/producer/dist/producer_lambda.zip"

log_retention_days = 90
log_level          = "INFO"

manage_apigw_account = false

# WAF: the stack creates one ACL per environment (set waf_acl_arn to use a shared ACL instead).
waf_rate_limit = 2000

# On-call address(es); each must confirm by email. Parked: with [] the prod plan FAILS on purpose
# (alarm_emails validation) until an address is set. See docs/RUNBOOK.md "Alarms".
alarm_emails = []

# Disaster recovery: AWS Backup of the process, articles and uploads tables, copied to another region.
backup_enabled     = true
backup_copy_region = "us-east-2"

# Shared account: keep uploads registering when dev is busy.
registrar_reserved_concurrency = 10
api_reserved_concurrency       = 10

# Who may use which source; required before the first prod apply, e.g.
#   source_access = { news = { tenant_id = "news", writers = ["role/news-feed"],
#                              readers = ["role/search-*"] } }
admin_principals = ["role/entingestion-prod-deploy"]
source_access    = {}

