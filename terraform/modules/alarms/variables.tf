variable "topic_name" {
  description = "SNS topic that every alarm notifies."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key for the topic. Its key policy must let cloudwatch.amazonaws.com use it."
  type        = string
}

variable "email_endpoints" {
  description = "Email addresses subscribed to the topic (each must confirm by email)."
  type        = list(string)
  default     = []
}

variable "notify_on_ok" {
  description = "Also notify the topic when an alarm returns to OK. Off by default: one message per incident, not two."
  type        = bool
  default     = false
}

variable "alarms" {
  description = <<-EOT
    CloudWatch metric alarms, keyed by a short name used in the alarm name. Each alarm watches
    either one metric (namespace, metric_name, dimensions and statistic or extended_statistic)
    or a metric math `expression` over `metrics`, keyed by the ids the expression uses (ids
    start with a lowercase letter). An expression alarm uses each metric's own `stat` and the
    alarm's `period`.
  EOT
  type = map(object({
    description        = string
    namespace          = optional(string)
    metric_name        = optional(string)
    dimensions         = optional(map(string), {})
    statistic          = optional(string)
    extended_statistic = optional(string)
    expression         = optional(string)
    metrics = optional(map(object({
      namespace   = string
      metric_name = string
      dimensions  = optional(map(string), {})
      stat        = string
    })), {})
    threshold           = number
    period              = optional(number, 300)
    evaluation_periods  = optional(number, 1)
    datapoints_to_alarm = optional(number)
    comparison_operator = optional(string, "GreaterThanOrEqualToThreshold")
    treat_missing_data  = optional(string, "notBreaching")
  }))

  validation {
    condition = alltrue([
      for a in values(var.alarms) : a.expression != null || (a.statistic == null) != (a.extended_statistic == null)
    ])
    error_message = "Each single-metric alarm needs exactly one of statistic or extended_statistic."
  }

  validation {
    condition = alltrue([
      for a in values(var.alarms) : a.expression == null ? (a.namespace != null && a.metric_name != null && length(a.metrics) == 0) : (
        length(a.metrics) > 0 && a.namespace == null && a.metric_name == null && a.statistic == null && a.extended_statistic == null
      )
    ])
    error_message = "An alarm watches either namespace + metric_name (no metrics), or an expression over metrics (no namespace, metric_name or statistic)."
  }

  validation {
    condition = alltrue(flatten([
      for a in values(var.alarms) : [for id in keys(a.metrics) : can(regex("^[a-z][a-zA-Z0-9_]*$", id)) && id != "e1"]
    ]))
    error_message = "Metric ids must start with a lowercase letter (letters, digits, _), and e1 is the expression's own id."
  }

  validation {
    condition = alltrue([
      for a in values(var.alarms) : contains(["SampleCount", "Average", "Sum", "Minimum", "Maximum"], coalesce(a.statistic, "Sum"))
    ])
    error_message = "statistic must be SampleCount, Average, Sum, Minimum or Maximum (percentiles go in extended_statistic)."
  }

  validation {
    condition = alltrue([
      for a in values(var.alarms) : can(regex("^p([0-9]{1,2}(\\.[0-9]+)?|100)$", coalesce(a.extended_statistic, "p95")))
    ])
    error_message = "extended_statistic must be a percentile such as p95 or p99.9."
  }

  validation {
    condition = alltrue([
      for a in values(var.alarms) : contains(["missing", "ignore", "breaching", "notBreaching"], a.treat_missing_data)
    ])
    error_message = "treat_missing_data must be missing, ignore, breaching or notBreaching."
  }
}
