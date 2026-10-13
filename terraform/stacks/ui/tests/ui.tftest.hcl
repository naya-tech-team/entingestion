# Plan-time assertions for the upload UI stack (docs/UI.md). The producer and ingestion values
# it reads from SSM are overridden below; nothing here calls AWS.
mock_provider "aws" {
  # Make mocked computed values (ARNs) known at plan time so assertions can compare them.
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn                         = "arn:aws:s3:::ui-dev-site-123456789012-us-west-2"
      bucket_regional_domain_name = "ui-dev-site-123456789012-us-west-2.s3.us-west-2.amazonaws.com"
    }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      id          = "E2EXAMPLE"
      arn         = "arn:aws:cloudfront::123456789012:distribution/E2EXAMPLE"
      domain_name = "d111111abcdef8.cloudfront.net"
    }
  }
  mock_resource "aws_cloudfront_key_value_store" {
    defaults = { arn = "arn:aws:cloudfront::123456789012:key-value-store/0d2a7a1b-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_cloudfront_function" {
    defaults = { arn = "arn:aws:cloudfront::123456789012:function/ui-dev-basic-auth" }
  }
  mock_resource "aws_cloudfront_origin_access_control" {
    defaults = { id = "E1OAC" }
  }
  mock_resource "aws_cloudfront_response_headers_policy" {
    defaults = { id = "rhp-0001" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-west-2:123456789012:function:ui-dev-bff", version = "1" }
  }
  mock_resource "aws_lambda_alias" {
    defaults = {
      arn        = "arn:aws:lambda:us-west-2:123456789012:function:ui-dev-bff:live"
      invoke_arn = "arn:aws:apigateway:us-west-2:lambda:path/2015-03-31/functions/arn:aws:lambda:us-west-2:123456789012:function:ui-dev-bff:live/invocations"
    }
  }
  mock_resource "aws_lambda_function_url" {
    defaults = { function_url = "https://abcdefghijklmnop.lambda-url.us-west-2.on.aws/" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/ui-dev-bff" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-west-2:123456789012:log-group:/aws/lambda/ui-dev-bff" }
  }
}

# What the producer and ingestion stacks publish (contract: docs/UI.md).
override_data {
  target = data.aws_ssm_parameter.upstream["producer_api_url"]
  values = { value = "https://prod111.execute-api.us-west-2.amazonaws.com/prod" }
}
override_data {
  target = data.aws_ssm_parameter.upstream["producer_api_execution_arn"]
  values = { value = "arn:aws:execute-api:us-west-2:123456789012:prod111" }
}
override_data {
  target = data.aws_ssm_parameter.upstream["producer_raw_bucket_name"]
  values = { value = "producer-dev-raw-123456789012-us-west-2" }
}
override_data {
  target = data.aws_ssm_parameter.upstream["producer_kms_key_arn"]
  values = { value = "arn:aws:kms:us-west-2:123456789012:key/11111111-1111-1111-1111-111111111111" }
}
override_data {
  target = data.aws_ssm_parameter.upstream["control_api_url"]
  values = { value = "https://ctl222.execute-api.us-west-2.amazonaws.com/v1" }
}
override_data {
  target = data.aws_ssm_parameter.upstream["control_api_execution_arn"]
  values = { value = "arn:aws:execute-api:us-west-2:123456789012:ctl222" }
}

variables {
  env            = "dev"
  region         = "us-west-2"
  aws_account_id = "123456789012"
  mandatory_tags = {
    created_by           = "test"
    created_date         = "2026-01-01"
    owner                = "test"
    internal_client_code = "test"
    client               = "test"
    project_name         = "test"
    division             = "test"
  }

  # S3 artifact mode, so tests don't need a built zip.
  lambda_s3_bucket           = "artifacts"
  lambda_s3_key              = "ui-bff/test.zip"
  lambda_s3_source_code_hash = "abc="
}

# -- Origins and access control ------------------------------------------------------------

run "origin_access_controls_sign_every_request" {
  command = plan

  assert {
    condition = (
      aws_cloudfront_origin_access_control.site.origin_access_control_origin_type == "s3"
      && aws_cloudfront_origin_access_control.site.signing_behavior == "always"
      && aws_cloudfront_origin_access_control.site.signing_protocol == "sigv4"
    )
    error_message = "The site bucket is reached through an S3 OAC that always signs."
  }

  assert {
    condition = (
      aws_cloudfront_origin_access_control.bff.origin_access_control_origin_type == "lambda"
      && aws_cloudfront_origin_access_control.bff.signing_behavior == "always"
      && aws_cloudfront_origin_access_control.bff.signing_protocol == "sigv4"
    )
    error_message = "The BFF is reached through a Lambda OAC that always signs (replacing the viewer's basic auth)."
  }

  assert {
    condition = (
      one([for o in aws_cloudfront_distribution.this.origin : o if o.origin_id == "site"]).domain_name == "ui-dev-site-123456789012-us-west-2.s3.us-west-2.amazonaws.com"
      && one([for o in aws_cloudfront_distribution.this.origin : o if o.origin_id == "site"]).origin_access_control_id == "E1OAC"
      && one([for o in aws_cloudfront_distribution.this.origin : o if o.origin_id == "bff"]).domain_name == "abcdefghijklmnop.lambda-url.us-west-2.on.aws"
      && one(one([for o in aws_cloudfront_distribution.this.origin : o if o.origin_id == "bff"]).custom_origin_config).origin_protocol_policy == "https-only"
    )
    error_message = "Origins: the site bucket's regional domain and the function URL host, both through their OAC."
  }
}

run "site_bucket_is_read_only_by_this_distribution" {
  command = plan

  assert {
    condition = (
      length(data.aws_iam_policy_document.site_read_by_cloudfront.statement) == 1
      && one(data.aws_iam_policy_document.site_read_by_cloudfront.statement).actions == toset(["s3:GetObject"])
      && one(one(data.aws_iam_policy_document.site_read_by_cloudfront.statement).principals).identifiers == toset(["cloudfront.amazonaws.com"])
      && one(one(data.aws_iam_policy_document.site_read_by_cloudfront.statement).condition).variable == "AWS:SourceArn"
      && one(one(data.aws_iam_policy_document.site_read_by_cloudfront.statement).condition).values == tolist(["arn:aws:cloudfront::123456789012:distribution/E2EXAMPLE"])
    )
    error_message = "Only this distribution (OAC, AWS:SourceArn) may read the site bucket."
  }

  assert {
    condition     = module.site_bucket.settings.lifecycle_rules[0].noncurrent_version_expiration_days == 30 && module.site_bucket.settings.force_destroy
    error_message = "Old builds expire; the bucket holds only a rebuildable artefact."
  }
}

run "site_bucket_uses_sse_s3" {
  command = plan

  assert {
    condition     = module.site_bucket.settings.lifecycle_rules[0].abort_incomplete_multipart_upload_days == 7
    error_message = "Incomplete uploads are cleaned up."
  }

  assert {
    condition     = module.site_bucket.settings.sse_algorithm == "AES256"
    error_message = "Static assets use SSE-S3, so CloudFront needs no KMS grant."
  }
}

# -- The BFF ---------------------------------------------------------------------------------

run "function_url_is_iam_only_and_invoked_by_this_distribution" {
  command = plan

  assert {
    condition     = aws_lambda_function_url.bff.authorization_type == "AWS_IAM" && aws_lambda_function_url.bff.qualifier == "live"
    error_message = "The function URL requires IAM auth and serves the live alias."
  }

  assert {
    condition = (
      aws_lambda_permission.cloudfront_invoke_url.action == "lambda:InvokeFunctionUrl"
      && aws_lambda_permission.cloudfront_invoke_url.function_url_auth_type == "AWS_IAM"
      && aws_lambda_permission.cloudfront_invoke.action == "lambda:InvokeFunction"
      && aws_lambda_permission.cloudfront_invoke.invoked_via_function_url
    )
    error_message = "CloudFront needs both lambda:InvokeFunctionUrl and lambda:InvokeFunction (through the URL only)."
  }

  assert {
    condition = alltrue([
      for p in [aws_lambda_permission.cloudfront_invoke_url, aws_lambda_permission.cloudfront_invoke] :
      p.principal == "cloudfront.amazonaws.com"
      && p.source_arn == "arn:aws:cloudfront::123456789012:distribution/E2EXAMPLE"
      && p.qualifier == "live"
    ])
    error_message = "Only this distribution may invoke the BFF."
  }
}

run "bff_matches_the_contract" {
  command = plan

  assert {
    condition = (
      module.bff.function_name == "ui-dev-bff"
      && module.bff.settings.handler == "bff.app.handler"
      && module.bff.settings.runtime == "python3.12"
      && module.bff.settings.architectures == tolist(["arm64"])
      && module.bff.settings.timeout < 30
    )
    error_message = "ui-<env>-bff runs bff.app.handler on python3.12/arm64 and stops before CloudFront's 30 s origin timeout."
  }

  assert {
    condition = (
      module.bff.settings.environment["PRODUCER_API_URL"] == "https://prod111.execute-api.us-west-2.amazonaws.com/prod"
      && module.bff.settings.environment["CONTROL_API_URL"] == "https://ctl222.execute-api.us-west-2.amazonaws.com/v1"
    )
    error_message = "The BFF gets both API URLs from SSM."
  }
}

run "bff_may_invoke_only_its_allow_list" {
  command = plan

  assert {
    condition = (
      toset(flatten([for s in data.aws_iam_policy_document.bff.statement : s.resources])) == toset([
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/GET/v1/sources",
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/GET/v1/processes",
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/GET/v1/processes/*",
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/GET/v1/processes/*/uploads",
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/POST/v1/processes",
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/POST/v1/processes/*/close",
        "arn:aws:execute-api:us-west-2:123456789012:prod111/*/POST/v1/processes/*/uploads",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/GET/v1/processes/*/summary",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/GET/v1/processes/*/articles",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/GET/v1/articles/*/status",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/GET/v1/ops/overview",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/GET/v1/articles",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/GET/v1/tables",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/search",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/ingest",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/processes/*/ingest",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/dlq/redrive",
      ])
    )
    error_message = "The BFF role may invoke exactly the allow-listed upstream routes."
  }

  assert {
    condition = alltrue([
      for r in flatten([for s in data.aws_iam_policy_document.bff.statement : s.resources]) :
      !strcontains(r, "/DELETE/") && !strcontains(r, "/PATCH/") && !strcontains(r, "/PUT/")
      && !strcontains(r, "/*/*/") && !strcontains(r, "reindex")
      && !strcontains(r, "/v1/tables/")
    ])
    error_message = "No DELETE, PATCH or PUT, no method wildcard, no reindex and no table writes (only GET /v1/tables)."
  }

  assert {
    condition = length(setintersection(
      toset(flatten([for s in data.aws_iam_policy_document.bff.statement : s.resources])),
      toset([
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/reindex",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/tables/active",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/DELETE/v1/tables/writes/*",
      ])
    )) == 0
    error_message = "Model operations (reindex, table switch, retire) stay CLI-only."
  }

  assert {
    condition = alltrue([
      for r in flatten([for s in data.aws_iam_policy_document.bff.statement : s.resources]) :
      startswith(r, "arn:aws:execute-api:us-west-2:123456789012:ctl222/") ? (strcontains(r, "/GET/") || contains([
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/search",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/ingest",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/processes/*/ingest",
        "arn:aws:execute-api:us-west-2:123456789012:ctl222/*/POST/v1/dlq/redrive",
      ], r)) : true
    ])
    error_message = "On the control API the BFF may only read, search, re-run and redrive."
  }

  assert {
    condition     = toset(flatten([for s in data.aws_iam_policy_document.bff.statement : s.actions])) == toset(["execute-api:Invoke"])
    error_message = "The BFF's own policy grants only execute-api:Invoke."
  }
}

run "every_role_carries_the_workload_boundary" {
  command = plan

  assert {
    condition = (
      length(local.role_permissions_boundaries) == 1
      && alltrue([for b in local.role_permissions_boundaries : b == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary"])
    )
    error_message = "The BFF role must carry entingestion-<env>-workload-boundary (the deploy role can't create it otherwise)."
  }
}

run "workload_boundary_can_be_turned_off" {
  command = plan

  variables {
    workload_boundary_enabled = false
  }

  assert {
    condition     = alltrue([for b in local.role_permissions_boundaries : b == null])
    error_message = "workload_boundary_enabled = false leaves the role unbounded."
  }
}

# -- Basic auth ------------------------------------------------------------------------------

run "basic_auth_runs_on_every_behavior" {
  command = plan

  assert {
    condition = (
      aws_cloudfront_function.basic_auth.runtime == "cloudfront-js-2.0"
      && aws_cloudfront_function.basic_auth.publish
      && aws_cloudfront_function.basic_auth.key_value_store_associations == toset(["arn:aws:cloudfront::123456789012:key-value-store/0d2a7a1b-0000-0000-0000-000000000000"])
    )
    error_message = "The basic-auth function is published on cloudfront-js-2.0 with the KeyValueStore associated."
  }

  assert {
    condition = (
      one(aws_cloudfront_distribution.this.default_cache_behavior[0].function_association).event_type == "viewer-request"
      && one(aws_cloudfront_distribution.this.default_cache_behavior[0].function_association).function_arn == aws_cloudfront_function.basic_auth.arn
    )
    error_message = "The site behavior checks basic auth on viewer requests."
  }

  assert {
    condition = alltrue([
      for b in aws_cloudfront_distribution.this.ordered_cache_behavior :
      one(b.function_association).event_type == "viewer-request"
      && one(b.function_association).function_arn == aws_cloudfront_function.basic_auth.arn
    ])
    error_message = "The /api/* behavior checks basic auth on viewer requests."
  }

  assert {
    condition     = strcontains(aws_cloudfront_function.basic_auth.code, "kvs.get('auth')") && !strcontains(lower(aws_cloudfront_function.basic_auth.code), "password =")
    error_message = "The function compares against the KeyValueStore and holds no password."
  }
}

# -- Behaviors and headers -------------------------------------------------------------------

run "behaviors_cache_the_site_and_never_the_api" {
  command = plan

  assert {
    condition = (
      aws_cloudfront_distribution.this.default_cache_behavior[0].target_origin_id == "site"
      && aws_cloudfront_distribution.this.default_cache_behavior[0].cache_policy_id == "658327ea-f89d-4fab-a63d-7e88639e58f6"
      && aws_cloudfront_distribution.this.default_cache_behavior[0].allowed_methods == toset(["GET", "HEAD"])
      && aws_cloudfront_distribution.this.default_cache_behavior[0].viewer_protocol_policy == "redirect-to-https"
      && aws_cloudfront_distribution.this.default_cache_behavior[0].compress
      && aws_cloudfront_distribution.this.default_root_object == "index.html"
    )
    error_message = "The default behavior serves the SPA from S3, cached (CachingOptimized), GET/HEAD over HTTPS."
  }

  assert {
    condition = (
      length(aws_cloudfront_distribution.this.ordered_cache_behavior) == 1
      && aws_cloudfront_distribution.this.ordered_cache_behavior[0].path_pattern == "/api/*"
      && aws_cloudfront_distribution.this.ordered_cache_behavior[0].target_origin_id == "bff"
      && aws_cloudfront_distribution.this.ordered_cache_behavior[0].cache_policy_id == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
      && aws_cloudfront_distribution.this.ordered_cache_behavior[0].origin_request_policy_id == "b689b0a8-53d0-40ab-baf2-68738e2966ac"
      && contains(aws_cloudfront_distribution.this.ordered_cache_behavior[0].allowed_methods, "POST")
      && aws_cloudfront_distribution.this.ordered_cache_behavior[0].viewer_protocol_policy == "https-only"
    )
    error_message = "/api/* goes to the BFF uncached (CachingDisabled, AllViewerExceptHostHeader), all methods, HTTPS only."
  }

  assert {
    condition = (
      aws_cloudfront_distribution.this.price_class == "PriceClass_100"
      && aws_cloudfront_distribution.this.viewer_certificate[0].cloudfront_default_certificate
    )
    error_message = "Cheapest price class and the default certificate."
  }
}

run "security_headers" {
  command = plan

  assert {
    condition = alltrue([
      aws_cloudfront_distribution.this.default_cache_behavior[0].response_headers_policy_id == "rhp-0001",
      aws_cloudfront_distribution.this.ordered_cache_behavior[0].response_headers_policy_id == "rhp-0001",
    ])
    error_message = "Both behaviors send the security headers."
  }

  assert {
    condition = alltrue([
      strcontains(local.content_security_policy, "default-src 'self'"),
      strcontains(local.content_security_policy, "script-src 'self' 'wasm-unsafe-eval';"),
      strcontains(local.content_security_policy, "connect-src 'self' https://producer-dev-raw-123456789012-us-west-2.s3.us-west-2.amazonaws.com"),
      strcontains(local.content_security_policy, "frame-ancestors 'none'"),
      strcontains(local.content_security_policy, "base-uri 'none'"),
      !strcontains(local.content_security_policy, "'unsafe-inline'"),
      !strcontains(local.content_security_policy, "'unsafe-eval'"),
    ])
    error_message = "CSP: self only, plus presigned PUTs to the raw bucket's regional endpoint; no framing, no inline or eval."
  }

  assert {
    condition = (
      one(one(aws_cloudfront_response_headers_policy.security.security_headers_config).content_security_policy).content_security_policy == local.content_security_policy
      && one(one(aws_cloudfront_response_headers_policy.security.security_headers_config).strict_transport_security).access_control_max_age_sec >= 31536000
      && one(one(aws_cloudfront_response_headers_policy.security.security_headers_config).strict_transport_security).include_subdomains
      && one(one(aws_cloudfront_response_headers_policy.security.security_headers_config).frame_options).frame_option == "DENY"
      && one(one(aws_cloudfront_response_headers_policy.security.security_headers_config).referrer_policy).referrer_policy == "no-referrer"
      && length(one(aws_cloudfront_response_headers_policy.security.security_headers_config).content_type_options) == 1
    )
    error_message = "HSTS (1 year, subdomains), nosniff, frame DENY and no-referrer."
  }
}

# -- Published for the producer stack --------------------------------------------------------

run "publishes_the_origin" {
  command = plan

  assert {
    condition = (
      aws_ssm_parameter.origin.name == "/entingestion/dev/ui/origin"
      && aws_ssm_parameter.origin.value == "https://d111111abcdef8.cloudfront.net"
      && aws_ssm_parameter.origin.type == "String"
    )
    error_message = "The UI origin is published at /entingestion/<env>/ui/origin for the raw bucket's CORS."
  }
}

run "reads_the_upstream_from_ssm" {
  command = plan

  assert {
    condition = toset([for p in data.aws_ssm_parameter.upstream : p.name]) == toset([
      "/entingestion/dev/producer/api_url",
      "/entingestion/dev/producer/api_execution_arn",
      "/entingestion/dev/producer/raw_bucket_name",
      "/entingestion/dev/producer/kms_key_arn",
      "/entingestion/dev/ingestion/control_api_url",
      "/entingestion/dev/ingestion/control_api_execution_arn",
    ])
    error_message = "The SSM names must match what the producer and ingestion stacks publish."
  }
}

run "zip_mode_hashes_the_local_package" {
  command = plan

  variables {
    lambda_s3_bucket           = null
    lambda_s3_key              = null
    lambda_s3_source_code_hash = null
    lambda_zip_path            = "tests/fixtures/lambda.zip"
  }

  assert {
    condition     = module.lambda_artifact.code.zip_path == "tests/fixtures/lambda.zip" && module.lambda_artifact.code.source_code_hash == filebase64sha256("tests/fixtures/lambda.zip")
    error_message = "Zip mode deploys the local package with its hash."
  }
}

run "rejects_a_bad_env" {
  command = plan

  variables {
    env = "Dev_1"
  }

  expect_failures = [var.env]
}

run "default_tags_and_boundary_are_unchanged" {
  command = plan

  assert {
    condition = module.common.default_tags == {
      "Name"                 = "ui-dev"
      "Created By"           = "test"
      "Created Date"         = "2026-01-01"
      "Owner"                = "test"
      "Internal Client Code" = "test"
      "Client"               = "test"
      "Project Name"         = "test"
      "Division"             = "test"
      "env"                  = "dev"
    }
    error_message = "The provider tags every resource with the 8 mandatory tags (Name = ui-<env>) and env."
  }

  assert {
    condition     = local.permissions_boundary == "arn:aws:iam::123456789012:policy/entingestion-dev-workload-boundary" && module.boundary_check.all_bounded && module.bff.function_name == "ui-dev-bff"
    error_message = "The workload boundary ARN and the names are unchanged; every role carries the boundary."
  }
}
