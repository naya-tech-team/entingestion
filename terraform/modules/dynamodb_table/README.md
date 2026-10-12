# dynamodb_table

On-demand DynamoDB table. These are always on:
- Point-in-time recovery
- SSE with a customer-managed KMS key
- `prevent_destroy`

Deletion protection, GSIs, the stream and TTL are set through inputs.

```hcl
module "articles" {
  source      = "../../modules/dynamodb_table"
  name        = "articles"
  hash_key    = "article_id"
  kms_key_arn = module.kms.key_arn
  attributes = [
    { name = "article_id", type = "S" },
    { name = "process_id", type = "S" },
    { name = "updated_at", type = "S" },
  ]
  global_secondary_indexes = [
    { name = "gsi_process", hash_key = "process_id", range_key = "updated_at" },
  ]
  stream_view_type = "NEW_AND_OLD_IMAGES"
}
```

Because `prevent_destroy` is always set, `terraform destroy` refuses to remove these tables. To tear down a disposable environment, first run `terraform state rm` on the table and then delete it outside Terraform.

| Output | Description |
|---|---|
| `name`, `arn` | The table |
| `index_arns` | `<table_arn>/index/*`, for IAM policies |
| `stream_arn` | Stream ARN, or null when the stream is disabled |
| `settings` | Effective settings (keys, stream, TTL, PITR, SSE, GSIs), for plan assertions |
