mock_provider "aws" {
  override_during = plan

  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-west-2:123456789012:q" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

variables {
  name                       = "q"
  visibility_timeout_seconds = 1800
}

run "dlq_redrive_and_encryption" {
  command = plan

  assert {
    condition     = aws_sqs_queue.dlq.name == "q-dlq" && aws_sqs_queue.dlq.message_retention_seconds == 1209600
    error_message = "DLQ must be <name>-dlq with 14-day retention."
  }

  assert {
    condition     = jsondecode(aws_sqs_queue.this.redrive_policy).maxReceiveCount == 5
    error_message = "Messages must move to the DLQ after 5 receives by default."
  }

  assert {
    condition     = aws_sqs_queue.this.sqs_managed_sse_enabled && aws_sqs_queue.dlq.sqs_managed_sse_enabled
    error_message = "Both queues must be encrypted."
  }

  assert {
    condition     = aws_sqs_queue.this.visibility_timeout_seconds == 1800
    error_message = "Visibility timeout must be passed through."
  }

  assert {
    condition = alltrue([
      for doc in [data.aws_iam_policy_document.queue, data.aws_iam_policy_document.dlq] :
      anytrue([
        for s in doc.statement :
        s.sid == "DenyInsecureTransport" && s.effect == "Deny"
        && anytrue([for c in s.condition : c.variable == "aws:SecureTransport" && c.values == tolist(["false"])])
      ])
    ])
    error_message = "Both queues must refuse requests without TLS, even without policy_documents."
  }

  assert {
    condition     = aws_sqs_queue.this.kms_master_key_id == null && aws_sqs_queue.this.sqs_managed_sse_enabled
    error_message = "SSE-SQS (no customer managed key) by default."
  }
}

run "queue_policy_merges_policy_documents" {
  command = plan

  variables {
    policy_documents = ["{\"Version\":\"2012-10-17\",\"Statement\":[]}"]
  }

  assert {
    condition     = data.aws_iam_policy_document.queue.source_policy_documents == tolist(var.policy_documents)
    error_message = "policy_documents must be merged into the queue policy."
  }
}

run "customer_managed_key" {
  command = plan

  variables {
    kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
  }

  assert {
    condition = (
      aws_sqs_queue.this.kms_master_key_id == var.kms_key_arn
      && aws_sqs_queue.dlq.kms_master_key_id == var.kms_key_arn
      && aws_sqs_queue.this.kms_data_key_reuse_period_seconds == 300
      && aws_sqs_queue.this.sqs_managed_sse_enabled != true
      && output.settings.kms_key_arn == var.kms_key_arn
    )
    error_message = "kms_key_arn must encrypt both queues with that key instead of SSE-SQS."
  }
}

run "rejects_zero_receives" {
  command = plan

  variables {
    max_receive_count = 0
  }

  expect_failures = [var.max_receive_count]
}
