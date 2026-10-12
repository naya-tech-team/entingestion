variable "name" {
  description = "Table name."
  type        = string
}

variable "hash_key" {
  description = "Partition key attribute name."
  type        = string
}

variable "range_key" {
  description = "Sort key attribute name, if any."
  type        = string
  default     = null
}

variable "attributes" {
  description = "Attribute definitions for every table and index key. type is S, N or B."
  type = list(object({
    name = string
    type = string
  }))

  validation {
    condition     = alltrue([for a in var.attributes : contains(["S", "N", "B"], a.type)])
    error_message = "Every attribute type must be S, N or B."
  }
}

variable "global_secondary_indexes" {
  description = "Global secondary indexes. projection_type is ALL, KEYS_ONLY or INCLUDE."
  type = list(object({
    name               = string
    hash_key           = string
    range_key          = optional(string)
    projection_type    = optional(string, "ALL")
    non_key_attributes = optional(list(string))
  }))
  default = []

  validation {
    condition = alltrue([
      for g in var.global_secondary_indexes : contains(["ALL", "KEYS_ONLY", "INCLUDE"], g.projection_type)
    ])
    error_message = "projection_type must be ALL, KEYS_ONLY or INCLUDE."
  }

  validation {
    condition = alltrue([
      for g in var.global_secondary_indexes :
      (g.projection_type == "INCLUDE") == (g.non_key_attributes != null)
    ])
    error_message = "non_key_attributes must be set when, and only when, projection_type is INCLUDE."
  }
}

variable "stream_view_type" {
  description = "Enables the table stream with this view type when set (NEW_IMAGE, OLD_IMAGE, NEW_AND_OLD_IMAGES, KEYS_ONLY)."
  type        = string
  default     = null

  validation {
    condition     = var.stream_view_type == null || contains(["NEW_IMAGE", "OLD_IMAGE", "NEW_AND_OLD_IMAGES", "KEYS_ONLY"], coalesce(var.stream_view_type, "x"))
    error_message = "stream_view_type must be NEW_IMAGE, OLD_IMAGE, NEW_AND_OLD_IMAGES or KEYS_ONLY."
  }
}

variable "ttl_attribute" {
  description = "Enables TTL on this attribute (epoch seconds) when set."
  type        = string
  default     = null
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key for server-side encryption."
  type        = string
}

variable "deletion_protection" {
  description = "Enable DynamoDB deletion protection. Set true for any environment that holds real data."
  type        = bool
  default     = true
}
