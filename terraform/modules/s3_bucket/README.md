# s3_bucket

Private S3 bucket with secure defaults. These are always on:
- All four public access block settings
- `BucketOwnerEnforced` object ownership (ACLs disabled)
- Default encryption: SSE-KMS with an S3 bucket key when `kms_key_arn` is set, otherwise SSE-S3 (AES256). Leave `kms_key_arn` unset only for content that isn't sensitive, such as a static website served by CloudFront, and add a justified `CKV_AWS_145` skip to the module call
- A bucket policy that denies requests not made over TLS

Versioning, lifecycle rules, CORS, access logging and extra bucket policy statements are set through inputs.

```hcl
module "raw" {
  source      = "../../modules/s3_bucket"
  name        = "producer-raw-dev-123456789012"
  kms_key_arn = module.kms.key_arn
  lifecycle_rules = [{
    id                                     = "default"
    abort_incomplete_multipart_upload_days = 7
    noncurrent_version_transitions         = [{ days = 30, storage_class = "GLACIER_IR" }]
  }]
}
```

| Output | Description |
|---|---|
| `id`, `arn` | Bucket name and ARN |
| `regional_domain_name` | Regional endpoint domain |
| `settings` | Effective settings (force_destroy, versioning, lifecycle, CORS), for plan assertions |
