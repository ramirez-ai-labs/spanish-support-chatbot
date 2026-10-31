# Bootstrap — one-time manual setup

Everything else is Terraform applied by CI. This file records the steps that can't be, so the
setup is reproducible and auditable. Check items off as they're done; add the date and any IDs
that aren't secret.

## Why this exists

Terraform needs somewhere to keep state and a way for CI to authenticate before it can manage
anything. That chicken-and-egg step runs **locally, once**. After it, CI authenticates to GCP,
Azure and AWS with short-lived OIDC tokens — no long-lived cloud keys in GitHub.

## 1. Local tooling

| Tool | Required | Purpose | Install (Windows) |
| --- | --- | --- | --- |
| terraform | Yes | Bootstrap apply; local `plan` / `fmt` / `validate` | `winget install Hashicorp.Terraform` |
| gcloud | Yes | Bootstrap: GCS state bucket, Workload Identity Federation | `winget install Google.CloudSDK` |
| az | Yes | Bootstrap: Entra app + federated credentials | `winget install Microsoft.AzureCLI` |
| aws | Yes | Bootstrap: IAM OIDC provider + deploy role | `winget install Amazon.AWSCLI` |
| gh | Yes | Repo settings, branch protection, Actions secrets, milestones | `winget install GitHub.cli` |
| docker | Yes | Local `docker-compose` dev, image builds | Docker Desktop |
| neonctl | Optional | Branches and connection strings while debugging (Terraform uses the API key) | `npm i -g neonctl` |
| wrangler | Optional | Local Pages testing (Terraform uses the API token) | `npm i -g wrangler` |

Pin the Terraform version (`required_version` in the bootstrap stack) so local and CI match.

- [ ] Terraform installed and version pinned
- [ ] AWS CLI upgraded to current v2 (`winget upgrade Amazon.AWSCLI`)
- [ ] Azure CLI upgraded (`az upgrade`)

## 2. Accounts and safety rails

- [ ] GCP project `chatbot-demo` (billing on, for Cloud Run and friends)
- [ ] GCP project `chatbot-gemini` (no billing — keeps the Gemini key on the free tier)
- [ ] Gemini API key created in AI Studio under `chatbot-gemini`
- [ ] $1 budget alerts on GCP, Azure and AWS
- [ ] MFA on every account (GCP, Azure, AWS, GitHub, Neon, Cloudflare)
- [ ] Tag/label convention `project=showcase` documented and applied
- [ ] AWS: IAM user for daily work; root used only for account-level tasks
- [ ] AWS: $100 credit activities completed; any EC2 instance or database created for them deleted immediately

## 3. Authenticate the CLIs

Use the least privilege that still lets the bootstrap stack run. Never put credentials in
`.tfvars` or committed files — use a gitignored `.env` (loaded by direnv or your shell profile).

```powershell
# GCP — the second command gives Terraform Application Default Credentials
gcloud auth login
gcloud auth application-default login
gcloud config set project chatbot-demo

# AWS — prefer SSO; otherwise a named profile for the daily IAM user
aws configure sso            # or: aws configure --profile showcase
$env:AWS_PROFILE = "showcase"

# Azure
az login
az account set --subscription <subscription-id>

# GitHub — Terraform's GitHub provider can reuse the gh token
gh auth login
$env:GITHUB_TOKEN = (gh auth token)
```

- [ ] gcloud user + ADC logged in
- [ ] AWS profile configured
- [ ] Azure subscription selected
- [ ] gh logged in with access to `ramirez-ai-labs`

## 4. Provider API tokens (Neon, Cloudflare)

These providers don't support OIDC, so they're the only long-lived credentials. Scope them tightly,
store them as environment variables locally and as GitHub Actions secrets for CI.

| Secret | Where to create | Scope |
| --- | --- | --- |
| `NEON_API_KEY` | Neon console → Account → API keys | This project only, if org-scoped keys are available |
| `CLOUDFLARE_API_TOKEN` | Cloudflare dashboard → API tokens | Account-scoped: Pages edit, Turnstile edit. Not the global API key |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard | Not secret; store as an Actions variable |

```powershell
gh secret set NEON_API_KEY
gh secret set CLOUDFLARE_API_TOKEN
gh variable set CLOUDFLARE_ACCOUNT_ID --body <account-id>
```

- [ ] Neon API key created and stored
- [ ] Cloudflare token created and stored

## 5. Run the bootstrap stack (locally, once)

`infra/terraform/bootstrap/` creates:

- A private, versioned GCS bucket for Terraform state
- GCP Workload Identity Federation pool + provider trusting this repo's GitHub Actions
- Azure Entra app registration + federated credentials for this repo
- AWS IAM OIDC provider for GitHub + a deploy role trusted only by this repo

Restrict every trust policy to `repo:ramirez-ai-labs/spanish-support-chatbot` (and to `main` /
the `prod` environment where applicable), so no other repo or fork can assume the roles.

```powershell
cd infra/terraform/bootstrap
terraform init
terraform plan -out bootstrap.tfplan
terraform apply bootstrap.tfplan
```

Record the non-secret outputs (WIF provider name, Azure client/tenant IDs, AWS role ARN) as
GitHub Actions variables so the CI workflows can use them.

- [ ] Bootstrap applied
- [ ] Outputs stored as Actions variables
- [ ] Bootstrap state migrated to the GCS bucket

## 6. Hand off to CI and lock down

- [ ] `shared`, `gcp`, `azure`, `aws` stacks plan on PR and apply on merge
- [ ] `prod` GitHub environment requires approval
- [ ] Weekly scheduled `terraform plan` drift check enabled
- [ ] Local admin sessions logged out or downgraded to read-only — consoles are for looking only

## Not managed by Terraform

| Item | Managed by |
| --- | --- |
| App version rollouts | CI deploy step |
| Database schema | `migrations/` |
| Test users | Seed script |
| Gemini API key value | Pasted once into each cloud's secret store (Secret Manager, Container Apps secrets, SSM) |
