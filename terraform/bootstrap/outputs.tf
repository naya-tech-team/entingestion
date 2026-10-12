output "state_bucket" {
  description = "The bucket in every envs/<env>/<stack>.s3.tfbackend for this account."
  value       = module.state_bucket.id
}

output "state_kms_key_arn" {
  description = "KMS key that encrypts state objects."
  value       = module.kms.key_arn
}

output "deploy_role_arns" {
  description = "Deploy role per environment; put each in the matching AWS profile (docs/AWS_ACCESS.md)."
  value       = { for env, role in aws_iam_role.deploy : env => role.arn }
}

output "workload_boundary_arns" {
  description = "Workload permissions boundary per environment; the stacks set it on every role they create (permissions_boundary_arn)."
  value       = { for env, policy in aws_iam_policy.workload_boundary : env => policy.arn }
}

output "trail_arn" {
  description = "The account CloudTrail trail."
  value       = aws_cloudtrail.account.arn
}

output "trail_bucket" {
  description = "Bucket holding the trail's log files (AWSLogs/<account>/CloudTrail/...)."
  value       = module.trail_bucket.id
}
