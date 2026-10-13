# A Lambda deployment package as modules/lambda_function's `code`: a local zip, or a zip in S3.
# The local zip is hashed only in zip mode, so S3 mode never reads the disk.

locals {
  use_s3   = var.s3_key != null
  zip_hash = one([for p in compact([local.use_s3 ? "" : var.zip_path]) : filebase64sha256(p)])
}
