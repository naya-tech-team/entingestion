# No providers: the module only reads the local zip.

run "zip_mode_hashes_the_local_package" {
  command = plan

  variables {
    zip_path = "tests/fixtures/lambda.zip"
  }

  assert {
    condition = (
      output.code.zip_path == "tests/fixtures/lambda.zip"
      && output.code.s3_bucket == null
      && output.code.s3_key == null
      && output.code.source_code_hash == filebase64sha256("tests/fixtures/lambda.zip")
      && output.mode == "zip"
    )
    error_message = "Zip mode deploys the local zip with its filebase64sha256."
  }
}

run "s3_mode_never_reads_the_disk" {
  command = plan

  variables {
    zip_path            = "does/not/exist.zip"
    s3_bucket           = "artifacts"
    s3_key              = "producer/abc.zip"
    s3_source_code_hash = "abc="
  }

  assert {
    condition = (
      output.code.zip_path == null
      && output.code.s3_bucket == "artifacts"
      && output.code.s3_key == "producer/abc.zip"
      && output.code.source_code_hash == "abc="
      && output.mode == "s3"
    )
    error_message = "S3 mode passes the artifact through and ignores zip_path."
  }
}

run "needs_a_package" {
  command = plan

  expect_failures = [var.s3_key]
}

run "an_s3_artifact_needs_its_bucket_and_hash" {
  command = plan

  variables {
    s3_key = "producer/abc.zip"
  }

  expect_failures = [var.s3_key]
}
