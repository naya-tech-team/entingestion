# Ingest workflow: one Standard execution per article version.

locals {
  ingest_definition = templatefile("${path.module}/asl/ingest.asl.json.tftpl", merge(
    # The live aliases: a deploy rolls back by moving them (make rollback), never by editing ASL.
    { for name in local.workflow_functions : "${name}_arn" => module.fn[name].alias_arn },
    # Task timeouts: the function's own timeout plus headroom.
    { for name in local.workflow_functions : "timeout_${name}" => local.functions[name].timeout + 30 },
    {
      ocr_poll_seconds  = var.ocr_poll_seconds
      execution_timeout = var.execution_timeout_seconds
      # Throttling (Lambda concurrency, Bedrock quota) gets a long budget in the state machine:
      # 5 s doubling to 120 s, about 15 minutes in all, before MarkFailed's throttle budget.
      throttle_max_attempts = 12
    },
  ))
}

module "ingest_workflow" {
  source = "../../modules/step_function"

  name                 = local.state_machine_name
  definition           = local.ingest_definition
  policy_documents     = [data.aws_iam_policy_document.ingest_workflow.json]
  log_level            = "ALL"
  log_retention_days   = var.log_retention_days
  kms_key_arn          = module.kms.key_arn
  permissions_boundary = local.permissions_boundary
}

# Reindex / rebuild: Distributed Map over the planned articles -> backfill queue.
module "reindex_workflow" {
  source = "../../modules/step_function"

  name = local.reindex_state_machine_name
  definition = templatefile("${path.module}/asl/reindex.asl.json.tftpl", {
    reindex_plan_arn        = module.fn["reindex_plan"].alias_arn
    reindex_enqueue_arn     = module.fn["reindex_enqueue"].alias_arn
    timeout_reindex_plan    = local.functions["reindex_plan"].timeout + 30
    timeout_reindex_enqueue = local.functions["reindex_enqueue"].timeout + 30
    batch_size              = 100
    max_concurrency         = 2
  })
  policy_documents     = [data.aws_iam_policy_document.reindex_workflow.json]
  log_level            = "ERROR"
  log_retention_days   = var.log_retention_days
  kms_key_arn          = module.kms.key_arn
  permissions_boundary = local.permissions_boundary
}
