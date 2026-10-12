# Embedding profiles (variable embedding_profiles): what the functions get, and what they may call.
# docs/ARCHITECTURE.md "Embedding models".

locals {
  # As the functions read them (EMBEDDING_PROFILES, ingest.embedding.profiles): unset options
  # left out. Built with merge() so numbers and booleans stay JSON numbers and booleans.
  embedding_profiles_env = {
    for name, p in var.embedding_profiles : name => merge(
      {
        provider         = p.provider
        model            = p.model
        dimension        = p.dimension
        normalize        = p.normalize
        batch_size       = p.batch_size
        max_input_tokens = p.max_input_tokens
      },
      p.base_url == null ? {} : { base_url = p.base_url },
      p.api_key_secret == null ? {} : { api_key_secret = p.api_key_secret },
      p.document_input_type == null ? {} : { document_input_type = p.document_input_type },
      p.query_input_type == null ? {} : { query_input_type = p.query_input_type },
      p.send_dimensions ? { send_dimensions = true } : {},
    )
  }

  embedding_env = {
    EMBEDDING_PROFILES = jsonencode(local.embedding_profiles_env)
    DEFAULT_PROFILE    = var.default_profile
    TENANT_PROFILES    = jsonencode(var.tenant_profiles)
  }

  # bedrock:InvokeModel on each Bedrock profile's foundation model (Embed, the control API's
  # query embedding, evaluation).
  bedrock_embedding_models = distinct([
    for p in values(var.embedding_profiles) : p.model if p.provider == "bedrock"
  ])
  bedrock_embedding_model_arns = [
    for m in local.bedrock_embedding_models : "arn:${local.partition}:bedrock:${local.region}::foundation-model/${m}"
  ]

  # secretsmanager:GetSecretValue on each OpenAI-compatible profile's API key (the ARN ends in
  # -<6 random characters>).
  embedding_api_key_secret_arns = [
    for s in distinct(compact([for p in values(var.embedding_profiles) : p.provider == "openai_compatible" ? p.api_key_secret : null])) :
    "arn:${local.partition}:secretsmanager:${local.region}:${local.account_id}:secret:${s}-??????"
  ]

  # Throttling alarm and dashboard: the default profile's Bedrock model (or the first one).
  default_embedding_profile = var.embedding_profiles[var.default_profile]
  alarm_embedding_model = (
    local.default_embedding_profile.provider == "bedrock"
    ? local.default_embedding_profile.model
    : try(local.bedrock_embedding_models[0], null)
  )
}
