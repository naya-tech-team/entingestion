# CloudFront: one distribution, two origins, basic auth on every request.

locals {
  # AWS managed policies; their ids are fixed across accounts.
  managed_policy_ids = {
    caching_optimized             = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
    caching_disabled              = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
    all_viewer_except_host_header = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # Managed-AllViewerExceptHostHeader
  }

  # 'wasm-unsafe-eval' lets the page compile WebAssembly (streaming SHA-256 of large files), not
  # evaluate JavaScript. connect-src: the API (same origin) and presigned PUTs to the raw bucket.
  content_security_policy = join("; ", [
    "default-src 'self'",
    "script-src 'self' 'wasm-unsafe-eval'",
    "style-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self' ${local.raw_bucket_origin}",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
  ])

  # https://<url id>.lambda-url.<region>.on.aws/ -> the host
  bff_origin_domain = split("/", aws_lambda_function_url.bff.function_url)[2]
}

# -- Basic auth -----------------------------------------------------------------------------
# The KeyValueStore holds one key, "auth": the hex SHA-256 of the whole Authorization header
# ("Basic " + base64(user:password)). make ui-password sets it, so the password never reaches
# git, Terraform state or the function code. Until it is set, every request gets a 401.

resource "aws_cloudfront_key_value_store" "auth" {
  name    = "${local.name_prefix}-auth"
  comment = "Upload UI ${var.env}: basic-auth hash (key auth), set by make ui-password"
}

resource "aws_cloudfront_function" "basic_auth" {
  name                         = "${local.name_prefix}-basic-auth"
  runtime                      = "cloudfront-js-2.0"
  comment                      = "Upload UI ${var.env}: HTTP basic auth against the KeyValueStore"
  publish                      = true
  code                         = file("${path.module}/functions/basic_auth.js")
  key_value_store_associations = [aws_cloudfront_key_value_store.auth.arn]
}

# -- Origins' access ------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${local.name_prefix}-site"
  description                       = "Upload UI ${var.env}: CloudFront reads the site bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# "always" replaces the viewer's Authorization (basic auth) with CloudFront's SigV4 signature.
# POSTs must carry x-amz-content-sha256 (the web client sends it).
resource "aws_cloudfront_origin_access_control" "bff" {
  name                              = "${local.name_prefix}-bff"
  description                       = "Upload UI ${var.env}: CloudFront invokes the BFF function URL"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# -- Response headers -----------------------------------------------------------------------

resource "aws_cloudfront_response_headers_policy" "security" {
  name    = "${local.name_prefix}-security-headers"
  comment = "Upload UI ${var.env}: CSP, HSTS, nosniff, no framing, no referrer"

  security_headers_config {
    content_security_policy {
      content_security_policy = local.content_security_policy
      override                = true
    }

    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
  }
}

# -- Distribution ---------------------------------------------------------------------------

resource "aws_cloudfront_distribution" "this" {
  #checkov:skip=CKV_AWS_68:No WAF in dev (free tier): basic auth guards every request. A WAF web ACL is a prod prerequisite (docs/UI.md).
  #checkov:skip=CKV2_AWS_47:No WAF in dev (free tier), so no Log4j rule set; the origins are a static site and a Python function. Prod prerequisite.
  #checkov:skip=CKV_AWS_86:No access logs in dev (they need a logging bucket with ACLs); the BFF logs every API call. Prod prerequisite.
  #checkov:skip=CKV_AWS_310:One site bucket and one function; a failover origin adds cost for an internal tool.
  #checkov:skip=CKV_AWS_174:The default *.cloudfront.net certificate can't pin a minimum TLS version; a custom domain with ACM is a prod prerequisite.
  #checkov:skip=CKV_AWS_374:Users may be anywhere; access is limited by the login, not by country.
  #checkov:skip=CKV2_AWS_42:The default *.cloudfront.net certificate; a custom domain with ACM is a prod prerequisite.
  enabled             = true
  comment             = "Upload UI (${var.env})"
  default_root_object = "index.html"
  price_class         = var.price_class
  http_version        = "http2and3"
  is_ipv6_enabled     = true

  origin {
    origin_id                = "site"
    domain_name              = module.site_bucket.regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  origin {
    origin_id                = "bff"
    domain_name              = local.bff_origin_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.bff.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 30
    }
  }

  # The SPA: cached; index.html is uploaded with no-cache, hashed assets as immutable.
  default_cache_behavior {
    target_origin_id           = "site"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.managed_policy_ids.caching_optimized
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.basic_auth.arn
    }
  }

  # The API: never cached; every viewer header except Host goes to the function URL.
  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "bff"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.managed_policy_ids.caching_disabled
    origin_request_policy_id   = local.managed_policy_ids.all_viewer_except_host_header
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.basic_auth.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
