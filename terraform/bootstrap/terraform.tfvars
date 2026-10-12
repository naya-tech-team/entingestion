region         = "us-west-2"
aws_account_id = "106751264162"

mandatory_tags = {
  created_by           = "naveen"
  created_date         = "2026-10-10"
  owner                = "naveen"
  internal_client_code = "TBD" # placeholder: replace with the real value
  client               = "TBD" # placeholder: replace with the real value
  project_name         = "entingestion"
  division             = "TBD" # placeholder: replace with the real value
}

# People who may assume the deploy roles (and use the state bucket as break-glass).
deploy_principal_arns = ["arn:aws:iam::106751264162:user/bedrock-role"]
