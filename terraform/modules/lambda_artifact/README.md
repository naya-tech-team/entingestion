# lambda_artifact

A Lambda deployment package for [lambda_function](../lambda_function)'s `code`: a local zip (built by the service's `scripts/build_lambda.sh`), or a zip already in an artifact bucket (CI). The local zip is hashed only in zip mode, so S3 mode never touches the disk.

```hcl
module "lambda_artifact" {
  source = "../../modules/lambda_artifact"

  zip_path            = var.lambda_zip_path            # local build, or
  s3_bucket           = var.lambda_s3_bucket           # an artifact in S3
  s3_key              = var.lambda_s3_key              # (s3_key set = S3 mode)
  s3_source_code_hash = var.lambda_s3_source_code_hash
}

module "api_function" {
  source = "../../modules/lambda_function"
  code   = module.lambda_artifact.code
  # ...
}
```

| Output | Description |
|---|---|
| `code` | `{ zip_path, s3_bucket, s3_key, source_code_hash }` |
| `mode` | `zip` or `s3` |
