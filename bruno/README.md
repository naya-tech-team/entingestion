# Bruno collection: entingestion APIs

A [Bruno](https://www.usebruno.com/) collection (MIT-licensed, files in git, no cloud account) for the producer API and the ingestion control API. It includes runnable end-to-end use cases. Route reference: [docs/API.md](../docs/API.md). Scenarios: [docs/USE_CASES.md](../docs/USE_CASES.md).

```
bruno/
  bruno.json, collection.bru        collection settings; AWS SigV4 auth inherited by every request
  environments/dev.bru, prod.bru    read every deploy-specific value from process.env; allowOperations
  .gitignore                        keeps a local .env (the values below) out of git
  01 Producer API/                  create process → ticket → PUT to S3 → upload → articles → patch → close → delete; sources, uploads
  02 Ingestion status/              article status, process summary, vector tables, process articles, ops overview, articles by status
  03 Ingestion operations/          re-run, redrive, reindex, rebuild, switch / roll back, retire a table
  04 Use cases/                     UC1, UC2, UC3, UC8, UC11 end to end; UC9 dry run
  05 Search/                        hybrid search for a tenant, filters, another tenant refused
```

## Set up

1. **Install Bruno 4.x** (desktop app), or use the CLI through `npx @usebruno/cli@4.1.0`.
2. **Open the collection:** *Open Collection* → this `bruno/` folder.
3. **Set the environment variables.** Nothing deploy-specific is stored in the collection: the environments (`dev`, `prod`; pick one top right, or `--env` on the CLI) read these from `process.env`:

   | Variable | Bruno variable | Value |
   |---|---|---|
   | `PRODUCER_URL` | `producerUrl` | producer stack output `api_url` |
   | `INGESTION_URL` | `ingestionUrl` | ingestion stack output `control_api_url` |
   | `AWS_REGION` | `awsRegion` | the stacks' region (`region` in `terraform/envs/<env>/common.tfvars`) |
   | `AWS_PROFILE` | `awsProfile` | the AWS profile to sign with, for example `entingestion-dev` |
   | `BRUNO_SOURCE` | `source` | a producer source you write, for example `bruno` (dev `source_access`) |
   | `BRUNO_TENANT` | `tenantId` | the tenant of a source you read, for search, for example `internal` |
   | `LIVE_TABLE` | `liveTable` | the live vector table, `chunks_<default_profile>`, for example `chunks_primary` |
   | `NEW_TABLE` | `newTable` | another profile's table for the model-change requests, for example `chunks_compact` |

   Export them in the shell that starts Bruno or the CLI, or put them in `bruno/.env` (`NAME=value` lines; git ignores it). For example, from the stack outputs:
   ```bash
   export PRODUCER_URL=$(make -sC terraform output ENV=dev STACK=producer  | awk -F'"' '/^api_url/{print $2}')
   export INGESTION_URL=$(make -sC terraform output ENV=dev STACK=ingestion | awk -F'"' '/^control_api_url/{print $2}')
   export AWS_PROFILE=entingestion-dev BRUNO_SOURCE=bruno BRUNO_TENANT=internal
   export LIVE_TABLE=chunks_primary NEW_TABLE=chunks_compact
   ```
   (`AWS_REGION` as for the AWS CLI.) The `--env` name only labels the run; the values decide where requests go.
4. **Credentials.** Requests are signed with AWS SigV4 (service `execute-api`), with short-lived credentials only ([docs/AWS_ACCESS.md](../docs/AWS_ACCESS.md)):
   - the AWS profile in `AWS_PROFILE` (for example `entingestion-dev`, after `aws login --profile entingestion`);
   - or exported credentials: `eval "$(tools/aws-env.sh entingestion-dev)"` before starting Bruno or the CLI.

   Your principal needs the producer's `invoke_policy_arn`, plus the ingestion `control_api_reader_policy_arn` or `control_api_operator_policy_arn` (and `control_api_search_policy_arn` for 05 Search). It must also be a writer of `BRUNO_SOURCE` in the producer's `source_access`, and a reader of a source whose tenant is `BRUNO_TENANT` to search.

No secrets are stored in the collection.

`02 Ingestion status` also has **Ops overview** (`GET /v1/ops/overview`: in-progress and failed counts, queue depths) and **Articles by status** (`GET /v1/articles?status=FAILED_PERMANENT&limit=50`, paged with `next_token`).

## Use it

**One request at a time:** run `01 Producer API` top to bottom. Each request saves the ids the next one needs (`processId`, `uploadId`, `articleId`, `uploadUrl`, `uploadHeaders`). The ticket request builds a small test document and computes its size and SHA-256. The PUT sends it with exactly the ticket's headers.

**A whole folder** (collection runner, or the CLI):

```bash
cd bruno
npx @usebruno/cli@4.1.0 run "04 Use cases/UC1 New article" --env dev     # one use case
npx @usebruno/cli@4.1.0 run -r --env dev                                 # everything (about 5 minutes on dev)
```

**Polling.** The "Wait until …" requests poll in runner mode: every 5 seconds, up to 4 minutes, until the pipeline reaches the expected state. Their tests are asserted only on the final poll. Sent on its own, a wait request runs once.

**Guard for real operations.** Requests in `03 Ingestion operations` that start real work are skipped in folder and collection runs unless the environment sets `allowOperations: true`. Those are:
- re-run with force;
- redrive by error code;
- reindex and rebuild;
- switch and roll back the live table.

Sent individually, they always run. The dry-run requests always run.

**Cost and data.** Every use-case run creates one small text article (source `BRUNO_SOURCE`) in the target environment. Keep runs few in dev. Delete the articles afterwards with `01 Producer API / 10 Delete article`, or the producer CLI.

## Conventions

- **Request names must be unique** across the collection, because `bru.runner.setNextRequest()` finds requests by name. Use-case requests are prefixed with their UC id.
- **No deploy-specific values in files.** URLs, region, profile, source, tenant and table names come from `process.env` through the environments; request bodies use `{{liveTable}}` / `{{newTable}}`, never a table name.
- **Scripts** use only Bruno built-ins and `crypto-js` (whitelisted in `bruno.json`); filesystem access is off.
- **Adding a request:** create it in Bruno (it writes a `.bru` file here), set `auth: inherit`, add a `docs` block that says what it does, and add tests for the status and the key fields.
