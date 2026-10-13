# ui stack, prod. Code: terraform/stacks/ui. Usage: make plan ENV=prod STACK=ui
# Region and mandatory tags: envs/prod/common.tfvars.
# Plan only for now. Before a prod apply: a WAF web ACL, access logs and a custom domain with an
# ACM certificate (docs/UI.md "Follow-ups"), and a login per person (Cognito) rather than one.

env = "prod"

# CI should switch to lambda_s3_bucket / lambda_s3_key / lambda_s3_source_code_hash.
lambda_zip_path = "../../../services/ui/bff/dist/bff_lambda.zip"

log_retention_days = 90
log_level          = "INFO"

price_class = "PriceClass_100"
