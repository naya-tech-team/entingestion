locals {
  # SSE-SQS unless a customer managed key is given. With a key, senders and consumers need
  # kms:GenerateDataKey / kms:Decrypt on it, and AWS services that send (S3, SNS, EventBridge)
  # need key policy grants.
  sse_sqs = var.kms_key_arn == null
}

resource "aws_sqs_queue" "dlq" {
  name                              = "${var.name}-dlq"
  message_retention_seconds         = var.dlq_message_retention_seconds
  sqs_managed_sse_enabled           = local.sse_sqs ? true : null
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = local.sse_sqs ? null : var.kms_data_key_reuse_period_seconds
}

resource "aws_sqs_queue" "this" {
  name                              = var.name
  visibility_timeout_seconds        = var.visibility_timeout_seconds
  message_retention_seconds         = var.message_retention_seconds
  sqs_managed_sse_enabled           = local.sse_sqs ? true : null
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = local.sse_sqs ? null : var.kms_data_key_reuse_period_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

# Only this queue may use the DLQ as its dead-letter target.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this.arn]
  })
}

# Both queues refuse plain HTTP. The caller's policy_documents are merged into the main queue's.
data "aws_iam_policy_document" "queue" {
  source_policy_documents = var.policy_documents

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.this.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "this" {
  queue_url = aws_sqs_queue.this.id
  policy    = data.aws_iam_policy_document.queue.json
}

# The policy used to exist only with policy_documents.
moved {
  from = aws_sqs_queue_policy.this[0]
  to   = aws_sqs_queue_policy.this
}

data "aws_iam_policy_document" "dlq" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.dlq.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = data.aws_iam_policy_document.dlq.json
}
