# Bootstrap — one-time manual setup

Everything else is Terraform applied by CI. This file records the steps that can't be, so the
setup is reproducible and auditable. Check items off as they're done; add the date and any IDs
that aren't secret.

## Why this exists

Terraform needs somewhere to keep state and a way for CI to authenticate before it can manage
anything. That chicken-and-egg step runs **locally, once**. After it, CI authenticates to GCP,
Azure and AWS with short-lived OIDC tokens — no long-lived cloud keys in GitHub.

## 1. Local tooling

The bootstrap runs from Windows or macOS. Commands are identical in both unless a block is
labeled per shell.

| Tool | Required | Purpose | Windows (winget) | macOS (Homebrew) |
| --- | --- | --- | --- | --- |
| git | Yes | Source control | `winget install Git.Git` | `brew install git` |
| terraform | Yes | Bootstrap apply; local `plan` / `fmt` / `validate` | `winget install Hashicorp.Terraform --version <pin>` | `brew install hashicorp/tap/terraform` |
| gcloud | Yes | Bootstrap: GCS state bucket, Workload Identity Federation | `winget install Google.CloudSDK` | `brew install --cask gcloud-cli` |
| az | Yes | Bootstrap: Entra app + federated credentials | `winget install Microsoft.AzureCLI` | `brew install azure-cli` |
| aws | Yes | Bootstrap: IAM OIDC provider + deploy role | `winget install Amazon.AWSCLI` | `brew install awscli` |
| gh | Yes | Repo settings, branch protection, Actions secrets, milestones | `winget install GitHub.cli` | `brew install gh` |
| docker | Yes | Local `docker-compose` dev, image builds | `winget install Docker.DockerDesktop` (WSL2 backend) | `brew install --cask docker` |
| python + uv | Yes | App, tests, tooling | `winget install Python.Python.3.12` + `winget install astral-sh.uv` | `brew install python@3.12 uv` |
| node | Optional | Needed only for the npm tools below | `winget install OpenJS.NodeJS.LTS` | `brew install node` |
| neonctl | Optional | Branches and connection strings while debugging (Terraform uses the API key) | `npm i -g neonctl` | `npm i -g neonctl` |
| wrangler | Optional | Local Pages testing (Terraform uses the API token) | `npm i -g wrangler` | `npm i -g wrangler` |

**Terraform version.** [`.terraform-version`](../.terraform-version) is the single pin; the bootstrap
stack's `required_version` and the CI `setup-terraform` step must use the same value. Install exactly that version on every machine.
Don't use `brew install terraform`: Homebrew core froze at 1.5.7 after the license change, so it
silently drifts from CI. Use the `hashicorp/tap` formula above.

**Windows path length.** Terraform's provider cache nests deeply and can exceed the 260-character
path limit. Clone to a short path (e.g., `C:\src\spanish-support-chatbot`) and enable long paths:

```powershell
git config --global core.longpaths true
```

**Line endings.** [`.gitattributes`](../.gitattributes) forces LF, so leave `core.autocrlf` unset
(or `input`). If a Windows checkout predates `.gitattributes`, run `git add --renormalize .` once.

- [ ] Terraform installed at the version in `.terraform-version` (`terraform version` matches)
- [ ] AWS CLI upgraded to current v2 (`winget upgrade Amazon.AWSCLI` / `brew upgrade awscli`)
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

Use the least privilege that still lets the bootstrap stack run. The cloud CLIs store their own
credentials (gcloud ADC, AWS profiles, `az login`, `gh auth`), so none of them go in `.env`,
`.tfvars` or any committed file.

```bash
# GCP: the second command gives Terraform Application Default Credentials
gcloud auth login
gcloud auth application-default login
gcloud config set project chatbot-demo

# AWS: prefer SSO; otherwise a named profile for the daily IAM user
aws configure sso --profile showcase   # or: aws configure --profile showcase
aws sso login --profile showcase       # SSO only; repeat when the session expires

# Azure
az login
az account set --subscription <subscription-id>

# GitHub: Terraform's GitHub provider reuses the gh token
gh auth login
```

Then point the tools at the profile and token for the current shell:

```powershell
# Windows (PowerShell)
$env:AWS_PROFILE  = "showcase"
$env:GITHUB_TOKEN = (gh auth token)
```

```bash
# macOS (zsh/bash)
export AWS_PROFILE=showcase
export GITHUB_TOKEN="$(gh auth token)"
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

For local runs, copy [`.env.example`](../.env.example) to `.env` (gitignored), fill it in, and load
it into the current shell:

```powershell
# Windows (PowerShell)
Get-Content .env | Where-Object { $_ -match '^\s*([^#][^=]*)=(.*)$' } |
  ForEach-Object { Set-Item "env:$($Matches[1].Trim())" $Matches[2] }
```

```bash
# macOS (zsh/bash)
set -a; source .env; set +a
```

Store the same values for CI (prompts for the secret, so it never lands in shell history):

```bash
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

```bash
cd infra/terraform/bootstrap
terraform init

# Record provider hashes for every OS that runs Terraform, so init works on Windows, macOS
# and CI from the same committed lock file. Add *_arm64 if any machine is ARM.
terraform providers lock -platform=windows_amd64 -platform=darwin_amd64 -platform=linux_amd64

terraform plan -out bootstrap.tfplan
terraform apply bootstrap.tfplan
```

Commit `.terraform.lock.hcl`. Rerun the `providers lock` command whenever a provider version
changes. Then run `terraform init` and `terraform plan` from the other OS to confirm it shows
no changes.

Record the non-secret outputs (WIF provider name, Azure client/tenant IDs, AWS role ARN) as
GitHub Actions variables so the CI workflows can use them.

- [ ] Bootstrap applied
- [ ] `.terraform.lock.hcl` committed with Windows, macOS and Linux hashes
- [ ] Clean `plan` from the second OS
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
