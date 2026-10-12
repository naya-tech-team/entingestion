# Bruno collection: entingestion APIs

A [Bruno](https://www.usebruno.com/) collection (MIT-licensed, files in git, no cloud account) for the producer API and the ingestion control API. It includes runnable end-to-end use cases. Route reference: [docs/API.md](../docs/API.md). Scenarios: [docs/USE_CASES.md](../docs/USE_CASES.md).

```
bruno/
  bruno.json, collection.bru        collection settings; AWS SigV4 auth inherited by every request
  environments/dev.bru, prod.bru    API URLs, AWS profile, allowOperations
  01 Producer API/                  create process → ticket → PUT to S3 → upload → articles → patch → close → delete; sources, uploads
  02 Ingestion status/              article status, process summary, vector tables, process articles
  03 Ingestion operations/          re-run, redrive, reindex, rebuild, switch / roll back, retire a table
  04 Use cases/                     UC1, UC2, UC3, UC8, UC11 end to end; UC9 dry run
  05 Search/                        hybrid search for a tenant, filters, another tenant refused
```

## Set up

1. **Install Bruno 4.x** (desktop app), or use the CLI through `npx @usebruno/cli@4.1.0`.
2. **Open the collection:** *Open Collection* → this `bruno/` folder.
3. **Pick an environment** (top right): `dev` or `prod`. Check the URLs against the stack outputs:
   ```bash
   make -sC terraform output ENV=dev STACK=producer  | grep api_url          # → producerUrl
   make -sC terraform output ENV=dev STACK=ingestion | grep control_api_url  # → ingestionUrl
   ```
4. **Credentials.** Requests are signed with AWS SigV4 (service `execute-api`), with short-lived credentials only ([docs/AWS_ACCESS.md](../docs/AWS_ACCESS.md)):
   - the AWS profile named in the environment (`awsProfile`, for example `entingestion-dev`, after `aws login --profile entingestion`);
   - or exported credentials: `eval "$(tools/aws-env.sh entingestion-dev)"` before starting Bruno or the CLI.

   Your principal needs the producer's `invoke_policy_arn`, plus the ingestion `control_api_reader_policy_arn` or `control_api_operator_policy_arn` (and `control_api_search_policy_arn` for 05 Search). It must also be a writer of the environment's `source` in the producer's `source_access`, and a reader of a source whose tenant is `tenantId` to search.

No secrets are stored in the collection.

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

**Cost and data.** Every use-case run creates one small text article (source `bruno`) in the target environment. Keep runs few in dev. Delete the articles afterwards with `01 Producer API / 10 Delete article`, or the producer CLI.

## Verified

On dev (2026-10-11, after the architecture-review fixes), the whole collection passed: 118 requests (111 passed, 7 operations skipped by the guard) and 58/58 tests. Besides the earlier coverage it checks an idempotent retry of a create, a 403 for a source the caller may not use, listing a source's processes, a conditional PATCH (If-Match), the METADATA run of UC3, and search. Earlier coverage:
- UC1: new article indexed, version 1, 8 steps;
- UC2: version 2 with embedding cache hits;
- UC3: unchanged and PATCH;
- UC8: delete;
- UC11: process COMPLETE;
- UC9: reindex dry run.

## Conventions

- **Request names must be unique** across the collection, because `bru.runner.setNextRequest()` finds requests by name. Use-case requests are prefixed with their UC id.
- **Scripts** use only Bruno built-ins and `crypto-js` (whitelisted in `bruno.json`); filesystem access is off.
- **Adding a request:** create it in Bruno (it writes a `.bru` file here), set `auth: inherit`, add a `docs` block that says what it does, and add tests for the status and the key fields.
