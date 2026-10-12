# Reconciler (brief §11): EventBridge Scheduler runs it on a fixed schedule (hourly by default).

module "reconcile_schedule" {
  source = "../../modules/scheduler_schedule"

  name                = "${local.name_prefix}-reconciler"
  description         = "Ingestion reconciler (${var.env})"
  schedule_expression = var.reconcile_schedule
  target_arn          = module.fn["reconciler"].alias_arn
  kms_key_arn         = module.kms.key_arn
  enabled             = var.reconcile_enabled
  # A missed run isn't worth catching up on: the next one does the same work.
  retry_attempts        = 1
  max_event_age_seconds = 900
  permissions_boundary  = local.permissions_boundary
}

# Weekly chunk-count drift check (brief §11 step 6): the same function with {"drift": true}.
module "drift_schedule" {
  source = "../../modules/scheduler_schedule"

  name                  = "${local.name_prefix}-drift-check"
  description           = "Ingestion drift check: pgvector chunk counts vs LATEST (${var.env})"
  schedule_expression   = var.drift_schedule
  target_arn            = module.fn["reconciler"].alias_arn
  kms_key_arn           = module.kms.key_arn
  enabled               = var.reconcile_enabled
  input                 = jsonencode({ drift = true })
  retry_attempts        = 1
  max_event_age_seconds = 3600
  permissions_boundary  = local.permissions_boundary
}
