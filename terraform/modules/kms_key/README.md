# kms_key

Customer-managed KMS key with an alias. Rotation is always on.

The key policy delegates access to IAM, so identity policies in the account decide who can use the key. Service principals that act without an IAM role, such as CloudWatch Logs encrypting a log group, are added through `policy_statements`.

`admin_principals` (optional) splits administration from use: only those roles or users may change the key policy, rotation, tags or deletion, and the account root keeps only encrypt, decrypt, data keys and grants for AWS resources. Include the principal that applies Terraform, or it can't update the key policy afterwards.

```hcl
module "kms" {
  source      = "../../modules/kms_key"
  alias       = "producer-dev"
  description = "Producer service data key (dev)"
}
```

| Output | Description |
|---|---|
| `key_arn`, `key_id` | The key |
| `alias_arn`, `alias_name` | The alias |
