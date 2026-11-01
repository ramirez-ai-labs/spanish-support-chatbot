output "ui_url" {
  description = "Where the chat UI is served."
  value       = "https://${cloudflare_pages_project.ui.subdomain}"
}

output "turnstile_site_key" {
  description = "Public site key the chat page embeds. The secret key is never output."
  value       = cloudflare_turnstile_widget.chat.sitekey
}
