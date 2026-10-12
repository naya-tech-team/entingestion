# ingestion stack, dev. Code: terraform/stacks/ingestion. Usage: make plan ENV=dev STACK=ingestion

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

producer_ssm_prefix = "/entingestion/dev/producer"
table_prefix        = "dev-"

# Built by services/ingestion/scripts/build_lambda.sh (relative to terraform/stacks/ingestion).
lambda_core_zip = "../../../services/ingestion/dist/core.zip"
lambda_docs_zip = "../../../services/ingestion/dist/docs.zip"

# AWS free plan: Aurora is only offered as "express" clusters (outside the VPC, not creatable
# with Terraform) and RDS Proxy isn't available, so dev uses one free-tier RDS instance that the
# Lambdas reach directly with IAM auth. prod keeps Aurora + RDS Proxy.
database_mode            = "rds"
rds_instance_class       = "db.t4g.micro"
db_backup_retention_days = 1 # free-plan maximum
db_performance_insights  = false
db_monitoring_interval   = 0

# Only migrate uses an interface endpoint (Secrets Manager); one AZ keeps it cheap.
interface_endpoint_az_count = 1

# The account's Lambda concurrency limit is 10, so nothing can be reserved yet. After a quota
# increase, set embed_reserved_concurrency = 4 to bound concurrent Bedrock calls.
embed_reserved_concurrency      = null
dispatcher_reserved_concurrency = null
dispatcher_max_concurrency      = 2
# Admission control: 8 live runs, 2 backfill runs at a time (defaults).

# AWS Backup and cross-region copies are prod-only (not on the free plan).
backup_enabled = false

# Textract needs a paid account plan; this account is on the free plan. The OCR path is deployed
# but switched off: scanned PDFs are SKIPPED, PDFs over 50 MB fail E_TOO_LARGE.
ocr_enabled = false

log_retention_days = 30
log_level          = "DEBUG"

# The workload boundary policy comes with the bootstrap apply (terraform/bootstrap/access.tf),
# which waits for `aws login`. Until then dev roles are created without it; set true after.
workload_boundary_enabled = false

# Admission control: with a 10-Lambda account limit, at most 3 articles run at once; the rest
# wait on the ingest queue (queue age shows the backlog).
live_slots = 3

# Embedding models (docs/ARCHITECTURE.md "Embedding models", RUNBOOK "Add a model"). The default
# is the two Titan v2 tables (titan2_1024 live, titan2_512), as before profiles. To add a model,
# add a profile here, apply (make deploy: migrate creates chunks_<name>), then reindex/switch.
# embedding_profiles = {
#   titan2_1024 = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
#   titan2_512  = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 512 }
#   # Cohere Embed Multilingual v3 on Bedrock (enable model access first). 512-token input limit:
#   # Validate fails chunks above it, so chunk smaller (or use parent-child chunking) first.
#   cohere_m3 = { provider = "bedrock", model = "cohere.embed-multilingual-v3", dimension = 1024, max_input_tokens = 512, batch_size = 96 }
#   # Later: any OpenAI-compatible endpoint, e.g. a LiteLLM proxy (reachable from the internet:
#   # Embed runs outside the VPC). The API key lives in Secrets Manager under that name.
#   # litellm_768 = { provider = "openai_compatible", model = "nomic-embed-text", dimension = 768,
#   #                 base_url = "https://litellm.example.com/v1", api_key_secret = "entingestion/dev/litellm" }
# }
# default_profile = "titan2_1024"

# A tenant's first table (its own registry), for new tenants. To try per-tenant routing, map a
# test tenant to the 512-d table and give a producer source that tenant (producer.tfvars
# source_access, e.g. "tenant-test" = { tenant_id = "t-small", writers = [...] }):
tenant_profiles = { "t-small" = "titan2_512" } # test tenant on the 512-d model (source tenant-test)
