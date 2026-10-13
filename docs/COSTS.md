# What an environment costs

What each environment costs when it's idle (deployed, no documents arriving), and what grows with traffic. Prices are approximate on-demand list prices for us-west-2 (the region in the tracked `common.tfvars`; other regions differ); check the AWS pricing pages before you budget, and Cost Explorer (tag `env`) for the real bill.

## Idle, per month

| Item | dev | prod | Why it costs at idle |
|---|---|---|---|
| Database | RDS `db.t4g.micro` + 20 GB: ~$15 (free tier: $0 for its first 12 months) | Aurora Serverless v2, 2 instances at the 0.5 ACU minimum: ~$88 (0.5 ACU x $0.12/ACU-h x 730 h x 2) + storage | Serverless v2 never scales below `aurora_min_acu`; the reader exists for failover (`aurora_instance_count`) |
| RDS Proxy | none (not on the free plan) | ~$88: billed on at least 8 ACUs ($0.015/ACU-h) whatever Aurora uses | The minimum, not usage, sets the price at idle |
| Interface endpoints | Secrets Manager in 1 AZ: ~$7.30 | 2 AZs (`interface_endpoint_az_count`): ~$14.60 | $0.01 per endpoint per AZ-hour. S3 and DynamoDB use free gateway endpoints; there's no NAT gateway |
| WAF | 2 web ACLs (producer API, control API), 3 managed rule groups each: ~$16 | ~$16 | $5 per ACL + $1 per rule group per month |
| CloudWatch | ~45 alarms (~$5; metric math alarms bill per metric), 1 dashboard (~$3), custom metrics published every hour (backlog per status, ~25: ~$8) | the same | Metrics cost only in months they're published; the reconciler publishes the backlog hourly |
| VPC flow logs | < $1 (little traffic) | < $1 | $0.50 per GB ingested |
| CloudTrail | management events: free (first trail); data events on the state bucket only: cents | the same | Data events are $0.10 per 100,000; only the state bucket and listed buckets are logged |
| KMS keys | 2 (ingestion, producer): $2 | 4 (+ the 2 backup copy keys in `backup_copy_region`): $4 | $1 per key per month; the state and trail keys are shared ($2) |
| Secrets Manager | the RDS-managed master secret: $0.40 | $0.40 | |
| AWS Backup | none (`backup_enabled = false`) | a few dollars at small data: DynamoDB backups $0.10/GB-month, Aurora snapshots $0.021/GB-month, plus $0.02/GB copied to `backup_copy_region` (us-east-2 in the prod tfvars) each day | Prod only |
| DynamoDB, S3, Lambda, Step Functions, SQS, EventBridge, Scheduler | ~$1 (storage, PITR, the hourly reconciler) | ~$1 plus storage | On-demand; idle costs only storage |
| Upload UI ([UI.md](UI.md)) | ~$0 | ~$0 at idle; a WAF web ACL (~$6-8) once added, a prod prerequisite | See the line items below |
| **Total, idle** | **~$35-50** (less while free-tier credits last) | **~$230 + storage** | |

**Upload UI, line by line.** Its idle cost is about $0 in dev.

| Item | Free each month | Beyond that |
|---|---|---|
| CloudFront data transfer and requests (PriceClass_100) | 1 TB out and 10 million requests (always free) | $0.085/GB and $0.0075-0.01 per 10,000 HTTPS requests |
| CloudFront Function (basic auth, every request) | 2 million invocations | $0.10 per million |
| KeyValueStore reads (one per request) | none | $0.03 per million reads; a few cents at most |
| BFF Lambda (`ui-<env>-bff`, 256 MB) | 1 million requests and 400,000 GB-s (shared with the other functions) | $0.20 per million requests + duration |
| Site bucket (SSE-S3, no KMS key) | | A few MB of static files: cents |
| Log group | | Encrypted with the producer's key, so no new $1/month key |

File uploads go straight to the raw bucket and are billed like any other producer upload.

The two biggest prod items are fixed minimums: Aurora's 0.5 ACU per instance and RDS Proxy's 8-ACU floor. One Aurora instance (`aurora_instance_count = 1`) saves ~$44 at the price of a slower failover (Aurora creates a new instance). Dropping RDS Proxy would need connection handling in Index instead; not planned.

## Per document

Dominated by the embedding model. With the tracked profiles (Titan Text Embeddings v2 on Bedrock, $0.02 per million input tokens), an article of 5,000 tokens costs about $0.0001 to embed (the embedding cache makes re-runs of unchanged chunks free). Then about 12 Step Functions state transitions ($0.0003), ~12 Lambda invocations, a dozen DynamoDB writes, and the staged files in S3 (kept while the article exists; older versions expire). OCR (Textract `AnalyzeDocument` with layout and tables) is far more: about $0.02 per page, so scanned PDFs dominate any bill they're in. Reranking (`rerank_enabled`) is billed per search query.

## Dev on the AWS free plan

The free plan bounds what dev can run: no Aurora (only "express" clusters Terraform can't create), no RDS Proxy, no Textract (OCR is off), no AWS Backup copies, a Lambda concurrency limit of 10 and RDS backups of at most 1 day. Spend comes out of the plan's credits until they run out. Dev and prod share the account, so they also share its Lambda concurrency and Bedrock quotas: prod reserves concurrency for its critical functions (RUNBOOK "Prerequisites"), and heavy dev load can still raise prod's Bedrock throttling alarm.
