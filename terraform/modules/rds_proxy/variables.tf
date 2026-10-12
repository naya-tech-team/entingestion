variable "name" {
  description = "Proxy name."
  type        = string
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets (at least two AZs)."
  type        = list(string)
}

variable "cluster_id" {
  description = "Aurora cluster identifier the proxy fronts."
  type        = string
}

variable "cluster_resource_id" {
  description = "Aurora cluster resource id (cluster-XXXX); the proxy connects to it with IAM auth."
  type        = string
}

variable "db_security_group_id" {
  description = "The cluster's security group; the proxy is allowed to reach it on 5432."
  type        = string
}

variable "allowed_security_group_ids" {
  description = "Client security groups allowed on 5432, keyed by a stable label."
  type        = map(string)
  default     = {}
}

variable "idle_client_timeout" {
  description = "Seconds before an idle client connection is closed."
  type        = number
  default     = 1800
}

variable "max_connections_percent" {
  description = "Share of the database's max_connections the proxy may use."
  type        = number
  default     = 75
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates. null = no boundary."
  type        = string
  default     = null
}
