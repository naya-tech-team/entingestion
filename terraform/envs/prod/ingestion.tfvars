# ingestion stack, prod. Code: terraform/stacks/ingestion. Usage: make plan ENV=prod STACK=ingestion

env            = "prod"
region         = "us-west-2"
aws_account_id = "106751264162"

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "" # required: the date of the first prod apply
  owner                = "naveen"
  internal_client_code = "" # required: fill in
  client               = "" # required: fill in
  project_name         = "entingestion"
  division             = "" # required: fill in
}

producer_ssm_prefix = "/entingestion/prod/producer"
table_prefix        = ""

# Built by services/ingestion/scripts/build_lambda.sh; the stack uploads them to its artifacts bucket.
lambda_core_zip = "../../../services/ingestion/dist/core.zip"
lambda_docs_zip = "../../../services/ingestion/dist/docs.zip"

database_mode            = "aurora" # Aurora Serverless v2 behind RDS Proxy
aurora_min_acu           = 0.5
aurora_max_acu           = 8
aurora_instance_count    = 2 # writer + a reader in another AZ that Aurora promotes on failover
db_backup_retention_days = 7

# The rebuild test (migrate {"action": "truncate"}) empties a vector table; never in prod.
allow_truncate = false

# Bound concurrent Bedrock calls (plan correction 3). Reserving concurrency needs headroom in the
# account's Lambda quota (at least 100 must stay unreserved), so request the increase before the
# first prod apply; see docs/RUNBOOK.md "Prerequisites".
embed_reserved_concurrency = 4
dispatcher_max_concurrency = 5
# Admission control: live_slots defaults to 2 x embed_reserved_concurrency (8); reindexing and
# drift repairs share backfill_slots.
backfill_slots = 2

# Scanned and very large PDFs go through Textract (needs a paid account plan).
ocr_enabled = true

# Alarms (rationale and dev baselines in docs/RUNBOOK.md "Alarms"). Stream runs take seconds
# end to end (sum of step p95s on dev ~6 s), so 15 minutes of lag means something is stuck.
# step_p95_alarm_ms keeps its defaults until prod has a week of data.
freshness_alarm_seconds = 900

# On-call address(es); each must confirm by email. Parked: with [] the prod plan FAILS on purpose
# (alarm_emails validation) until an address is set. See docs/RUNBOOK.md "Alarms".
alarm_emails = []

# Disaster recovery: AWS Backup of Aurora and the tables, daily, 35 days, copied to us-east-2.
backup_enabled     = true
backup_copy_region = "us-east-2"

# Shared account (dev and prod): reserve concurrency for what must keep working when dev is busy
# (the account's unreserved pool must stay >= 100; see docs/RUNBOOK.md "Prerequisites").
dispatcher_reserved_concurrency  = 5
control_api_reserved_concurrency = 5
index_reserved_concurrency       = 10

log_retention_days = 90
log_level          = "INFO"

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
#   #                 base_url = "https://litellm.example.com/v1", api_key_secret = "entingestion/prod/litellm" }
# }
# default_profile = "titan2_1024"
# tenant_profiles = {} # tenant_id => profile: a new tenant's first table
