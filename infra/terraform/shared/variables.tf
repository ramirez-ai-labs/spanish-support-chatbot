variable "cloudflare_account_id" {
  description = "Cloudflare account that hosts the chat UI. Set via TF_VAR_cloudflare_account_id from a prod environment secret."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be the 32-character account ID (npx wrangler whoami)."
  }
}

variable "name" {
  description = "Pages project name; the UI is served at <name>.pages.dev."
  type        = string
  default     = "spanish-support-chatbot"
}
