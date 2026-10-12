output "name" {
  description = "Table name."
  value       = aws_dynamodb_table.this.name
}

output "arn" {
  description = "Table ARN."
  value       = aws_dynamodb_table.this.arn
}

output "index_arns" {
  description = "ARN pattern covering all of the table's indexes, for IAM policies."
  value       = "${aws_dynamodb_table.this.arn}/index/*"
}

output "stream_arn" {
  description = "Latest stream ARN, or null when the stream is disabled."
  value       = var.stream_view_type == null ? null : aws_dynamodb_table.this.stream_arn
}

output "settings" {
  description = "Effective table settings, for plan assertions in consuming stacks."
  value = {
    name                   = aws_dynamodb_table.this.name
    hash_key               = aws_dynamodb_table.this.hash_key
    range_key              = aws_dynamodb_table.this.range_key
    billing_mode           = aws_dynamodb_table.this.billing_mode
    stream_enabled         = aws_dynamodb_table.this.stream_enabled
    stream_view_type       = aws_dynamodb_table.this.stream_view_type
    ttl_attribute          = var.ttl_attribute
    deletion_protection    = aws_dynamodb_table.this.deletion_protection_enabled
    point_in_time_recovery = aws_dynamodb_table.this.point_in_time_recovery[0].enabled
    sse_enabled            = aws_dynamodb_table.this.server_side_encryption[0].enabled
    sse_kms_key_arn        = aws_dynamodb_table.this.server_side_encryption[0].kms_key_arn
    global_secondary_index = var.global_secondary_indexes
  }
}
