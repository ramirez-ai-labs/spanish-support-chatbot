# Cross-cloud pieces every cloud's build shares: the chat UI and its bot check. Planned and applied
# only in the approved `prod` job, because the Cloudflare token is a `prod` environment secret.
# Neon is not here (ADR 0006).

# The chat UI (#22) is uploaded by CI as static files (direct upload), so no Git connection.
# Always free: unlimited sites, requests and bandwidth.
resource "cloudflare_pages_project" "ui" {
  account_id        = var.cloudflare_account_id
  name              = var.name
  production_branch = "main"
}

# Bot check in front of the chat API. The site key is public (it goes in the page). The secret key
# is stored in this stack's state by the provider, which is why tf-plan can't read this state; it is
# copied into Secret Manager by hand when the API starts checking tokens (#22). Always free.
resource "cloudflare_turnstile_widget" "chat" {
  account_id = var.cloudflare_account_id
  name       = "${var.name} chat"
  domains    = [cloudflare_pages_project.ui.subdomain]
  mode       = "managed"
}
