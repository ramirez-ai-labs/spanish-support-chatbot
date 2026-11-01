# Bootstrap — one-time manual setup

Everything else is Terraform applied by CI. This file records the steps that can't be, so the
setup is reproducible and auditable. Check items off as they're done and add the date. This repo
is public: real account and project IDs go in your gitignored `.env`, never in this file.

## Why this exists

Terraform needs somewhere to keep state and a way for CI to authenticate before it can manage
anything. That chicken-and-egg step runs **locally**, once per cloud. After it, CI authenticates
with short-lived OIDC tokens — no long-lived cloud keys in GitHub.

## When each section runs

Infrastructure is added only when the next slice needs it (see [docs/roadmap.md](../docs/roadmap.md)).

| Milestone | Sections |
| --- | --- |
| M0 | 1 (tooling), 2 (accounts and safety rails), 3 (GCP + GitHub auth only) — enough for the local bot and CI |
| M1 | 3 (rest of GCP), 4 (Neon, Cloudflare), 5 (GCP bootstrap), 6 (`shared` + `gcp` stacks) |
| M7 | 3 (Azure), 5 (add Azure to bootstrap), 6 (`azure` stack) |
| M8 | 3 (AWS), 5 (add AWS to bootstrap), 6 (`aws` stack) |

Budget alerts on GCP happen on M0 day 1. MFA on GitHub, Google, Neon and Cloudflare is scheduled
for the start of M2 (#36), and Azure and AWS get MFA and budget alerts at the start of M7 and M8.
Until then, those accounts rely on strong, unique passwords.

## 1. Local tooling

The bootstrap runs from Windows or macOS. Commands are identical in both unless a block is
labeled per shell.

| Tool | Required | Purpose | Windows (winget) | macOS (Homebrew) |
| --- | --- | --- | --- | --- |
| git | Yes | Source control | `winget install Git.Git` | `brew install git` |
| terraform | Yes | Bootstrap apply; local `plan` / `fmt` / `validate` | `winget install Hashicorp.Terraform --version <pin>` | `brew install hashicorp/tap/terraform` |
| gcloud | Yes | Bootstrap: GCS state bucket, Workload Identity Federation | `winget install Google.CloudSDK` | `brew install --cask gcloud-cli` |
| az | Yes | Bootstrap: Entra app + federated credentials | `winget install Microsoft.AzureCLI` | `brew install azure-cli`, or `uv tool install azure-cli --python 3.12` |
| aws | Yes | Bootstrap: IAM OIDC provider + deploy role | `winget install Amazon.AWSCLI` | Official installer (below) |
| gh | Yes | Repo settings, branch protection, Actions secrets, milestones | `winget install GitHub.cli` | `brew install gh` |
| docker | Yes | Local `docker-compose` dev, image builds | `winget install Docker.DockerDesktop` (WSL2 backend) | `brew install --cask docker` |
| python + uv | Yes | App, tests, tooling | `winget install Python.Python.3.12` + `winget install astral-sh.uv` | `brew install python@3.12 uv` |
| node | Optional | Needed only for the npm tools below | `winget install OpenJS.NodeJS.LTS` | `brew install node` |
| neonctl | Optional | Branches and connection strings while debugging (Terraform uses the API key) | `npm i -g neonctl` | `npm i -g neonctl` |
| wrangler | Optional | Local Pages testing (Terraform uses the API token) | `npm i -g wrangler` | `npm i -g wrangler` |

**macOS: AWS CLI.** Use AWS's signed installer instead of the Homebrew formula, which compiles
cmake, OpenSSL and Python wherever Homebrew has no prebuilt bottles:

```bash
curl -o AWSCLIV2.pkg "https://awscli.amazonaws.com/AWSCLIV2.pkg"
sudo installer -pkg AWSCLIV2.pkg -target /
rm AWSCLIV2.pkg
```

**Older macOS (Homebrew Tier 3).** Homebrew builds every formula from source
there, which can take an hour or fail. Prefer vendor installers, casks and `uv tool install`.
Verified on a Tier 3 macOS: terraform from `hashicorp/tap`, the AWS `.pkg` and az via `uv tool install`
all install in seconds. If you use the python.org Python, run
`"/Applications/Python 3.12/Install Certificates.command"` once, or anything that downloads over
HTTPS with that Python (pip, installer scripts) fails with `CERTIFICATE_VERIFY_FAILED`.

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

- [x] Terraform installed at the version in `.terraform-version` (`terraform version` matches)
- [ ] AWS CLI upgraded to current v2 (`winget upgrade Amazon.AWSCLI` / rerun the `.pkg` on macOS)
- [ ] Azure CLI upgraded (`az upgrade`; `uv tool upgrade azure-cli` if installed with uv)

## 2. Accounts and safety rails

GCP project IDs are globally unique, so GCP usually appends a numeric suffix to the name. Use the
IDs, not the display names, in commands and Terraform. This repo is public: record the real IDs in
your gitignored `.env` (and later as GitHub Actions secrets), never in committed files.

| Display name | Project ID | Billing |
| --- | --- | --- |
| `chatbot-demo` | `<demo-project-id>` | Linked (Cloud Run and friends) |
| `chatbot-gemini` | `<gemini-project-id>` | **Not linked**: keeps the Gemini key on the free tier |

- [x] GCP project `chatbot-demo` created; ID saved as `GCP_PROJECT_ID` in `.env`
- [x] GCP project `chatbot-gemini` created; ID saved as `GCP_GEMINI_PROJECT_ID` in `.env`
- [x] Billing linked to `chatbot-demo` only (Billing → Account management → linked projects)
- [x] Gemini API key created in AI Studio under `chatbot-gemini`; stored in `.env` only
- [ ] $1 budget alerts on GCP, Azure and AWS. On GCP, one alert for the whole billing account
      (it may also pay for other projects) plus one scoped to `chatbot-demo`. GCP done, with
      $1/month spend caps on Cloud Run and the Gemini API in `chatbot-demo`; Azure and AWS pending
- [ ] MFA on every account (GCP, Azure, AWS, GitHub, Neon, Cloudflare)
- [x] Tag/label convention `project=showcase` documented and applied
- [ ] AWS: IAM user for daily work with MFA, used through the `showcase` CLI profile; root used only for account-level tasks
- [ ] AWS: $100 credit activities completed; any EC2 instance or database created for them deleted immediately

## 3. Authenticate the CLIs

Use the least privilege that still lets the bootstrap stack run. The cloud CLIs store their own
credentials (gcloud ADC, AWS profiles, `az login`, `gh auth`), so none of them go in `.env`,
`.tfvars` or any committed file.

**GCP: use a dedicated gcloud configuration.** If you have other GCP projects, your default
configuration and ADC quota project probably point at one of them, so commands and Terraform runs
for this repo would silently hit the wrong project. A named configuration keeps them apart:

```bash
gcloud config configurations create ssc
gcloud config set account <your-google-account>
gcloud config set project <demo-project-id>
gcloud auth login
gcloud auth application-default login   # gives Terraform Application Default Credentials
gcloud auth application-default set-quota-project <demo-project-id>
```

Switch with `gcloud config configurations activate ssc` (or `default` for other work). ADC is
shared across configurations, so rerun the `set-quota-project` line after working on another
project.

```bash
# AWS: named profile for the daily IAM user (see section 2). Not needed until M8.
aws configure --profile showcase

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

- [x] gcloud user + ADC logged in
- [ ] AWS profile configured
- [ ] Azure subscription selected
- [x] gh logged in with access to `ramirez-ai-labs`

## 4. Provider API tokens (Neon, Cloudflare)

These providers don't support OIDC, so they're the only long-lived credentials. Scope them tightly,
store them as environment variables locally and as GitHub Actions secrets for CI.

| Secret | Where to create | Scope |
| --- | --- | --- |
| `NEON_API_KEY` | Neon console → Account → API keys | This project only, if org-scoped keys are available |
| `CLOUDFLARE_API_TOKEN` | Cloudflare dashboard → API tokens | Account-scoped: Pages edit, Turnstile edit. Not the global API key |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard | Not secret, but stored as an Actions secret so it is masked in public logs |

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
gh secret set CLOUDFLARE_ACCOUNT_ID
```

- [ ] Neon API key created and stored
- [ ] Cloudflare token created and stored

## 5. Run the bootstrap stack (locally, once per cloud)

[`infra/terraform/bootstrap/`](terraform/bootstrap/) grows one cloud at a time:

- **M1 (GCP):** a private, versioned state bucket (us-east1, inside Cloud Storage's always-free
  regions) and a Workload Identity Federation pool + provider for GitHub Actions. Two service
  accounts:
  - `tf-plan`: read-only, usable by any workflow run in this repo, so PR checks can plan. It can
    read state but never write it (plans run with `-lock=false`).
  - `tf-apply`: changes infrastructure; usable only by `push` and `workflow_dispatch` runs on
    `main` **in the `prod` GitHub environment**, which requires the owner's approval. Pull
    requests get `refs/pull/N/merge` and jobs without the environment get `none`, so neither can
    impersonate it. It holds service-specific roles (Cloud Run, Artifact Registry, Firestore,
    BigQuery, Service Usage, service accounts), not `editor`, and its IAM admin role is
    conditioned to grant only an allowlist of runtime roles: it can never grant itself owner or
    editor. For Secret Manager it has a custom role that manages secret containers and their
    access but **cannot read or add secret values**; secret access is granted per secret, never
    project-wide. (Any deployer can still reach app secrets indirectly by deploying code; the
    `prod` approval, code review and audit logs are the controls for that.)
  - Data Access audit logs on `sts` and `iam` (which covers the `iamcredentials` API) record every
    CI login.
- **M7:** Azure Entra app registration + federated credentials for this repo
- **M8:** AWS IAM OIDC provider for GitHub + a deploy role trusted only by this repo

Trust is pinned to the **numeric repository and owner IDs**, not the name, so a renamed,
transferred or re-created repo cannot authenticate. Each later addition is a local `plan` +
`apply` of the same stack; existing resources should show no changes.

### First run (GCP, M1)

```bash
cd infra/terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars        # gitignored; fill in the three values
gh api repos/ramirez-ai-labs/spanish-support-chatbot --jq '.id, .owner.id'

# The state bucket doesn't exist yet: use local state for this one run.
printf 'terraform {\n  backend "local" {}\n}\n' > backend_override.tf   # gitignored

terraform init
terraform plan -out bootstrap.tfplan
terraform apply bootstrap.tfplan
```

### Move the state into the bucket

```bash
export TF_STATE_BUCKET="$(terraform output -raw state_bucket)"   # read while state is still local
rm backend_override.tf
terraform init -migrate-state -backend-config="bucket=$TF_STATE_BUCKET"   # answer "yes"
terraform plan                                                     # expect: No changes
rm -f terraform.tfstate terraform.tfstate.backup bootstrap.tfplan  # plan files can hold secrets
```

From then on, every `init` passes `-backend-config="bucket=$TF_STATE_BUCKET"` (add
`TF_STATE_BUCKET` to your `.env`).

### Hand the outputs to CI

They grant nothing on their own, but this repo's Actions logs are public and GitHub masks only
**secrets**, never variables. So they are stored as secrets.

```bash
terraform output -raw state_bucket               | gh secret set TF_STATE_BUCKET
terraform output -raw workload_identity_provider | gh secret set GCP_WIF_PROVIDER
terraform output -raw plan_service_account       | gh secret set GCP_PLAN_SA
terraform output -raw apply_service_account      | gh secret set GCP_APPLY_SA
printf '%s' "$GCP_PROJECT_ID"                 | gh secret set GCP_PROJECT_ID
```

### Check the permissions

After any change to this stack, run the read-only permission check (Policy Troubleshooter). It
verifies what each CI account can do, including what it must never do, and exits non-zero on any
mismatch:

```bash
gcloud services enable policytroubleshooter.googleapis.com --project "$GCP_PROJECT_ID"   # once
bash infra/scripts/check-permissions.sh
```

Policy Troubleshooter cannot evaluate the condition that limits which roles `tf-apply` may grant,
so a second test makes real grants. Run **Actions → iam-grant-test → Run workflow** on `main` and
approve `prod`. As `tf-apply`, it creates a throwaway service account, grants it one runtime role
(must succeed), then tries owner, editor, IAM admin, token creator and project-wide secret access
(all must be refused), and deletes the account. It cannot run locally: as the project owner every
grant would succeed, so it would prove nothing.

### Provider lock file

[`.terraform.lock.hcl`](terraform/bootstrap/.terraform.lock.hcl) is committed with hashes for
Windows, macOS (Intel and Apple silicon) and Linux, so `init` gives the same providers
everywhere. After changing a provider version:

```bash
terraform providers lock -platform=windows_amd64 -platform=darwin_amd64 \
  -platform=darwin_arm64 -platform=linux_amd64
```

Then run `terraform init` and `terraform plan` from the other OS to confirm it shows no changes.

- [x] M1: GCP bootstrap applied (2026-10-31)
- [x] M1: state migrated to the bucket; local state and plan files deleted (2026-10-31)
- [ ] M1: clean `plan` from the second OS
- [x] M1: outputs stored as Actions secrets
- [ ] M7: Azure added to bootstrap; outputs stored as Actions secrets
- [ ] M8: AWS added to bootstrap; outputs stored as Actions secrets

## 6. Hand off to CI and lock down

The `prod` GitHub environment (Settings → Environments) requires the owner's approval and allows
only protected branches. Its name is part of the apply account's trust, so renaming it breaks
applies until the bootstrap stack is updated.

**How infrastructure changes flow** ([`.github/workflows/terraform.yml`](../.github/workflows/terraform.yml)):

| When | Job | Logs in as |
| --- | --- | --- |
| Every PR | `tf-plan`: plan the `gcp` stack (no state lock) | `tf-plan` |
| Every PR | `tf-apply-gate`: tries to act as `tf-apply` and **must be refused** | (refused) |
| Every PR | `tf-scan`: Trivy misconfiguration scan; HIGH or CRITICAL fails | none |
| Merge to `main` touching `infra/terraform/gcp/` or the app (`core/`, `adapters/`, `apps/`, `Dockerfile`, dependencies) | `tf-apply-gate-main`: tries `tf-apply` without `prod`, **must be refused** | (refused) |
| Same, then **you approve** | `tf-apply`: plan and apply, build and push the image, deploy it to Cloud Run, smoke test `/health` and one streamed answer | `tf-apply` |

Actions logs are public on this repo, so project-specific values are Actions **secrets**, which GitHub
masks everywhere; variables are never masked.

**Never create secret values through Terraform.** State is readable by the plan account. Terraform
creates secret containers; values are added by hand (Secret Manager) or by CI.

### Add the Gemini key (M1, once)

After the apply that creates the `gemini-api-key` secret, add its value from your `.env`. It is
piped in, so the key never appears in your shell history or on screen:

```bash
# macOS (zsh/bash), from the repo root
set -a; source .env; set +a
printf %s "$GEMINI_API_KEY" | gcloud secrets versions add gemini-api-key \
  --data-file=- --project "$GCP_PROJECT_ID"
gcloud secrets versions list gemini-api-key --project "$GCP_PROJECT_ID"   # one ENABLED version
```

```powershell
# Windows (PowerShell), from the repo root, after loading .env as in section 4. Piping would add a
# newline to the key, so it goes through a temporary file written without one.
$f = New-TemporaryFile
[IO.File]::WriteAllText($f, $env:GEMINI_API_KEY)
gcloud secrets versions add gemini-api-key --data-file=$f --project $env:GCP_PROJECT_ID
Remove-Item $f
gcloud secrets versions list gemini-api-key --project $env:GCP_PROJECT_ID   # one ENABLED version
```

To rotate, add a new version the same way and disable the old one
(`gcloud secrets versions disable <n>`). Only the `chatbot-run` service account can read it.

- [x] M1: Gemini key added to Secret Manager (2026-10-31). Check the length before adding: a
  truncated paste stores a key Gemini rejects with 401

- [x] M1: `gcp` stack plans on PR and applies on merge, with the app deploy (2026-11-01)
- [ ] M1: `shared` stack plans on PR and applies on merge
- [ ] M7: `azure` stack plans on PR and applies on merge
- [ ] M8: `aws` stack plans on PR and applies on merge
- [x] `prod` GitHub environment requires approval (2026-10-31)
- [x] `iam-grant-test` workflow passes: `tf-apply` can grant only the runtime roles (2026-10-31)
- [ ] Weekly scheduled `terraform plan` drift check enabled
- [ ] Local admin sessions logged out or downgraded to read-only — consoles are for looking only

## Not managed by Terraform

| Item | Managed by |
| --- | --- |
| App version rollouts | CI deploy step |
| Database schema | `migrations/` |
| Test users | Seed script |
| Gemini API key value | Pasted once into each cloud's secret store (Secret Manager, Container Apps secrets, SSM) |
