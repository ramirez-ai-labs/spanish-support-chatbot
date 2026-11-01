# Same state bucket as the other stacks, separate prefix. tf-plan can't read this prefix: Cloudflare's
# provider stores the Turnstile secret in state (bootstrap stack, `plan_state` condition).
terraform {
  backend "gcs" {
    prefix = "shared"
  }
}
