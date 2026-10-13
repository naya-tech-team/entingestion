# Use cases

Each use case gives the scenario, how to run it and the expected result. The ingestion use cases (UC1–UC12, DUP, CONC, TF) are the acceptance tests. The producer use cases (P1–P6) are the producer's contract. The step-by-step mechanics are in [WORKFLOWS.md](WORKFLOWS.md).

**Ways to run them:**
- **Bruno:** the folders under `04 Use cases` in [bruno/](../bruno/README.md). Run them in Bruno's collection runner, or with `cd bruno && npx @usebruno/cli@4.1.0 run "04 Use cases/<folder>" --env dev` after exporting the environment variables the collection reads (`PRODUCER_URL`, `INGESTION_URL`, `AWS_REGION`, …; see its README). They poll until the pipeline reaches the expected state, and each run creates one small article.
- **Scripts:** in `services/ingestion`, with `uv run python scripts/<script> …`. They drive the real producer CLI, the control API and the Lambdas against a deployed environment, reading every name from the stacks' Terraform outputs (`--env`, default `$ENV`, else `dev`). They write the source `$ACCEPTANCE_SOURCE` (default `acceptance`).
- **Unit tests:** `uv run pytest` in `services/ingestion` (moto, with Bedrock and pgvector stubbed).

**Keep dev runs small:** a few articles per run. Never run the 1,000-article corpus in dev.

## Summary

| ID | Scenario | Bruno | Script | Unit test |
|---|---|---|---|---|
| UC1 | New article | ✓ | `acceptance_phase2.py --only dup`, `gate_phase1.py` | ✓ |
| UC2 | Content changed | ✓ | `acceptance_phase3.py uc2` | ✓ |
| UC3 | Unchanged / metadata PATCH | ✓ | `acceptance_phase2.py --only uc3` | ✓ |
| UC4 | Bedrock throttled once | – | – (can't be forced on demand) | ✓ |
| UC5 | Broken PDF → fix → redrive | – | `acceptance_phase2.py --only uc5` | ✓ |
| UC6 | Run interrupted after Chunk's staging write | – | `acceptance_phase2.py --only uc6` (waits for the lease, 35 min) | ✓ |
| UC7 | Stream events dropped | – | `acceptance_phase2.py --only uc7` | ✓ |
| UC8 | Article deleted | ✓ | `acceptance_phase2.py --only uc8` | ✓ |
| UC9 | Model change | dry run | `acceptance_phase3.py uc9` | ✓ |
| UC10 | Rebuild from staging | – | `acceptance_phase3.py uc10` | ✓ |
| UC11 | Process settles | ✓ | `acceptance_phase2.py --only dup` | ✓ |
| UC12 | Large scanned PDF (OCR) | – | `acceptance_phase3.py uc12 [--ocr-off]` (OCR on needs a paid account) | ✓ |
| DUP | Same record delivered 3× | – | `acceptance.py dup` | ✓ |
| CONC | Two runs for one article | – | `acceptance.py conc` | ✓ |
| TF | Every module and stack | – | `make -C terraform check` | – |

The whole Bruno collection runs with `npx @usebruno/cli@4.1.0 run -r --env dev`. The operations that start real work are skipped unless `allowOperations` is true.

---

## Producer use cases

### P1. Register a batch of documents
- **Actor:** a client system or `producer-cli ingest-dir`.
- **Steps:**
  1. `POST /v1/processes`.
  2. For each file, `POST …/uploads`, then PUT to the presigned URL.
  3. `POST …/close`.
- **Expect:** each upload REGISTERED, one STAGED `articles` row per document, and the process `settled` with `registered_count` = number of files.
- **Run:** Bruno `01 Producer API` (01–09), or `services/producer/scripts/smoke.sh`.

### P2. Re-send a document (same `external_id`)
- **Expect:** the same `article_id`; the row now points at the new object version with the new `content_sha256`. Ingestion makes a new version (UC2).
- **Run:** Bruno `04 Use cases / UC2`.

### P3. A file doesn't match its declaration
- **Steps:** upload bytes that differ from the declared size or SHA-256 (S3 itself refuses a body whose SHA-256 differs).
- **Expect:** the upload ends REJECTED with a `reason` (`sha256_mismatch`, `size_mismatch`, …), and no `articles` row is written. Rejections count towards `settled`.

### P4. Late or duplicate events
- **Expect:**
  - A duplicate S3 event is a no-op.
  - An older upload of the same article arriving after a newer one becomes SUPERSEDED.
  - Counts stay exact.
- **Covered by:** producer unit tests (`services/producer/tests`).

### P5. Patch title or metadata
- **Expect:** `PATCH /v1/articles/{id}` returns 200, and ingestion records one METADATA run: the chunks' metadata is updated in place, without re-embedding or a new version (UC3). A title change re-runs the article.

### P6. Delete an article
- **Expect:** `DELETE /v1/articles/{id}` returns 204. The stream REMOVE makes ingestion remove the vectors (UC8).

---

## Ingestion use cases

### UC1. New article from the producer
- **Scenario:** a new document is uploaded and registered.
- **Run:**
  - Bruno: `04 Use cases / UC1 New article` (create → ticket → PUT → wait registered → wait indexed).
  - Script: `scripts/gate_phase1.py --corpus <dir with 3 files>`.
- **Expect:**
  - LATEST INDEXED, `doc_version = 1`, `chunk_count > 0`;
  - one STREAM run with `claim = NEW` and 8 SUCCEEDED steps;
  - 10 status records;
  - the rows are active in `chunks_active`.

### UC2. Content changed (new upload)
- **Scenario:** the same `external_id` is uploaded with an extra paragraph.
- **Run:** Bruno `04 Use cases / UC2 Changed content`, or `scripts/acceptance_phase3.py uc2`.
- **Expect:**
  - `doc_version = 2`, `claim = CHANGED`;
  - v1 is served until Index commits, and only v2 afterwards;
  - the EMBED step shows `cache_hits > 0`, with only changed chunks sent to Bedrock.

### UC3. Re-registered unchanged, or metadata PATCH
- **Run:** Bruno `04 Use cases / UC3 Unchanged and metadata PATCH`, or `scripts/acceptance_phase2.py --only uc3`.
- **Expect:**
  - one record: RUN# with outcome SKIPPED_UNCHANGED and `claim = UNCHANGED`;
  - LATEST unchanged (INDEXED, same version);
  - no Bedrock calls and no database writes;
  - counted as skipped for the process.

### UC4. Bedrock throttled once
- **Expect:** the Embed step retries on the **same** step item and ends with `attempt = 2`, SUCCEEDED.
- **Covered by:** unit test `tests/unit/test_status_store.py::test_retry_updates_the_same_step_item`. Throttling can't be forced on demand against the real service.

### UC5. Broken PDF → fix → redrive by error code
- **Run:** `scripts/acceptance_phase2.py --only uc5`.
- **Steps the script takes:**
  1. Upload an encrypted PDF. The run ends FAILED_PERMANENT with `E_UNREADABLE`, counted once as failed, with an alert.
  2. Redrive by error code (`POST /v1/dlq/redrive {"error_code": "E_UNREADABLE"}`). The new run resumes after Fetch, at Extract, and fails again. The process still counts it once.
  3. Upload a readable PDF under the same article (the fix), then close the process.
- **Expect:**
  - the fixed version is INDEXED;
  - the process counters move `failed −1, indexed +1`;
  - the summary is COMPLETE.
- **Manual equivalent:** Bruno `03 Ingestion operations / 04` (dry run), then `05` (redrive by error code).

### UC6. Crash after Chunk's staging write
- **Run:** `scripts/acceptance_phase2.py --only uc6`.
- **Steps the script takes:**
  1. Stop the Step Functions execution as it enters Embed. By then Chunk has written its staged output.
  2. Wait for the lease to expire (`LEASE_SECONDS`, 35 minutes).
  3. Invoke the reconciler.
- **Expect:**
  - a run with `trigger = RECONCILER` resumes after the last intact staged output, not at Fetch;
  - the article ends INDEXED with no duplicate `chunk_id`s;
  - the process counts it once as indexed.

### UC7. Stream events dropped
- **Run:** `scripts/acceptance_phase2.py --only uc7`.
- **Steps the script takes:**
  1. Disable the dispatcher's ingest-queue mapping.
  2. Register an article.
  3. Delete its stream message from the queue, so the event is "dropped".
  4. Re-enable the mapping.
  5. Wait out the reconciler's 5-minute grace period for stream events, then invoke the reconciler.
- **Expect:**
  - the reconciler finds the article in the producer's `gsi_updated_shard` with no LATEST and enqueues it;
  - a run with `trigger = RECONCILER` indexes it;
  - the process completes.

### UC8. Article deleted in the producer
- **Run:** Bruno `04 Use cases / UC8 Delete`, or `scripts/acceptance_phase2.py --only uc8`.
- **Expect:** the rows are gone from every vector table; LATEST is DELETED with `claim = DELETE`; 2 records.

### UC9. Model change
- **Run:**
  - Bruno `04 Use cases / UC9 Model change (dry run)` checks the plan without changing anything.
  - The full flow is `scripts/acceptance_phase3.py uc9`, or by hand: `03 Ingestion operations / 07` (reindex), then `scripts/evaluate.py --table chunks_compact`, then `09` (switch), then `10` (roll back).
- **Expect:**
  - the new table is filled from the staged `chunks/`, while the live table keeps serving;
  - the view switches; rollback works;
  - Embed writes the new model's vectors to separate staged files.

### UC10. Rebuild from staging
- **Run:** `scripts/acceptance_phase3.py uc10`. It truncates a table through `migrate`, which needs a confirm, then reindexes with `from_stage: INDEX`.
- **Expect:** the row counts are restored with **zero** Bedrock calls, and every run starts at INDEX.

### UC11. Producer process settles
- **Run:** Bruno `04 Use cases / UC11 Process settles` (upload, close, wait for the summary), or `scripts/acceptance_phase2.py --only dup`.
- **Expect:** once the producer's process is settled and every article has an outcome:
  - the ingestion summary is COMPLETE (or COMPLETE_WITH_ERRORS);
  - `counts.indexed + skipped + failed = registered_count`;
  - `process.ingested` is emitted.

### UC12. Large scanned PDF
- **Run:** `scripts/acceptance_phase3.py uc12 --pages 600` with OCR on, or `--ocr-off` to check the switched-off path.
- **Expect, OCR on:** Textract runs through OcrStart → Wait/Poll → OcrCollect; the article is INDEXED with no Lambda timeout.
- **Expect, OCR off:** a scanned PDF ends SKIPPED and its process still completes.
- **Note:** the free plan blocks Textract, so the OCR-on case needs a paid account.

### DUP. The same stream record delivered 3×
- **Run:** `scripts/acceptance.py dup`.
- **Expect:** one execution and one run. The other deliveries hit `ExecutionAlreadyExists` and are acknowledged (`Dispatch_duplicate` metric).

### CONC. Two runs for one article
- **Run:** `scripts/acceptance.py conc --article-id <indexed> --process-id <its process>`.
- **Expect:** one run wins the lease; the other ends quietly as Stale and doesn't change LATEST.

### TF. Every module
- **Run:** `make -C terraform check`.
- **Expect:** `terraform test` passes for the bootstrap, every module and every stack, the teardown script tests pass, tflint is clean, and checkov reports no failed checks. CI is disabled, so run it locally.
