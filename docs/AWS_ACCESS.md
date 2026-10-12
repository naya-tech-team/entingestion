# AWS access without long-lived keys

How people and tools get AWS credentials for this project. **Nobody uses an access key.**
- You sign in to the console with MFA.
- `aws login` turns that sign-in into short-lived CLI credentials that refresh automatically.
- Terraform runs as a per-environment **deploy role** that it assumes from those credentials.

```
console sign-in (password + MFA) ──aws login──▶ profile "entingestion"        (you, short-lived)
        │ sts:AssumeRole (+MFA)
        ├──▶ role entingestion-dev-deploy   ──▶ make … ENV=dev    (dev state only)
        └──▶ role entingestion-prod-deploy  ──▶ make … ENV=prod   (prod state only)
later: GitHub Actions ──OIDC──▶ the same roles (off until github_repository is set)
```

The roles, their guardrails and the state-bucket policy live in [`terraform/bootstrap/access.tf`](../terraform/bootstrap/access.tf); the audit trail in [`trail.tf`](../terraform/bootstrap/trail.tf).

## What the deploy roles can and can't do

Each `entingestion-<env>-deploy` role:
- has AdministratorAccess, because Terraform manages IAM, VPC, RDS, Lambda and more;
- is assumable only by the people in `deploy_principal_arns`, with MFA;
- has a 1-hour session.

A **permissions boundary** (`entingestion-<env>-deploy-boundary`) caps each role. It denies:
- **Long-lived credentials:** creating IAM users, access keys, login profiles, or service-specific credentials.
- **The access layer itself:** changing the deploy roles, any `entingestion-*` policy (both boundaries), or the GitHub OIDC provider.
- **Unbounded roles:** creating a role, or setting a role's boundary, unless the boundary is this environment's **workload boundary** (below); removing a boundary.
- **Roles and policies of anything else:** role changes and `iam:PassRole` only on `producer-<env>-*`, `ingestion-<env>-*` and `ui-<env>-*` roles; policy changes only on the stacks' own policies (`ingestion-<env>-*`, `producer-*-<env>`, `producer-<env>-*`, `ui-<env>-*`). No role may be given AdministratorAccess, IAMFullAccess or PowerUserAccess.
- **The state store:** reconfiguring the state bucket (`s3:PutBucket*`: policy, versioning, public access block, …; lifecycle, encryption, replication) or deleting it, and disabling or deleting its key.
- **The audit trail:** stopping, changing or deleting it, its bucket, logs and key.
- **Account-level changes:** Organizations, account settings, aliases, password policy.
- **Other environments:**
  - their state files: the dev role can't read or write `prod/*`, and vice versa. Neither can touch `bootstrap/*`;
  - resources tagged with their `env`: both stacks tag everything with `env` (next to the mandatory tags) through provider `default_tags`;
  - their resources by name, where tags don't apply: functions (Lambda and CloudFront), queues, topics, state machines, buckets and log groups named `*-<other env>-*`, SSM parameters under `/entingestion/<other env>/`, and their DynamoDB tables (`dev-*`; prod's unprefixed `process`, `articles`, … from `table_prefixes`).

The boundary is about 4.9k of the 6,144 characters a managed policy may have; `terraform test` in `bootstrap` fails if it grows past the limit.

**Workload boundary.** Every role the stacks create (functions, workflows, the Pipe, schedules, RDS monitoring, RDS Proxy, VPC flow logs) carries `entingestion-<env>-workload-boundary`. It allows what the role's own policies grant, except: any IAM action, Organizations and account changes, CloudTrail changes, the state and trail buckets and keys, and the other environments (by tag and by name). So the deploy role can't escape its own boundary by creating a role and passing it to a service. The stacks compute its ARN (`workload_boundary_enabled`, default on; `permissions_boundary_arn` overrides it), and a `check` block plus the stack tests assert that every role carries it. A role the stacks don't name `<stack>-<env>-*` can't be created: `manage_apigw_account` (role `apigateway-cloudwatch-logs`) stays off.

The **state bucket** policy admits only the deploy roles, the people in `deploy_principal_arns` (break-glass) and the account root. Only the root may set up replication, and objects may only be written SSE-KMS: the backends set `kms_key_id` to the state key (`alias/entingestion-tfstate`). Existing objects are re-encrypted once ([RUNBOOK.md](RUNBOOK.md#state-encryption-one-time)).

**Audit trail.** `entingestion-account-trail` records management events (read and write) in every region, and object-level reads and writes of the state bucket (plus `trail_data_event_bucket_arns`). Log files go to `entingestion-trail-<account>-<region>`, encrypted with their own key, with log file validation, kept `trail_retention_days` (365). No CloudWatch Logs delivery, so it costs next to nothing. Look up events in the console (CloudTrail → Event history, 90 days) or query the bucket with Athena; `aws cloudtrail validate-logs` proves no log file was changed.

> dev and prod share one account today. The boundaries stop accidents and the obvious escalation paths, but not everything: not every action supports the `env` tag keys, and name patterns only cover the stacks' naming. The hard boundary comes with separate accounts (phase 2, below).

## One-time setup

1. **Add MFA to your IAM user:** console → IAM → Users → *you* → Security credentials → *Assign MFA device* (an authenticator app).
2. **Create the deploy roles** (an admin does this once per account, with their own login):
   ```bash
   aws login --profile entingestion          # opens the browser; sign in with MFA
   export AWS_ACCOUNT_ID=<12-digit account> TF_VAR_aws_account_id=<same>
   export TF_VAR_deploy_principal_arns='["user/<you>"]'   # you must be on it (see below)
   cd terraform/bootstrap
   AWS_PROFILE=entingestion terraform init && AWS_PROFILE=entingestion terraform apply
   terraform output deploy_role_arns
   ```
   The people come from the environment, never a tracked file. To add someone, apply again with them in the list: `TF_VAR_deploy_principal_arns='["user/<you>","user/<them>"]'`. The ARN is derived from the account; a full ARN also works, for someone in another account. The plan refuses to run if you, the admin applying, aren't in the list: the state bucket policy would lock you out.
3. **Add the profiles** to `~/.aws/config`. `aws login` writes the first one; you add the role profiles yourself:
   ```ini
   [profile entingestion]
   region = us-west-2
   # login_session = …   (added by aws login)

   [profile entingestion-dev]
   role_arn          = arn:aws:iam::<account>:role/entingestion-dev-deploy
   source_profile    = entingestion
   role_session_name = <your-name>
   region            = us-west-2

   [profile entingestion-prod]
   role_arn          = arn:aws:iam::<account>:role/entingestion-prod-deploy
   source_profile    = entingestion
   role_session_name = <your-name>
   region            = us-west-2
   ```
4. **Check:**
   ```bash
   aws sts get-caller-identity --profile entingestion-dev   # …:assumed-role/entingestion-dev-deploy/<your-name>
   ```
5. **Remove the old key** once everything works: deactivate it, use the tools for a day, check CloudTrail for any use of it, then delete it. Also delete its lines from `~/.aws/credentials`.

## Daily use

```bash
aws login --profile entingestion                       # when the session has expired
make -C terraform plan  ENV=dev STACK=ingestion         # uses profile entingestion-dev
make -C terraform apply ENV=dev STACK=ingestion
```

**The Makefile:**
- sets `AWS_PROFILE=entingestion-<ENV>`;
- **refuses to run** unless the caller is `assumed-role/entingestion-<ENV>-deploy`, which prevents running prod as the dev role, or as your admin user by accident;
- accepts these switches:

| Switch | Effect |
|---|---|
| `AWS_PROFILE=…` | Use another profile, for example in another account |
| `AWS_EXPORT_CREDS=1` | The AWS CLI resolves the profile and passes plain environment credentials to Terraform. Use it if Terraform can't read an `aws login` session on your machine |
| `ALLOW_AMBIENT=1` | Break-glass: skip the role check and use whatever credentials are active |

## Other tools

| Tool | How it gets credentials |
|---|---|
| AWS CLI | `--profile entingestion-dev`, or `AWS_PROFILE` |
| `uvx awscurl` | `AWS_PROFILE=entingestion-dev` (or `--profile`) |
| `producer-cli` | `--profile entingestion-dev` |
| Python scripts (`scripts/*.py`, boto3) | `AWS_PROFILE=entingestion-dev` |
| Bruno | `awsProfile: entingestion-dev` in `bruno/environments/dev.bru`, or environment credentials |
| `make destroy` / `teardown.py` | Inherit the Makefile's profile |

If a tool's AWS SDK can't read an `aws login` session or follow the role chain, export short-lived credentials into the shell instead. They last up to 1 hour:

```bash
eval "$(tools/aws-env.sh entingestion-dev)"
```

## Troubleshooting

| Message | Fix |
|---|---|
| `No AWS session for profile entingestion-dev` | `aws login --profile entingestion` |
| `Expected role entingestion-dev-deploy, got …:user/…` | You're using your user directly. Set `AWS_PROFILE=entingestion-dev`, or check `role_arn` in the profile |
| `AccessDenied … sts:AssumeRole` | Your user isn't in `deploy_principal_arns`, or the session has no MFA (sign in again with MFA) |
| `AccessDenied` on `…/prod/…` state as the dev role | Working as intended: use `ENV=prod` with the prod profile |
| Terraform: `no valid credential sources` while the CLI works | `AWS_EXPORT_CREDS=1 make …` |
| `AccessDenied … iam:CreateRole` or `iam:PutRolePermissionsBoundary` | The workload boundary doesn't exist yet (apply bootstrap first), or the role isn't named `<stack>-<env>-*`, or `workload_boundary_enabled` is off |
| `AccessDenied … s3:PutObject` on state | The backend has no `kms_key_id`: re-run `make init`, which passes it from `AWS_ACCOUNT_ID` |
| `AWS_ACCOUNT_ID must be the 12-digit account …` / `AWS_ACCOUNT_ID is … but the credentials are …` | Set `export AWS_ACCOUNT_ID=<account>` to the account you mean to deploy to, and use credentials for that account |
| `SSL validation failed … CERTIFICATE_VERIFY_FAILED` | A TLS-inspecting proxy (Zscaler): set `AWS_CA_BUNDLE`; see [RUNBOOK](RUNBOOK.md#behind-a-tls-inspecting-proxy-zscaler) |
| A script works on the wrong region (`us-east-1`) | boto3 ignores `AWS_REGION`: set `AWS_DEFAULT_REGION=us-west-2` too, or use `tools/aws-env.sh`, which sets both |

## Phase 2

- **Separate accounts:** an AWS Organization with separate dev and prod accounts (the management account stays empty). Identity Center with permission sets replaces `aws login`. Each account gets the same deploy-role design, so only role ARNs and profile names change.
- **Separate log-group keys:** the log groups share each stack's data key today. A dedicated logs key per stack keeps log readers away from the data; it means new log groups (replacement), so it waits for a quiet window.
- **Key administrators:** `modules/kms_key` takes `admin_principals` to split key administration from use. No key uses it yet; turning it on for an existing key changes only its policy.
- **GitHub Actions:**
  1. Set `github_repository = "owner/repo"` in the bootstrap tfvars and apply.
  2. Create GitHub environments `dev` and `prod`, with reviewers on prod.
  3. Add jobs using `aws-actions/configure-aws-credentials` with `role-to-assume`. No secrets are stored in GitHub. Add read-only plan roles for pull requests.
