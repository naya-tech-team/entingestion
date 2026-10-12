# scheduler_schedule

An EventBridge Scheduler schedule that calls one target, usually a Lambda function, together with the role the scheduler assumes to call it.

- **Least privilege:** the role can only perform `target_actions` (default `lambda:InvokeFunction`) on `target_arn`. Its trust policy accepts only schedules from this account.
- **Encryption:** the schedule is encrypted with the given customer managed key, and the role may decrypt with it.
- **Delivery:** failed calls are retried (`retry_attempts`, `max_event_age_seconds`). Runs that still can't be delivered go to `dead_letter_queue_arn` when one is given.

```hcl
module "reconcile_schedule" {
  source              = "../../modules/scheduler_schedule"
  name                = "ingestion-dev-reconciler"
  schedule_expression = "rate(1 hour)"
  target_arn          = module.reconciler.function_arn
  kms_key_arn         = module.kms.key_arn
}
```

| Output | Description |
|---|---|
| `schedule_arn` | Schedule ARN |
| `role_arn` | Role the scheduler assumes |
| `settings` | Effective settings, for plan assertions |
