# -- Account and tagging ------------------------------------------------------

variable "env" {
  description = "Environment name. prod turns on deletion protection."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.env))
    error_message = "env must be 2-16 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "aws_account_id" {
  description = "Account this environment deploys to."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit account id."
  }
}

variable "mandatory_tags" {
  description = "The organization's required tags, applied to every resource through the provider's default_tags. Name comes from the resource itself, or else the stack."
  type = object({
    created_by           = string
    created_date         = string # YYYY-MM-DD: when this stack was first applied in this environment
    owner                = string
    internal_client_code = string
    client               = string
    project_name         = string
    division             = string
  })

  validation {
    condition     = alltrue([for v in values(var.mandatory_tags) : trimspace(v) != ""])
    error_message = "Every mandatory tag needs a value: created_by, created_date, owner, internal_client_code, client, project_name, division."
  }

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", var.mandatory_tags.created_date))
    error_message = "mandatory_tags.created_date must be YYYY-MM-DD."
  }

  validation {
    condition     = alltrue([for v in values(var.mandatory_tags) : can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]{0,256}$", v))])
    error_message = "Tag values may contain only letters, digits, spaces and _ . : / = + - @ (up to 256 characters)."
  }
}

variable "workload_boundary_enabled" {
  description = "Set a permissions boundary on every IAM role the stack creates. The deploy role may only create roles with entingestion-<env>-workload-boundary (terraform/bootstrap), so turn this off only where that bootstrap isn't used."
  type        = bool
  default     = true
}

variable "permissions_boundary_arn" {
  description = "Boundary policy ARN for the stack's roles. null = arn:<partition>:iam::<account>:policy/entingestion-<env>-workload-boundary."
  type        = string
  default     = null
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}

  # S3 objects (the Lambda packages) take at most 10 tags: the 8 mandatory tags, env, and this one.
  validation {
    condition     = length(var.tags) <= 1
    error_message = "At most 1 extra tag: S3 objects allow 10 tags, and 9 are mandatory_tags plus env."
  }
}

# -- Producer hand-off (read-only) ---------------------------------------------

variable "producer_ssm_prefix" {
  description = "SSM prefix where the producer publishes its outputs, e.g. /entingestion/dev/producer."
  type        = string
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix for values other stacks consume (the ui stack); this stack publishes under <ssm_prefix>/<env>/ingestion/."
  type        = string
  default     = "/entingestion"

  validation {
    condition     = startswith(var.ssm_prefix, "/") && !endswith(var.ssm_prefix, "/")
    error_message = "ssm_prefix must start with / and must not end with /."
  }
}

# -- Lambda packages (services/ingestion/scripts/build_lambda.sh) ---------------

variable "lambda_core_zip" {
  description = "Path to dist/core.zip, relative to this directory."
  type        = string
}

variable "lambda_docs_zip" {
  description = "Path to dist/docs.zip, relative to this directory."
  type        = string
}

# -- Service --------------------------------------------------------------------

variable "table_prefix" {
  description = "Prefix for the DynamoDB table names."
  type        = string
  default     = ""
}

variable "pipeline_version" {
  description = "Bump to re-process every article (part of the dedupe key and the unchanged check)."
  type        = string
  default     = "v1"
}

variable "embedding_profiles" {
  description = <<-EOT
    Embedding models, one pgvector table each: chunks_<name> (docs/ARCHITECTURE.md "Embedding
    models"). The migrate function creates a new profile's table; reindex fills it; a switch
    makes it live for the default registry or one tenant. Never change a profile's model or
    dimension once its table has data: add a new profile instead.
      provider             "bedrock" (InvokeModel: amazon.titan-embed-text-v2*, cohere.embed-*)
                           or "openai_compatible" (POST <base_url>/embeddings: LiteLLM, vLLM, ...)
      model                Bedrock model id, or the server's model name
      dimension            1..4000 (pgvector's HNSW limit for halfvec)
      normalize            unit-length vectors (Titan: by the model; others: by the pipeline)
      base_url             openai_compatible: https://... (http only for localhost)
      api_key_secret       openai_compatible: Secrets Manager secret name holding the API key
      document_input_type  input_type sent when embedding chunks (Cohere: search_document)
      query_input_type     input_type sent when embedding search queries (Cohere: search_query)
      send_dimensions      openai_compatible: send "dimensions" (Matryoshka models)
      batch_size           texts per call (Titan: always 1; Cohere on Bedrock: at most 96)
      max_input_tokens     the model's input limit; Validate fails chunks above it
  EOT
  type = map(object({
    provider            = string
    model               = string
    dimension           = number
    normalize           = optional(bool, true)
    base_url            = optional(string)
    api_key_secret      = optional(string)
    document_input_type = optional(string)
    query_input_type    = optional(string)
    send_dimensions     = optional(bool, false)
    batch_size          = optional(number, 16)
    max_input_tokens    = optional(number, 8192)
  }))
  default = {
    titan2_1024 = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 1024 }
    titan2_512  = { provider = "bedrock", model = "amazon.titan-embed-text-v2:0", dimension = 512 }
  }

  validation {
    condition     = length(var.embedding_profiles) > 0 && alltrue([for name in keys(var.embedding_profiles) : can(regex("^[a-z][a-z0-9_]{0,40}$", name))])
    error_message = "Define at least one profile; names must match ^[a-z][a-z0-9_]{0,40}$ (the table is chunks_<name>)."
  }

  validation {
    condition     = alltrue([for p in values(var.embedding_profiles) : contains(["bedrock", "openai_compatible"], p.provider)])
    error_message = "provider must be bedrock or openai_compatible."
  }

  validation {
    condition     = alltrue([for p in values(var.embedding_profiles) : p.dimension >= 1 && p.dimension <= 4000 && floor(p.dimension) == p.dimension])
    error_message = "dimension must be a whole number from 1 to 4000 (pgvector's HNSW limit for halfvec)."
  }

  validation {
    condition = alltrue([
      for p in values(var.embedding_profiles) :
      p.provider != "bedrock" || startswith(p.model, "amazon.titan-embed-text-v2") || startswith(p.model, "cohere.embed-")
    ])
    error_message = "bedrock profiles support amazon.titan-embed-text-v2* and cohere.embed-* models."
  }

  validation {
    condition = alltrue([
      for p in values(var.embedding_profiles) :
      p.provider != "openai_compatible" || can(regex("^(https://[^/]+|http://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?)(/.*)?$", coalesce(p.base_url, "-")))
    ])
    error_message = "openai_compatible profiles need base_url, https:// (http:// only for localhost)."
  }

  validation {
    condition     = alltrue([for p in values(var.embedding_profiles) : p.batch_size >= 1 && p.max_input_tokens >= 1])
    error_message = "batch_size and max_input_tokens must be at least 1."
  }

  # Every function gets the profiles as one environment variable; Lambda allows 4 KB in all.
  validation {
    condition     = length(jsonencode(var.embedding_profiles)) <= 2048
    error_message = "embedding_profiles is too large for the Lambda environment (2 KB of the 4 KB budget): remove unused profiles."
  }
}

variable "default_profile" {
  description = "The default registry's table before it has a registry item (tenants without their own registry): chunks_<default_profile>."
  type        = string
  default     = "titan2_1024"

  validation {
    condition     = contains(keys(var.embedding_profiles), var.default_profile)
    error_message = "default_profile must name one of embedding_profiles."
  }
}

variable "tenant_profiles" {
  description = "tenant_id => profile: a tenant's first table (its own registry until it changes). For new tenants; moving a tenant that already has articles is a reindex and switch (docs/RUNBOOK.md \"Move a tenant to another model\")."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for p in values(var.tenant_profiles) : contains(keys(var.embedding_profiles), p)])
    error_message = "tenant_profiles must map tenants to profiles defined in embedding_profiles."
  }

  validation {
    condition     = alltrue([for t in keys(var.tenant_profiles) : trimspace(t) != "" && t != "default"]) && length(jsonencode(var.tenant_profiles)) <= 512
    error_message = "tenant_profiles keys must be tenant ids (not empty or \"default\"), at most 512 bytes in all."
  }
}

variable "embed_threads" {
  description = "Concurrent Bedrock calls inside one Embed invocation."
  type        = number
  default     = 4
}

variable "lease_seconds" {
  description = "How long a run holds an article between heartbeats. Must exceed the longest step (900 s) plus its retries, so a slow step isn't taken over mid-flight."
  type        = number
  default     = 2100

  validation {
    condition     = var.lease_seconds >= 1860
    error_message = "lease_seconds must be at least 2 x (longest step timeout 900 s + 30 s)."
  }
}

variable "embed_reserved_concurrency" {
  description = "Reserved concurrency for Embed (bounds total Bedrock calls). null = unreserved (needed while the account limit is below ~20)."
  type        = number
  default     = null
}

variable "dispatcher_reserved_concurrency" {
  description = "Reserved concurrency for the dispatcher. null = unreserved."
  type        = number
  default     = null
}

variable "dispatcher_max_concurrency" {
  description = "Max concurrent dispatcher invocations driven by the ingest queue (2-1000). Bounds how fast runs start, not how many run: that's live_slots."
  type        = number
  default     = 2
}

variable "live_slots" {
  description = "Admission control: most runs from the ingest queue (stream, reconciler, operators) in flight at once; more wait on the queue. null = 2 x embed_reserved_concurrency, or 8 without it. 0 = no limit."
  type        = number
  default     = null

  validation {
    condition     = var.live_slots == null || coalesce(var.live_slots, 0) >= 0
    error_message = "live_slots must be 0 (no limit) or more."
  }
}

variable "backfill_slots" {
  description = "Admission control: most runs from the backfill queue (reindex, rebuild, drift repairs) in flight at once. 0 = no limit."
  type        = number
  default     = 2

  validation {
    condition     = var.backfill_slots >= 0
    error_message = "backfill_slots must be 0 (no limit) or more."
  }
}

variable "queue_max_receive_count" {
  description = "Receives before a message moves to its queue's DLQ. High, because a record waiting for a slot is received again and again; messages that can never start go to the DLQ at once (the dispatcher sends them)."
  type        = number
  default     = 50

  validation {
    condition     = var.queue_max_receive_count >= 5 && var.queue_max_receive_count <= 1000
    error_message = "queue_max_receive_count must be between 5 and 1000."
  }
}

variable "control_api_reserved_concurrency" {
  description = "Reserved concurrency for the control API. null = unreserved. Set in prod so a busy dev in the same account can't starve it (shared Lambda concurrency)."
  type        = number
  default     = null
}

variable "index_reserved_concurrency" {
  description = "Reserved concurrency for Index (bounds database connections). null = unreserved."
  type        = number
  default     = null
}

variable "max_chunks_per_article" {
  description = "An article that chunks into more than this fails E_TOO_LARGE (permanent): bounds Bedrock calls and rows per article."
  type        = number
  default     = 5000
}

variable "execution_timeout_seconds" {
  description = "Ingest state machine TimeoutSeconds: a run still going after this times out (alarm runs-timing-out); its lease expires and the reconciler resumes the article."
  type        = number
  default     = 10800

  validation {
    condition     = var.execution_timeout_seconds >= 3600
    error_message = "execution_timeout_seconds must be at least 3600 (OCR of a large PDF takes a while)."
  }
}

variable "log_retention_days" {
  description = "Retention for every log group in the stack."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "POWERTOOLS_LOG_LEVEL."
  type        = string
  default     = "INFO"
}

variable "staging_noncurrent_days" {
  description = "Days before noncurrent staging versions move to GLACIER_IR."
  type        = number
  default     = 30
}

variable "staging_noncurrent_expiration_days" {
  description = "Days before noncurrent staging versions are deleted: an article's older versions after a new one is indexed (the current version stays, as the rebuild source). A delete removes all of an article's versions at once."
  type        = number
  default     = 90

  validation {
    condition     = var.staging_noncurrent_expiration_days > var.staging_noncurrent_days
    error_message = "staging_noncurrent_expiration_days must be after the transition (staging_noncurrent_days)."
  }
}

# -- Network and database ---------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the ingestion VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Availability Zones for the VPC."
  type        = number
  default     = 3
}

variable "interface_endpoint_az_count" {
  description = "AZs that get interface endpoint ENIs (billed per AZ-hour)."
  type        = number
  default     = 2
}

variable "database_mode" {
  description = <<-EOT
    aurora: Aurora PostgreSQL Serverless v2 behind RDS Proxy (the brief's design; prod).
    rds:    one RDS for PostgreSQL instance, Lambdas connect directly. For the AWS free plan,
            which only allows "express" Aurora (no VPC, not in Terraform) and no RDS Proxy.
  EOT
  type        = string
  default     = "aurora"

  validation {
    condition     = contains(["aurora", "rds"], var.database_mode)
    error_message = "database_mode must be aurora or rds."
  }
}

variable "rds_engine_version" {
  description = "RDS for PostgreSQL version (database_mode = rds)."
  type        = string
  default     = "17.11"
}

variable "rds_instance_class" {
  description = "RDS instance class (database_mode = rds); db.t4g.micro fits the free tier."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_backup_retention_days" {
  description = "Automated backup retention (the AWS free plan allows at most 1)."
  type        = number
  default     = 7
}

variable "db_performance_insights" {
  description = "Performance Insights on the RDS instance (database_mode = rds)."
  type        = bool
  default     = true
}

variable "db_monitoring_interval" {
  description = "Enhanced Monitoring interval in seconds (0 disables it)."
  type        = number
  default     = 60
}

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL version (pgvector >= 0.8 needs 16.6 or later)."
  type        = string
  default     = "16.13"
}

variable "aurora_min_acu" {
  description = "Serverless v2 minimum ACU."
  type        = number
  default     = 0.5
}

variable "aurora_max_acu" {
  description = "Serverless v2 maximum ACU."
  type        = number
  default     = 4
}

variable "aurora_instance_count" {
  description = "Aurora instances: 1 writer, the rest readers that Aurora promotes on failover (2+ for high availability)."
  type        = number
  default     = 1

  validation {
    condition     = var.aurora_instance_count >= 1 && var.aurora_instance_count <= 15
    error_message = "aurora_instance_count must be between 1 and 15."
  }
}

variable "backup_enabled" {
  description = "AWS Backup of the database and the status/cache tables: daily, kept backup_retention_days, copied to backup_copy_region. prod only (not on the AWS free plan)."
  type        = bool
  default     = false
}

variable "backup_copy_region" {
  description = "Region the backups are copied to (disaster recovery); must differ from region."
  type        = string
  default     = "us-east-2"

  validation {
    condition     = var.backup_copy_region != var.region
    error_message = "backup_copy_region must be another region than region."
  }
}

variable "backup_retention_days" {
  description = "How long backups are kept, in both regions."
  type        = number
  default     = 35
}

variable "allow_truncate" {
  description = "Let the migrate function empty a vector table ({\"action\": \"truncate\"}, the rebuild test). Off in prod."
  type        = bool
  default     = true
}

# -- Operations (Phase 2) ------------------------------------------------------------

variable "api_throttle_rate" {
  description = "Control API steady-state requests per second."
  type        = number
  default     = 10
}

variable "api_throttle_burst" {
  description = "Control API burst limit."
  type        = number
  default     = 20
}

variable "waf_acl_arn" {
  description = "Existing regional WAF web ACL for the control API. When null, the stack creates one."
  type        = string
  default     = null
}

variable "waf_rate_limit" {
  description = "Requests per source IP per 5 minutes before WAF blocks (created ACL only)."
  type        = number
  default     = 500
}

variable "reconcile_schedule" {
  description = "When the reconciler runs (EventBridge Scheduler expression)."
  type        = string
  default     = "rate(1 hour)"
}

variable "reconcile_enabled" {
  description = "Run the reconciler on its schedule."
  type        = bool
  default     = true
}

variable "reconcile_batch" {
  description = "Most articles the reconciler handles per step and run."
  type        = number
  default     = 500
}

variable "alarm_emails" {
  description = "Email addresses subscribed to the alerts topic (each must confirm). Required in prod."
  type        = list(string)
  default     = []

  # Alarms nobody receives are worse than none: prod refuses to plan without a subscriber.
  validation {
    condition     = var.env != "prod" || length(var.alarm_emails) > 0
    error_message = "prod needs someone to receive its alarms: set alarm_emails = [\"oncall@example.com\"] in envs/prod/ingestion.tfvars (each address must confirm the SNS email). See docs/RUNBOOK.md \"Alarms\"."
  }
}

variable "alarm_notify_on_ok" {
  description = "Also email when an alarm recovers (OK). Off: one message per incident."
  type        = bool
  default     = false
}

variable "queue_age_alarm_seconds" {
  description = "Alarm when the oldest ingest message is older than this."
  type        = number
  default     = 900
}

variable "failed_runs_alarm_threshold" {
  description = "Alarm when this many executions fail in 5 minutes."
  type        = number
  default     = 5
}

variable "freshness_alarm_seconds" {
  description = "Freshness SLO: alarm when the p95 producer-update-to-indexed lag of stream runs exceeds this for 15 minutes."
  type        = number
  default     = 900
}

variable "backlog_alarm_seconds" {
  description = "Backlog SLO: alarm when an article has sat in a running status (CLAIMED..INDEXING, STAGED) longer than this (reconciler, hourly)."
  type        = number
  default     = 1800
}

variable "failure_ratio_alarm_percent" {
  description = "Failure SLO: alarm when more than this percentage of runs in an hour fail permanently (at least 20 runs)."
  type        = number
  default     = 1
}

variable "step_p95_alarm_ms" {
  description = <<-EOT
    Alarm when a step's p95 duration (StepDuration_<STEP>) stays above this for 15 minutes.
    Defaults are about 5-10x the dev baseline (p95 0.1-2.6 s on the sample corpus), with room
    for large documents in Extract and Index; retune from the dashboard after a week of prod.
  EOT
  type        = map(number)
  default = {
    FETCH     = 3000
    EXTRACT   = 30000
    NORMALIZE = 10000
    CHUNK     = 10000
    EMBED     = 15000
    VALIDATE  = 5000
    INDEX     = 20000
    FINALIZE  = 5000
  }

  validation {
    condition     = alltrue([for k in keys(var.step_p95_alarm_ms) : contains(["FETCH", "EXTRACT", "NORMALIZE", "CHUNK", "EMBED", "VALIDATE", "INDEX", "FINALIZE"], k)])
    error_message = "Keys must be step names: FETCH, EXTRACT, NORMALIZE, CHUNK, EMBED, VALIDATE, INDEX, FINALIZE."
  }
}

variable "proxy_connections_alarm" {
  description = "Alarm when RDS Proxy client connections reach this (aurora mode)."
  type        = number
  default     = 500
}

variable "ocr_enabled" {
  description = "Send scanned and over-size PDFs to Textract. Off where Textract isn't available (the AWS free plan): scanned PDFs are then SKIPPED and over-size ones fail E_TOO_LARGE."
  type        = bool
  default     = true
}

variable "ocr_poll_seconds" {
  description = "How often the state machine checks a running Textract job."
  type        = number
  default     = 20
}

variable "ocr_max_wait_seconds" {
  description = "A Textract job still running after about this long fails the run E_TIMEOUT (retryable) instead of polling on."
  type        = number
  default     = 3600

  validation {
    condition     = var.ocr_max_wait_seconds >= 300
    error_message = "ocr_max_wait_seconds must be at least 300."
  }
}

variable "drift_schedule" {
  description = "When the drift check (pgvector chunk counts vs LATEST) runs."
  type        = string
  default     = "rate(7 days)"
}

# -- Retrieval -----------------------------------------------------------------------------

variable "parent_child_enabled" {
  description = "Parent-child chunking: small chunks are embedded and matched, and search returns their section-level parent's text as context. It changes every chunk: bump pipeline_version (or reindex) when switching it."
  type        = bool
  default     = false
}

variable "rerank_enabled" {
  description = "Rerank /v1/search results with a Bedrock reranking model when the request asks for it (paid per query; off by default)."
  type        = bool
  default     = false
}

variable "rerank_model_id" {
  description = "Bedrock reranking model (bedrock-agent-runtime Rerank)."
  type        = string
  default     = "amazon.rerank-v1:0"
}

variable "eval_tolerance" {
  description = "Switch gate: a table may go live if its newest recall@10 is at most this much below the live table's (same golden set)."
  type        = number
  default     = 0.02

  validation {
    condition     = var.eval_tolerance >= 0 && var.eval_tolerance < 1
    error_message = "eval_tolerance must be in [0, 1)."
  }
}

variable "eval_max_age_days" {
  description = "Switch gate: the table's evaluation report must be younger than this."
  type        = number
  default     = 7

  validation {
    condition     = var.eval_max_age_days >= 1
    error_message = "eval_max_age_days must be at least 1."
  }
}

variable "pii_screening" {
  description = "Screen article text for PII with Amazon Comprehend in Normalize and apply each source's pii policy (block / redact / allow, from the producer's source_access). Paid, and not on the AWS free plan: off by default."
  type        = bool
  default     = false
}

variable "pii_min_score" {
  description = "Comprehend confidence below which a PII entity is ignored."
  type        = number
  default     = 0.8

  validation {
    condition     = var.pii_min_score > 0 && var.pii_min_score <= 1
    error_message = "pii_min_score must be in (0, 1]."
  }
}
