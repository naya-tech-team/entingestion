# kms_key

Customer-managed KMS key with an alias. Rotation is always on.

The key policy delegates access to IAM, so identity policies in the account decide who can use the key. Service principals that act without an IAM role are allowed by flags or `policy_statements`:
- `allow_cloudwatch_logs`: CloudWatch Logs may encrypt this account's log groups in this region with the key (statement `CloudWatchLogsEncryption`);
- `allow_cloudwatch_alarms_topic`: CloudWatch alarms of this account may publish to an SNS topic encrypted with the key (statement `CloudWatchAlarmsToEncryptedTopic`);
- `policy_statements`: anything else, appended after those.

`admin_principals` (optional) splits administration from use: only those roles or users may change the key policy, rotation, tags or deletion, and the account root keeps only encrypt, decrypt, data keys and grants for AWS resources. Include the principal that applies Terraform, or it can't update the key policy afterwards.

```hcl
module "kms" {
  source      = "../../modules/kms_key"
  alias       = "producer-dev"
  description = "Producer service data key (dev)"

  allow_cloudwatch_logs         = true # the service's log groups
  allow_cloudwatch_alarms_topic = true # its alarm topic
}
```

| Output | Description |
|---|---|
| `key_arn`, `key_id` | The key |
| `alias_arn`, `alias_name` | The alias |
| `statement_sids` | The key policy's statement ids, in order |
