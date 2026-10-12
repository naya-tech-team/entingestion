# bootstrap

Creates the S3 bucket (with its KMS key) that holds Terraform state for every stack in one account. Run it once per account, before any stack, and again whenever it changes (before the stacks).

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

It also creates:
- the **deploy roles** `entingestion-<env>-deploy`. Terraform runs as these, through `aws login` with MFA, so nobody needs an access key. Each has a permissions boundary, `entingestion-<env>-deploy-boundary`;
- the **workload boundaries** `entingestion-<env>-workload-boundary`. Every role the stacks create must carry the one of its environment (the deploy boundary refuses anything else), and it denies IAM, so those roles can't be used to escape the deploy role's boundary;
- the **state-bucket policy**: only those roles, the people in `deploy_principal_arns` and the account root; replication only by root; objects only SSE-KMS;
- the **audit trail** `entingestion-account-trail` ([trail.tf](trail.tf)): every region's management events plus object events on the state bucket, in its own encrypted, versioned bucket.

See [docs/AWS_ACCESS.md](../../docs/AWS_ACCESS.md). Apply it as an admin with `AWS_PROFILE=entingestion`.

The bucket name it outputs goes into each `envs/<env>/<stack>.s3.tfbackend`, together with `kms_key_id` (the alias ARN `arn:aws:kms:<region>:<account>:alias/entingestion-tfstate`; the S3 backend accepts aliases). State written before `kms_key_id` was set is SSE-S3: re-encrypt it once ([RUNBOOK.md](../../docs/RUNBOOK.md#state-encryption-one-time)).

| Variable | Default | |
|---|---|---|
| `deploy_principal_arns` | | People who may assume the deploy roles (and use the state bucket as break-glass) |
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
   AWS_PROFILE=entingestion terraform init -migrate-state -backend-config=bootstrap.s3.tfbackend
   ```
   Answer `yes` to copy the local state. Check with `terraform state list`, then delete the local `terraform.tfstate*` files.
3. Commit the uncommented block. From then on, everyone runs `terraform init -backend-config=bootstrap.s3.tfbackend`.

To go back to local state: comment the block out again and run `terraform init -migrate-state`.
