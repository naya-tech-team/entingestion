# Alarms (pipeline health and SLOs) and the reconciler's hourly failure digest -> one SNS topic (KMS).
# Notified on ALARM only (alarm_notify_on_ok); prod refuses to plan without alarm_emails.

locals {
  # Dead-letter queues. A message the redrive policy moves into a DLQ isn't counted in its
  # NumberOfMessagesSent (only messages sent to it directly are), so "new arrivals" is an
  # increase of the visible count, or a direct send (the dispatcher, the Pipe): one alarm per
  # 5 minutes with arrivals. A second alarm repeats while a DLQ stays undrained for a day.
  dlqs = {
    ingest   = { queue = module.ingest_queue.dlq_name, what = "Ingest messages the dispatcher couldn't start (unparseable, or still waiting after queue_max_receive_count receives). Redrive: POST /v1/dlq/redrive." }
    backfill = { queue = module.backfill_queue.dlq_name, what = "Backfill messages (reindex, rebuild, drift repairs) that couldn't start. Move them back with an SQS redrive (StartMessageMoveTask)." }
    pipe     = { queue = aws_sqs_queue.pipe_dlq.name, what = "Stream records the Pipe couldn't deliver. The reconciler re-enqueues from gsi_updated_shard; check the Pipe." }
  }
  dlq_alarms = merge(
    {
      for name, d in local.dlqs : "${name}-dlq-new-messages" => {
        description = "New messages in the ${name} DLQ. ${d.what}"
        expression  = "MAX([DIFF(visible), sent])"
        metrics = {
          visible = { namespace = "AWS/SQS", metric_name = "ApproximateNumberOfMessagesVisible", dimensions = { QueueName = d.queue }, stat = "Maximum" }
          sent    = { namespace = "AWS/SQS", metric_name = "NumberOfMessagesSent", dimensions = { QueueName = d.queue }, stat = "Sum" }
        }
        threshold = 1
      }
    },
    {
      for name, d in local.dlqs : "${name}-dlq-not-drained" => {
        description         = "The ${name} DLQ has held messages for 24 hours. ${d.what}"
        namespace           = "AWS/SQS"
        metric_name         = "ApproximateNumberOfMessagesVisible"
        dimensions          = { QueueName = d.queue }
        statistic           = "Minimum"
        threshold           = 1
        period              = 3600
        evaluation_periods  = 24
        datapoints_to_alarm = 24
      }
    },
  )

  pipeline_alarms = {
    ingest-queue-backlog = {
      description = "The oldest ingest message has waited too long: every live slot is busy (see BacklogCount), or the dispatcher is failing. Messages received more than 3 times drop out of this metric."
      namespace   = "AWS/SQS"
      metric_name = "ApproximateAgeOfOldestMessage"
      dimensions  = { QueueName = module.ingest_queue.queue_name }
      statistic   = "Maximum"
      threshold   = var.queue_age_alarm_seconds
    }
    runs-failing = {
      description = "Ingest executions are failing (permanent failures; the reconciler's hourly digest lists them by error_code)."
      namespace   = "AWS/States"
      metric_name = "ExecutionsFailed"
      dimensions  = { StateMachineArn = module.ingest_workflow.state_machine_arn }
      statistic   = "Sum"
      threshold   = var.failed_runs_alarm_threshold
    }
    runs-timing-out = {
      description = "Ingest executions hit the state machine timeout (execution_timeout_seconds). Their leases expire and the reconciler resumes them; look for a stuck step or OCR job."
      namespace   = "AWS/States"
      metric_name = "ExecutionsTimedOut"
      dimensions  = { StateMachineArn = module.ingest_workflow.state_machine_arn }
      statistic   = "Sum"
      threshold   = 1
    }
    # SLOs (docs/RUNBOOK.md "SLOs"). Freshness: p95 of stream runs; no stream runs, no data.
    freshness-lag = {
      description         = "Freshness SLO: p95 producer-update-to-searchable lag of stream runs above ${var.freshness_alarm_seconds} s for 15 minutes."
      namespace           = "Ingestion"
      metric_name         = "FreshnessLagSeconds"
      dimensions          = { service = "ingestion", Trigger = "STREAM" }
      extended_statistic  = "p95"
      threshold           = var.freshness_alarm_seconds
      evaluation_periods  = 3
      datapoints_to_alarm = 3
    }
    # The reconciler publishes the backlog hourly; a missed hour is the reconciler-errors alarm.
    backlog-age = {
      description = "Backlog SLO: an article has been in a running status (CLAIMED..INDEXING, STAGED) for over ${var.backlog_alarm_seconds} s. BacklogCount / BacklogOldestSeconds per status on the dashboard."
      namespace   = "Ingestion"
      metric_name = "BacklogOldestSeconds"
      dimensions  = { service = "ingestion", status = "RUNNING" }
      statistic   = "Maximum"
      threshold   = var.backlog_alarm_seconds
      period      = 3600
    }
    failure-ratio = {
      description = "Failure SLO: over ${var.failure_ratio_alarm_percent}% of the last hour's runs failed permanently (with at least 20 runs). Hourly digest: by error_code."
      expression  = "IF(indexed + failed >= 20, 100 * failed / (indexed + failed), 0)"
      metrics = {
        indexed = { namespace = "Ingestion", metric_name = "Run_INDEXED", dimensions = { service = "ingestion" }, stat = "Sum" }
        failed  = { namespace = "Ingestion", metric_name = "Run_FAILED_PERMANENT", dimensions = { service = "ingestion" }, stat = "Sum" }
      }
      comparison_operator = "GreaterThanThreshold"
      threshold           = var.failure_ratio_alarm_percent
      period              = 3600
    }
    reconciler-errors = {
      description = "The reconciler Lambda failed."
      namespace   = "AWS/Lambda"
      metric_name = "Errors"
      dimensions  = { FunctionName = module.fn["reconciler"].function_name }
      statistic   = "Sum"
      threshold   = 1
    }
    dispatcher-errors = {
      description         = "The dispatcher Lambda failed for 15 minutes (3 x 5 min)."
      namespace           = "AWS/Lambda"
      metric_name         = "Errors"
      dimensions          = { FunctionName = module.fn["dispatcher"].function_name }
      statistic           = "Sum"
      threshold           = 1
      evaluation_periods  = 3
      datapoints_to_alarm = 3
    }
    # Waiting for a slot is normal and reported as batch item failures, not function errors;
    # failing to start executions (StartExecution errors) is this.
    dispatcher-start-failing = {
      description         = "The dispatcher couldn't start executions for 15 minutes (Dispatch_error). Check its logs: permissions, the state machine, throttling."
      namespace           = "Ingestion"
      metric_name         = "Dispatch_error"
      dimensions          = { service = "ingestion" }
      statistic           = "Sum"
      threshold           = 1
      evaluation_periods  = 3
      datapoints_to_alarm = 3
    }
    control-api-5xx = {
      description = "The control API returned 5 or more 5XX responses in 5 minutes."
      namespace   = "AWS/ApiGateway"
      metric_name = "5XXError"
      dimensions  = { ApiName = "${local.name_prefix}-control", Stage = module.control_api.stage_name }
      statistic   = "Sum"
      threshold   = 5
    }
  }

  # p95 step duration vs the baseline. 3 of 3 five-minute periods, so one large
  # document doesn't page; no traffic means no data, which isn't breaching.
  step_duration_alarms = {
    for step, ms in var.step_p95_alarm_ms : "step-${lower(step)}-slow" => {
      description         = "${step} p95 duration above ${ms} ms for 15 minutes (baseline in step_p95_alarm_ms)."
      namespace           = "Ingestion"
      metric_name         = "StepDuration_${step}"
      dimensions          = { service = "ingestion" }
      extended_statistic  = "p95"
      threshold           = ms
      evaluation_periods  = 3
      datapoints_to_alarm = 3
    }
  }

  # Database alarms for each database_mode: Aurora + RDS Proxy (prod) or one RDS instance
  # (free-plan dev). Only the current mode's are created.
  db_alarm_defs = {
    aurora-cpu = {
      mode = "aurora"
      alarm = {
        description        = "Aurora CPU above 80% for 15 minutes."
        namespace          = "AWS/RDS"
        metric_name        = "CPUUtilization"
        dimensions         = { DBClusterIdentifier = one(module.db[*].cluster_id) }
        statistic          = "Average"
        threshold          = 80
        evaluation_periods = 3
      }
    }
    aurora-capacity-near-max = {
      mode = "aurora"
      alarm = {
        description        = "Aurora is near its maximum ACU for 15 minutes: raise aurora_max_acu or lower concurrency."
        namespace          = "AWS/RDS"
        metric_name        = "ServerlessDatabaseCapacity"
        dimensions         = { DBClusterIdentifier = one(module.db[*].cluster_id) }
        statistic          = "Average"
        threshold          = 0.9 * var.aurora_max_acu
        evaluation_periods = 3
      }
    }
    aurora-buffer-cache = {
      mode = "aurora"
      alarm = {
        description         = "Aurora buffer cache hit ratio below 90% for 15 minutes (working set exceeds memory)."
        namespace           = "AWS/RDS"
        metric_name         = "BufferCacheHitRatio"
        dimensions          = { DBClusterIdentifier = one(module.db[*].cluster_id) }
        statistic           = "Average"
        threshold           = 90
        comparison_operator = "LessThanThreshold"
        evaluation_periods  = 3
      }
    }
    proxy-connections = {
      mode = "aurora"
      alarm = {
        description = "RDS Proxy client connections near the configured limit."
        namespace   = "AWS/RDS"
        metric_name = "ClientConnections"
        dimensions  = { ProxyName = local.name_prefix }
        statistic   = "Maximum"
        threshold   = var.proxy_connections_alarm
      }
    }
    db-cpu = {
      mode = "rds"
      alarm = {
        description        = "Database CPU above 80% for 15 minutes."
        namespace          = "AWS/RDS"
        metric_name        = "CPUUtilization"
        dimensions         = { DBInstanceIdentifier = one(module.rds[*].instance_id) }
        statistic          = "Average"
        threshold          = 80
        evaluation_periods = 3
      }
    }
    db-free-storage = {
      mode = "rds"
      alarm = {
        description         = "Database free storage below 2 GiB."
        namespace           = "AWS/RDS"
        metric_name         = "FreeStorageSpace"
        dimensions          = { DBInstanceIdentifier = one(module.rds[*].instance_id) }
        statistic           = "Minimum"
        threshold           = 2 * 1024 * 1024 * 1024
        comparison_operator = "LessThanThreshold"
      }
    }
  }
  db_alarms = { for name, d in local.db_alarm_defs : name => d.alarm if d.mode == var.database_mode }

  # Bedrock throttling of the default profile's model (or the first Bedrock profile's): none
  # without a Bedrock profile.
  bedrock_alarms = {
    for model in compact([local.alarm_embedding_model]) : "bedrock-throttled" => {
      description = "Bedrock is throttling embeddings (${model}). Retries absorb bursts; sustained throttling needs a quota increase or fewer live_slots. Shared account: dev load counts against the same quota."
      namespace   = "AWS/Bedrock"
      metric_name = "InvocationThrottles"
      dimensions  = { ModelId = model }
      statistic   = "Sum"
      threshold   = 50
    }
  }
}

module "alerts" {
  source = "../../modules/alarms"

  topic_name      = "${local.name_prefix}-alerts"
  kms_key_arn     = module.kms.key_arn
  email_endpoints = var.alarm_emails
  notify_on_ok    = var.alarm_notify_on_ok
  alarms          = merge(local.dlq_alarms, local.pipeline_alarms, local.bedrock_alarms, local.step_duration_alarms, local.db_alarms)
}
