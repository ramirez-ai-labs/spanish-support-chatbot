terraform {
  # Same version as .terraform-version; CI and every laptop must match.
  required_version = "~> 1.17.1"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.28"
    }
  }
}

# Reads CLOUDFLARE_API_TOKEN from the environment: an account token with Cloudflare Pages: Edit and
# Turnstile: Edit only, stored as a `prod` environment secret (infra/BOOTSTRAP.md, section 4).
provider "cloudflare" {}
