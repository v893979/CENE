variable "project_id" {
  type        = string
  description = "Google Cloud project to deploy into (use a dedicated project you can delete)."
}

variable "region" {
  type    = string
  default = "australia-southeast2" # Melbourne
}

variable "bq_location" {
  type    = string
  default = "australia-southeast2"
}

variable "dataset" {
  type    = string
  default = "cene"
}

variable "image_tag" {
  type    = string
  default = "v1"
}

variable "kms_protection_level" {
  type        = string
  default     = "SOFTWARE"
  description = "SOFTWARE for development; HSM to keep the signing keys in Cloud HSM."
  validation {
    condition     = contains(["SOFTWARE", "HSM"], var.kms_protection_level)
    error_message = "Use SOFTWARE or HSM."
  }
}

variable "enable_redis" {
  type        = bool
  default     = false
  description = "Shared state for replay ids, taint and temporal windows. Without it, services run single-instance."
}

variable "audit_retention_days" {
  type    = number
  default = 30
}

variable "lock_audit_retention" {
  type        = bool
  default     = false
  description = "Locks the bucket retention policy. IRREVERSIBLE - leave false while testing."
}

variable "oracle_signers" {
  type        = list(string)
  default     = []
  description = "Principals allowed to sign emergency-mode statements, e.g. [\"user:you@example.com\"]."
}

variable "operators" {
  type        = list(string)
  default     = []
  description = "Principals allowed to call the control plane and gateway (and approve escrow), e.g. [\"user:you@example.com\"]."
}

variable "model_armor_template" {
  type        = string
  default     = ""
  description = "Optional Model Armor template (projects/P/locations/L/templates/T). Empty = screening off. Create it with the prompt-injection/jailbreak and malicious-URL filters enabled."
}

variable "test_impersonators" {
  type        = list(string)
  default     = []
  description = "Testers allowed to impersonate cene-agent and cene-approver (roles/iam.serviceAccountTokenCreator on those two only), e.g. [\"user:you@example.com\"]. Set back to [] when testing is finished."
}
