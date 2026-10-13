# Values every stack derives the same way from its env, account and tags. No providers or data
# sources, so a stack can use the outputs in its provider blocks (default_tags).

locals {
  name_prefix          = "${var.stack}-${var.env}"
  iam_arn_prefix       = "arn:${var.partition}:iam::${var.aws_account_id}:"
  boundary_policy_name = "entingestion-${var.env}-workload-boundary"

  # The organization's mandatory tags. A resource that sets its own Name keeps it; env also
  # scopes the deploy roles' permissions boundary (terraform/bootstrap/access.tf).
  mandatory_tags = {
    "Name"                 = local.name_prefix
    "Created By"           = var.mandatory_tags.created_by
    "Created Date"         = var.mandatory_tags.created_date
    "Owner"                = var.mandatory_tags.owner
    "Internal Client Code" = var.mandatory_tags.internal_client_code
    "Client"               = var.mandatory_tags.client
    "Project Name"         = var.mandatory_tags.project_name
    "Division"             = var.mandatory_tags.division
  }
}
