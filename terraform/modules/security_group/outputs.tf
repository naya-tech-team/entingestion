output "id" {
  description = "Security group id."
  value       = aws_security_group.this.id
}

output "arn" {
  description = "Security group ARN."
  value       = aws_security_group.this.arn
}
