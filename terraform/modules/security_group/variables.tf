variable "name" {
  description = "Security group name."
  type        = string
}

variable "description" {
  description = "What the group is for."
  type        = string
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "ingress" {
  description = "Ingress rules, keyed by a stable label. Set exactly one source per rule."
  type = map(object({
    description                  = string
    from_port                    = number
    to_port                      = number
    ip_protocol                  = optional(string, "tcp")
    cidr_ipv4                    = optional(string)
    referenced_security_group_id = optional(string)
    prefix_list_id               = optional(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for r in values(var.ingress) : length(compact([r.cidr_ipv4, r.referenced_security_group_id, r.prefix_list_id])) == 1
    ])
    error_message = "Each ingress rule needs exactly one of cidr_ipv4, referenced_security_group_id or prefix_list_id."
  }
}

variable "egress" {
  description = "Egress rules, keyed by a stable label. Set exactly one destination per rule. Empty = no egress."
  type = map(object({
    description                  = string
    from_port                    = number
    to_port                      = number
    ip_protocol                  = optional(string, "tcp")
    cidr_ipv4                    = optional(string)
    referenced_security_group_id = optional(string)
    prefix_list_id               = optional(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for r in values(var.egress) : length(compact([r.cidr_ipv4, r.referenced_security_group_id, r.prefix_list_id])) == 1
    ])
    error_message = "Each egress rule needs exactly one of cidr_ipv4, referenced_security_group_id or prefix_list_id."
  }
}
