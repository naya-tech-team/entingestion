# bootstrap

Creates the S3 bucket (with its KMS key) that holds Terraform state for every stack in one account. Run it once per account, before any stack, and again whenever it changes (before the stacks).

```bash
export AWS_REGION=<region>                           # the state bucket's region
export TF_VAR_region=$AWS_REGION                     # no file names the region,
export TF_VAR_aws_account_id=<12-digit account>     # nor the account,
export TF_VAR_deploy_principal_arns='["user/<you>"]'  # nor the people; you must be one of them
cd terraform/bootstrap
terraform init
terraform apply
```

The workload boundary names and the expansion of `deploy_principal_arns` come from [modules/stack_common](../modules/stack_common), the same module the stacks use, so both sides always agree.

It also creates:
- the **deploy roles** `entingestion-<env>-deploy`. Terraform runs as these, through `aws login` with MFA, so nobody needs an access key. Each has a permissions boundary, `entingestion-<env>-deploy-boundary`;
- the **workload boundaries** `entingestion-<env>-workload-boundary`. Every role the stacks create must carry the one of its environment (the deploy boundary refuses anything else), and it denies IAM, so those roles can't be used to escape the deploy role's boundary;
- the **state-bucket policy**: only those roles, the people in `deploy_principal_arns` and the account root; replication only by root; objects only SSE-KMS;
- the **audit trail** `entingestion-account-trail` ([trail.tf](trail.tf)): every region's management events plus object events on the state bucket, in its own encrypted, versioned bucket.

See [docs/AWS_ACCESS.md](../../docs/AWS_ACCESS.md). Apply it as an admin with `AWS_PROFILE=entingestion`.

The stacks' Makefile derives the bucket (`entingestion-tfstate-<account>-<region>`) and `kms_key_id` (the alias ARN `arn:aws:kms:<region>:<account>:alias/entingestion-tfstate`; the S3 backend accepts aliases) from `AWS_ACCOUNT_ID` and `STATE_REGION` (default: `AWS_REGION`, so the same region you bootstrapped in), and passes the region as `-backend-config=region=...`. The `envs/<env>/<stack>.s3.tfbackend` files hold only the key and options. State written before `kms_key_id` was set is SSE-S3: re-encrypt it once ([RUNBOOK.md](../../docs/RUNBOOK.md#state-encryption-one-time)).

| Variable | Default | |
|---|---|---|
| `region` | `TF_VAR_region` | Region of the state bucket and the trail bucket. The stacks' `STATE_REGION` must match it |
| `aws_account_id` | `TF_VAR_aws_account_id` | The account; every ARN is derived from it |
| `deploy_principal_arns` | `TF_VAR_deploy_principal_arns` | People who may assume the deploy roles (and use the state bucket as break-glass): `user/<name>` or `role/<name>` in this account, or a full ARN. The plan refuses to run if the admin applying isn't one of them (the bucket policy would lock them out) |
| `environments` | `["dev", "prod"]` | One deploy role and workload boundary each |
| `table_prefixes` | `{dev = "dev-", prod = ""}` | Each env's `table_prefix`, so the boundaries can deny the other environments' tables by name |
| `trail_retention_days` | `365` | How long trail log files are kept |
| `trail_data_event_bucket_arns` | `[]` | Extra buckets whose object reads and writes the trail records (data events are billed) |
| `require_mfa`, `github_repository` | `true`, `null` | Deploy-role trust |

## Move this stack's state into S3

This stack creates the state bucket, so its first apply keeps local state (`terraform.tfstate`; keep it out of git, re-create it with `terraform import` if lost). Once the bucket exists, move the state into it, under `bootstrap/` (which the deploy roles can't read):

1. Uncomment `backend "s3" {}` in [versions.tf](versions.tf).
2. Migrate, as an admin:
   ```bash
   AWS_PROFILE=entingestion terraform init -migrate-state -backend-config=bootstrap.s3.tfbackend \
     -backend-config=region=$AWS_REGION \
     -backend-config=bucket=entingestion-tfstate-$AWS_ACCOUNT_ID-$AWS_REGION \
     -backend-config=kms_key_id=arn:aws:kms:$AWS_REGION:$AWS_ACCOUNT_ID:alias/entingestion-tfstate
   ```
   Answer `yes` to copy the local state. Check with `terraform state list`, then delete the local `terraform.tfstate*` files.
3. Commit the uncommented block. From then on, everyone runs `terraform init` with the same four `-backend-config` flags.

To go back to local state: comment the block out again and run `terraform init -migrate-state`.
