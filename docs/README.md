# entingestion documentation

entingestion turns uploaded documents into searchable, versioned chunks in PostgreSQL with pgvector. It has two services, each deployed as its own Terraform stack:

- **Producer.** Clients register a batch (a *process*), upload files straight to S3 with presigned URLs, and get one verified row per document in the DynamoDB `articles` table.
- **Ingestion pipeline.** It consumes the `articles` stream. For every article version it fetches, extracts, normalizes, chunks, embeds (Amazon Bedrock Titan v2), validates and indexes the text. It keeps status per article, run and step, and stages every step's output in S3.

![System overview](diagrams/png/01-overview.png)

## Where to start

| You want to | Read |
|---|---|
| Deploy it into an account and see a document indexed | [GETTING_STARTED.md](GETTING_STARTED.md) |
| Understand the components, data model and design decisions | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Follow what happens step by step for an upload, a run, a failure, a reindex | [WORKFLOWS.md](WORKFLOWS.md) |
| See each acceptance scenario, how to run it and what to expect | [USE_CASES.md](USE_CASES.md) |
| Call the APIs (routes, bodies, responses, errors) | [API.md](API.md) and the [Bruno collection](../bruno/README.md) |
| Operate it: alarms, SLOs, deploy and rollback, redrive, reindex, backups, teardown | [RUNBOOK.md](RUNBOOK.md) |
| Upload files through the hosted page, set its login, deploy it | [UI.md](UI.md) |
| Know what an environment costs at idle | [COSTS.md](COSTS.md) |
| Get AWS credentials (no access keys; deploy roles) | [AWS_ACCESS.md](AWS_ACCESS.md) |

## Diagrams

[`diagrams/entingestion.drawio`](diagrams/entingestion.drawio) has 10 pages. Open it in draw.io (desktop, or app.diagrams.net) to edit it. A PNG of each page is in [`diagrams/png/`](diagrams/png/):

| Page | Shows |
|---|---|
| 1. System overview | Both stacks, the main flow numbered 1–12 |
| 2. Upload and registration | Sequence: process, ticket, presigned PUT, registrar, `articles` row |
| 3. Ingestion trigger and dispatch | Stream → Pipe → queues → dispatcher → one execution per version |
| 4. Ingest state machine | Every state, choice and OCR loop, plus retry and catch rules |
| 5. Status model | `ingestion_status` items, GSIs, the LATEST lifecycle |
| 6. Staging and vector store | S3 layout per step, vector tables, the `chunks_active` view, the Index swap |
| 7. Model change and rebuild | Reindex state machine, backfill, evaluate, switch, roll back |
| 8. Operations | Reconciler steps, alerts, dashboard, control API routes |
| 9. Network and security | VPC, endpoints, security groups, IAM, KMS |
| 10. Delivery and teardown | Build → plan → apply → migrate → smoke; CI; `make destroy` |

After editing the `.drawio` file, re-export the PNGs with the draw.io CLI:

```bash
cd docs/diagrams && i=1
for n in overview upload-registration ingestion-dispatch state-machine status-model staging-vectors \
         model-change operations network-security delivery-teardown; do
  drawio -x -f png -s 1 -b 10 -p $i -o png/$(printf %02d $i)-$n.png entingestion.drawio; i=$((i+1))
done
```

## Glossary

| Term | Meaning |
|---|---|
| Process | A batch registered with the producer (`prc_…`). It is *settled* when CLOSED, no upload is pending, and at least `expected_count` tickets were issued |
| Article | One document, `art_…`. The same `source` + `external_id` always maps to the same article, so re-sending a document updates it |
| Upload | One file transfer (`upl_…`): PENDING → REGISTERING → REGISTERED, or REJECTED / SUPERSEDED |
| Version (`doc_version`) | Increments when an article's content (SHA-256) changes |
| Run | One attempt to process an article version (`RUN#<run_id>`). Its trigger is STREAM, MANUAL, RECONCILER or BACKFILL |
| Step | One stage of a run: Fetch, Extract, Normalize, Chunk, Embed, Validate, Index, Finalize (plus Claim, OCR and MarkFailed) |
| LATEST | The current status item of an article |
| Staging | The S3 bucket holding each step's output per article version |
| Vector table | A pgvector table per embedding model and dimension, for example `chunks_primary` |
| `chunks_active` | The view that search reads. It points at the live vector table |
| Lease | A 15-minute claim on an article by one run, so concurrent runs can't interleave |
| Reconciler | An hourly Lambda that repairs what events missed; weekly it also checks for drift |
