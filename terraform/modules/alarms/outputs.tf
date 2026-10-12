output "topic_arn" {
  description = "Alarm topic ARN; subscribe on-call tooling here."
  value       = aws_sns_topic.this.arn
}

output "alarm_names" {
  description = "Alarm names by key."
  value       = { for k, a in aws_cloudwatch_metric_alarm.this : k => a.alarm_name }
}

output "settings" {
  description = "Effective alarm definitions, for plan assertions in consuming stacks."
  value       = var.alarms
}
