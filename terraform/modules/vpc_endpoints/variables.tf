variable "name" {
  description = "Name prefix for the endpoints' security group."
  type        = string
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets for interface endpoints (one ENI per subnet; fewer subnets = lower cost)."
  type        = list(string)
  default     = []
}

variable "route_table_ids" {
  description = "Route tables for gateway endpoints."
  type        = list(string)
  default     = []
}

variable "gateway_services" {
  description = "Gateway endpoints (free): s3, dynamodb."
  type        = list(string)
  default     = ["s3", "dynamodb"]

  validation {
    condition     = alltrue([for s in var.gateway_services : contains(["s3", "dynamodb"], s)])
    error_message = "Gateway endpoints exist only for s3 and dynamodb."
  }
}

variable "interface_services" {
  description = "Interface endpoints by service suffix, e.g. secretsmanager, sqs, states, logs, ecr.api, ecr.dkr, bedrock-runtime. Billed per AZ-hour."
  type        = list(string)
  default     = []
}

variable "allowed_security_group_ids" {
  description = "Security groups allowed to reach the interface endpoints on 443, keyed by a stable label (ids may be unknown at plan time)."
  type        = map(string)
  default     = {}
}

variable "policies" {
  description = "Optional endpoint policies (JSON) by service name; services not listed get the AWS default (full access)."
  type        = map(string)
  default     = {}
}
