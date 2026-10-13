variable "boundaries" {
  description = "The permissions boundary of every IAM role the stack creates (null: none). Each role-creating module reports its own as role_permissions_boundaries."
  type        = list(string)
}

variable "expected" {
  description = "The boundary every role must carry (stack_common's permissions_boundary), or null when roles carry none."
  type        = string
  default     = null
}
