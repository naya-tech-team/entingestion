# One CloudWatch dashboard per environment (brief §15 Phase 4): flow, backlog, step latency,
# Bedrock, quality, database and control API. docs/RUNBOOK.md says what to do with each panel.

locals {
  steps       = ["FETCH", "EXTRACT", "NORMALIZE", "CHUNK", "EMBED", "VALIDATE", "INDEX", "FINALIZE"]
  eval_tables = ["chunks_titan2_1024", "chunks_titan2_512"] # vectorstore/targets.py
  svc         = ["service", "ingestion"]
  sfn         = ["StateMachineArn", module.ingest_workflow.state_machine_arn]
  db_dim      = local.use_aurora ? ["DBClusterIdentifier", one(module.db[*].cluster_id)] : ["DBInstanceIdentifier", one(module.rds[*].instance_id)]

  header_widget = {
    type = "text", width = 24, height = 2
    properties = {
      markdown = "## Ingestion (${var.env})\nFlow: Pipe -> ingest queue -> dispatcher -> state machine -> pgvector. Alarms notify `${local.name_prefix}-alerts`. What to do with each panel: **docs/RUNBOOK.md**."
    }
  }

  # Metric widgets; region, period (5 min) and view are filled in below unless set.
  metric_widgets = [
    # Flow
    {
      type = "metric", width = 8, height = 6
      properties = {
        title   = "Executions (5 min)"
        stat    = "Sum"
        metrics = [for m in ["ExecutionsStarted", "ExecutionsSucceeded", "ExecutionsFailed", "ExecutionsTimedOut"] : concat(["AWS/States", m], local.sfn)]
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title = "Dispatch and outcomes (5 min)"
        stat  = "Sum"
        metrics = [for m in ["Dispatch_started", "Dispatch_duplicate", "Dispatch_waiting_live", "Dispatch_waiting_backfill", "Dispatch_error", "Run_INDEXED", "Run_SKIPPED", "Run_FAILED_PERMANENT", "Requeued_manual"] :
        concat(["Ingestion", m], local.svc)]
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title = "Freshness lag p95 by trigger (s; SLO: STREAM)"
        stat  = "p95"
        metrics = [for t in ["STREAM", "RECONCILER", "MANUAL", "BACKFILL"] :
          concat(["Ingestion", "FreshnessLagSeconds"], local.svc, ["Trigger", t, { label = t }])
        ]
        annotations = { horizontal = [{ label = "SLO", value = var.freshness_alarm_seconds }] }
      }
    },

    # SLOs: backlog (reconciler, hourly) and failures
    {
      type = "metric", width = 8, height = 6
      properties = {
        title  = "Backlog by status (reconciler, hourly)"
        stat   = "Maximum"
        period = 3600
        metrics = [for st in ["RUNNING", "STAGED", "EMBEDDING", "INDEXING", "FAILED_RETRYABLE"] :
          concat(["Ingestion", "BacklogCount"], local.svc, ["status", st, { label = st }])
        ]
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title  = "Oldest running article (s)"
        stat   = "Maximum"
        period = 3600
        metrics = [
          concat(["Ingestion", "BacklogOldestSeconds"], local.svc, ["status", "RUNNING", { label = "running" }]),
          concat(["Ingestion", "BacklogOldestSeconds"], local.svc, ["status", "FAILED_RETRYABLE", { label = "waiting to retry" }]),
        ]
        annotations = { horizontal = [{ label = "SLO", value = var.backlog_alarm_seconds }] }
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title  = "Permanent failures by error_code (hourly)"
        period = 3600
        metrics = [[{
          expression = "SEARCH('{Ingestion,error_code,service} MetricName=\"ArticlesFailedPermanent\"', 'Sum', 3600)"
          id         = "failed"
        }]]
      }
    },

    # Backlog
    {
      type = "metric", width = 8, height = 6
      properties = {
        title       = "Ingest queue: oldest message (s)"
        stat        = "Maximum"
        metrics     = [["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", module.ingest_queue.queue_name]]
        annotations = { horizontal = [{ label = "alarm", value = var.queue_age_alarm_seconds }] }
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title = "Queue depth"
        stat  = "Maximum"
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", module.ingest_queue.queue_name, { label = "ingest" }],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", module.backfill_queue.queue_name, { label = "backfill" }],
        ]
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title = "Dead-letter queues (new messages alarm)"
        stat  = "Maximum"
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", module.ingest_queue.dlq_name, { label = "ingest DLQ" }],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", module.backfill_queue.dlq_name, { label = "backfill DLQ" }],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.pipe_dlq.name, { label = "pipe DLQ" }],
        ]
      }
    },

    # Steps
    {
      type = "metric", width = 12, height = 6
      properties = {
        title   = "Step duration p95 (ms)"
        stat    = "p95"
        metrics = [for s in local.steps : concat(["Ingestion", "StepDuration_${s}"], local.svc, [{ label = s }])]
      }
    },
    {
      type = "metric", width = 12, height = 6
      properties = {
        title   = "Step failures, all attempts (5 min)"
        stat    = "Sum"
        metrics = [for s in local.steps : concat(["Ingestion", "StepFailed_${s}"], local.svc, [{ label = s }])]
      }
    },
    {
      type = "metric", width = 12, height = 6
      properties = {
        title = "Lambda errors by function"
        metrics = [[{
          expression = "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Errors\" ${local.name_prefix}', 'Sum', 300)"
          id         = "errors"
        }]]
      }
    },
    {
      type = "metric", width = 12, height = 6
      properties = {
        title = "Lambda throttles by function (account concurrency)"
        metrics = [[{
          expression = "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Throttles\" ${local.name_prefix}', 'Sum', 300)"
          id         = "throttles"
        }]]
      }
    },

    # Bedrock
    {
      type = "metric", width = 12, height = 6
      properties = {
        title = "Bedrock embeddings (5 min)"
        stat  = "Sum"
        metrics = concat(
          [for m in local.bedrock_embedding_models : ["AWS/Bedrock", "Invocations", "ModelId", m]],
          [for m in local.bedrock_embedding_models : ["AWS/Bedrock", "InvocationThrottles", "ModelId", m]],
          [for m in local.bedrock_embedding_models : ["AWS/Bedrock", "InvocationClientErrors", "ModelId", m]],
        )
      }
    },
    {
      type = "metric", width = 12, height = 6
      properties = {
        title   = "Bedrock latency p95 (ms)"
        stat    = "p95"
        metrics = [for m in local.bedrock_embedding_models : ["AWS/Bedrock", "InvocationLatency", "ModelId", m]]
      }
    },

    # Quality and reconciliation
    {
      type = "metric", width = 12, height = 6
      properties = {
        title  = "Retrieval quality per table (%; latest evaluation)"
        stat   = "Maximum"
        period = 86400
        view   = "singleValue"
        metrics = [for p in setproduct(local.eval_tables, ["Eval_recall_at_5", "Eval_recall_at_10", "Eval_mrr"]) :
          concat(["Ingestion", p[1]], local.svc, ["table", p[0], { label = "${p[0]} ${trimprefix(p[1], "Eval_")}" }])
        ]
      }
    },
    {
      type = "metric", width = 12, height = 6
      properties = {
        title  = "Reconciler (hourly; drift weekly)"
        stat   = "Sum"
        period = 3600
        metrics = [for m in ["Reconciler_requeued", "Reconciler_escalated", "Reconciler_processes_completed", "Reconciler_requeued_drift_repair"] :
        concat(["Ingestion", m], local.svc)]
      }
    },

    # Database
    {
      type = "metric", width = 8, height = 6
      properties = {
        title   = "Database CPU (%)"
        stat    = "Average"
        metrics = [concat(["AWS/RDS", "CPUUtilization"], local.db_dim)]
      }
    },
    {
      type = "metric", width = 8, height = 6
      properties = {
        title = "Database connections"
        stat  = "Maximum"
        metrics = concat(
          [concat(["AWS/RDS", "DatabaseConnections"], local.db_dim, [{ label = "database" }])],
          [for m in [["AWS/RDS", "ClientConnections", "ProxyName", local.name_prefix, { label = "proxy clients" }]] : m if local.use_aurora],
        )
      }
    },
    # Control API
    {
      type = "metric", width = 12, height = 6
      properties = {
        title = "Control API requests (5 min)"
        stat  = "Sum"
        metrics = [for m in ["Count", "4XXError", "5XXError"] :
        ["AWS/ApiGateway", m, "ApiName", "${local.name_prefix}-control", "Stage", module.control_api.stage_name]]
      }
    },
    {
      type = "metric", width = 12, height = 6
      properties = {
        title   = "Control API latency p95 (ms)"
        stat    = "p95"
        metrics = [["AWS/ApiGateway", "Latency", "ApiName", "${local.name_prefix}-control", "Stage", module.control_api.stage_name]]
      }
    },
  ]

  # The third database panel depends on database_mode.
  db_mode_widgets = [
    {
      mode = "aurora"
      widget = {
        type = "metric", width = 8, height = 6
        properties = {
          title = "Aurora capacity (ACU) and buffer cache hit (%)"
          stat  = "Average"
          metrics = [
            concat(["AWS/RDS", "ServerlessDatabaseCapacity"], local.db_dim),
            concat(["AWS/RDS", "BufferCacheHitRatio"], local.db_dim, [{ yAxis = "right" }]),
          ]
        }
      }
    },
    {
      mode = "rds"
      widget = {
        type = "metric", width = 8, height = 6
        properties = {
          title   = "Free storage (bytes)"
          stat    = "Minimum"
          metrics = [concat(["AWS/RDS", "FreeStorageSpace"], local.db_dim)]
        }
      }
    },
  ]
}

locals {
  dashboard_metric_widgets = concat(local.metric_widgets, [for d in local.db_mode_widgets : d.widget if d.mode == var.database_mode])
  # Metric names on the dashboard (known at plan time, unlike the body), for the stack tests.
  # tflint-ignore: terraform_unused_declarations
  dashboard_metric_names = toset(flatten([
    for w in local.dashboard_metric_widgets : [for m in w.properties.metrics : m[1] if can(tostring(m[1]))]
  ]))
}

resource "aws_cloudwatch_dashboard" "ingestion" {
  dashboard_name = local.name_prefix
  dashboard_body = jsonencode({
    widgets = concat([local.header_widget], [
      for w in local.dashboard_metric_widgets : merge(w, {
        properties = merge({ region = local.region, period = 300, view = "timeSeries" }, w.properties)
      })
    ])
  })
}
