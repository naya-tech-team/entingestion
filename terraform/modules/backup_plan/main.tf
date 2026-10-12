data "aws_partition" "current" {}

# Recovery points in this region, encrypted with the stack's key.
resource "aws_backup_vault" "this" {
  name        = var.name
  kms_key_arn = var.kms_key_arn
}

# ... and a copy of each in another region, under a key of that region.
module "copy_key" {
  source    = "../kms_key"
  providers = { aws = aws.copy }

  alias       = "${var.name}-copy"
  description = "Backup copies of ${var.name} (disaster recovery)."
}

resource "aws_backup_vault" "copy" {
  provider = aws.copy

  name        = "${var.name}-copy"
  kms_key_arn = module.copy_key.key_arn
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = var.name
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "this" {
  for_each = toset(["AWSBackupServiceRolePolicyForBackup", "AWSBackupServiceRolePolicyForRestores"])

  role       = aws_iam_role.this.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/${each.value}"
}

resource "aws_backup_plan" "this" {
  name = var.name

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.this.name
    schedule          = var.schedule
    start_window      = 60
    completion_window = 360

    lifecycle {
      delete_after = var.retention_days
    }

    copy_action {
      destination_vault_arn = aws_backup_vault.copy.arn

      lifecycle {
        delete_after = var.retention_days
      }
    }
  }
}

resource "aws_backup_selection" "this" {
  name         = var.name
  plan_id      = aws_backup_plan.this.id
  iam_role_arn = aws_iam_role.this.arn
  resources    = var.resource_arns

  depends_on = [aws_iam_role_policy_attachment.this]
}
