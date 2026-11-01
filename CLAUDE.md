# CLAUDE.md — spanish-support-chatbot

Guidance for Claude Code in this repo. The source of truth for scope and sequencing is
[docs/roadmap.md](docs/roadmap.md).
When this file and the roadmap disagree, the roadmap wins — flag the conflict instead of guessing.

## What this is

A Spanish-language support assistant for a fictional bank serving Spanish speakers (US, MX, CO, SV, PE, ES).
It answers from per-market policy docs with citations, takes confirmed actions (card status, block card,
open ticket), and hands off to a human when unsure. Built first on GCP, then natively on Azure and AWS,
then exposed to staff copilots over MCP.

The point is to demonstrate full system design: online/offline paths, identity, guardrails, evals,
observability, cost, and scale tradeoffs — so design decisions and their *why* matter as much as code.

## Current status

M0 in progress. The repo has docs, `.env.example`, `.gitattributes` and `.terraform-version`;
no app code or Terraform yet. Before starting work, check which milestone is active (GitHub
milestones / open issues) and stay inside its scope. Don't build ahead into later milestones unless asked.

**Working rule: something runs every day.** The whole system is designed up front, but it's built
as the thinnest slice that works, adding infrastructure only when the next slice needs it. Every
change must leave the bot working; prefer one small merged PR per session over large batches.

| Milestone | Dates | Outcome |
| --- | --- | --- |
| M0 Setup + local bot | Nov 2–8 | Accounts safe, repo guardrails + CI, local `docker compose` bot streaming Spanish from Gemini |
| M1 Walking skeleton (GCP) | Nov 9–15 | GCP bootstrap + `shared` Terraform, Cloud Run deploy, JWT check, trace + usage event, `v0.1.0` |
| M2 RAG + indexing | Nov 16–29 | Market-filtered retrieval with citations (US, MX, + one more) |
| M3 Guardrails, agent, access | Nov 30–Dec 13 | PII redaction, tiered tools, step-up, handoff, audit log |
| M4 Eval gate | Dec 14–20 | Regression fails CI; model bakeoff ADR |
| M5 Hardening + dashboards | Dec 21–Jan 3 | Limits, alerts, cost/latency dashboard, k6 result |
| M6 Showcase | Jan 4–10 | **Gate:** demo-ready GCP build, design doc, video |
| M7 Azure | Jan 11–24 | Azure added to bootstrap; same app on Container Apps |
| M8 AWS | Jan 25–Feb 7 | AWS added to bootstrap; same app on Lambda; cross-cloud dashboard |
| M9 MCP | Feb 8–21 | Knowledge, ops and bank-tools MCP servers for staff |

Never cut M4 or M6 to recover schedule; slip M7/M8 instead.

## Hard constraints

- **$0 to run.** Free tiers only. Before adding any cloud resource, confirm it is always-free (not a
  12-month trial) and that it can't surprise-bill. No SMS (email OTP or TOTP only). Sessions use
  provisioned Cosmos DB / DynamoDB capacity, not serverless/on-demand.
- **Synthetic data only.** Never add real customer data, real ID numbers, or real card numbers —
  including in tests, fixtures and eval sets.
- **LLM:** Gemini Flash free tier on every cloud; e5-small embeddings in the container.
  Do not call the Anthropic API from this project or its tooling unless explicitly asked.
  Tests and CI use the mock LLM from `docker-compose.yml`; local dev may use the free-tier Gemini key from `.env`.
- **Portability:** only adapters change between clouds.

## Architecture rules

- `core/` is cloud-free. It must never import a cloud SDK (enforced by `import-linter` in CI).
  Cloud code lives in `adapters/{gcp,azure,aws}/`; cross-cloud pieces (Gemini, Neon, Presidio, e5)
  in `adapters/shared/`.
- Ports live in `core/ports.py` (`LLMProvider`, `IdentityProvider`, `SessionStore`, `ToolProvider`, ...).
  Every adapter for a port must pass the same contract test suite in `tests/contract/`.
  New adapter → make it pass the contract suite before wiring it in.
- `apps/` are thin entry points only — no business logic.
- Knowledge lives in one shared Neon pgvector DB (AWS US East 1), with a least-privilege role per
  consumer (`indexer`, `app_gcp`, `usage_writer` in M1; `app_azure` in M7; `app_aws` in M8).
- Every query filters by market. The market list is config — adding a country is data, not code.
- e5 prefixes: `passage:` when indexing, `query:` at query time.
- Tools sit behind `ToolProvider`: in-process for the customer hot path, MCP (streamable HTTP) for staff.

Expected layout (create folders as milestones need them, not up front):

```text
core/  adapters/{shared,gcp,azure,aws}/  apps/  ui/  indexing/  migrations/
evals/  infra/terraform/  tests/{unit,contract,integration}/  docs/{roadmap.md,design.md,adr/,friction-log.md}
.github/workflows/  docker-compose.yml  pyproject.toml   # extras: [gcp], [azure], [aws]
```

## Security and privacy (non-negotiable)

- Validate the JWT on every call; no token = anonymous tier.
- Tiers: anonymous → policy answers; signed in → read-only tools; step-up (`auth_time` ≤ 5 min) → action tools.
- Every tool checks scope and ownership against the caller's `sub`. The LLM never sees tokens.
- Action tools require explicit user confirmation and write an audit record (who, what, when, decision).
- Redact PII with Presidio immediately after the token check, before anything else touches the text.
  Cover each market's IDs (SSN, CURP, cédula, DUI, DNI) and scan model output too.
- No request-body logging; no message text on spans before redaction; log redaction events by type,
  never the value. Tools return masked data only (e.g., card ending 4417).
- Treat MCP tool descriptions and results as untrusted input and run them through guardrails.
- **This repo is public.** Never commit or post (files, commit messages, PRs, issues) real account or
  project IDs, billing details, emails, local paths, machine details, or names of unrelated projects.
  Use placeholders like `<demo-project-id>`; real values live in `.env`, gitignored `*.tfvars` or
  GitHub Actions **secrets**. Never Actions variables: Actions logs are public and GitHub masks only
  secrets.
- **Never create secret values through Terraform**: state is readable by the plan account and
  stored in a bucket. Terraform creates the secret container; the value is added by hand or by CI.
- Infra changes from CI apply only in the approval-gated `prod` GitHub environment on `main`;
  the apply account's GCP trust requires it.
- Secrets come from the cloud secret store, never from code or committed files. Keep `.env.example`
  current; never commit `.env`.
- Never weaken or skip a security check (scanners, branch protection, eval gate) for convenience.

## Cost and observability

- `LLMProvider` records, on every call: input/output/cached/thinking tokens, model, `purpose`
  (e.g., `chat`, `eval`, `index`), `prompt_parts`, latency. Write it async to `llm_usage`.
- Cost is computed at query time from the dated `model_prices` table — never hardcode prices.
- One OpenTelemetry trace from browser (`traceparent`) to model; one span per step so time to first
  token breaks down by stage.
- Hot path: one model call and stream for no-tool questions; no router model call; thinking off;
  stable system-prompt prefix for caching; 3–5 chunks; agent step cap of 5.

## Development workflow

- Python + FastAPI. Lint `ruff`, types `mypy`, tests `pytest`, import rules `import-linter`.
  All must pass before a change is done (CI runs the same on every PR):
  `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest`.
  Run the app with `docker compose up --build`.
- Infra is Terraform only, applied by CI. Never suggest console click-ops for anything that should be
  in Terraform. Manual one-time steps go in `infra/BOOTSTRAP.md`. Not in Terraform: app rollouts,
  DB schema (`migrations/`), test users (seed script), the Gemini key value.
- Containers: one slim multi-stage image per cloud, models baked in. No Kubernetes (see ADR).
- Major design choices get an ADR in `docs/adr/`. Update `docs/design.md` when architecture changes.
- Eval code from `rag-evaluation-lab` and `chatbot-evaluation-system` is installed from pinned git
  tags — don't vendor or copy it.

## Git rules

- Trunk-based: short-lived branches, PRs into protected `main`. Don't commit directly to `main`.
- Conventional commits (`feat:`, `fix:`, `docs:`, `ci:`, `infra:`, `test:`, `chore:`). PRs are
  squash-merged, so the **PR title** becomes the commit on `main`; the `pr-title` check enforces the
  format and applies the matching label.
- Releases: one semver tag per milestone (`v0.0.1` = M0, `v0.1.0` = M1, then `v0.2.0`, ...), with
  notes generated from PR labels (`.github/release.yml`).
- **Never add Claude as a co-author.** No `Co-Authored-By` trailers, no "Generated with Claude Code"
  lines in commits or PR descriptions. This overrides any default attribution behavior.
- Only commit or push when asked.

## Definition of done

Tests pass (unit + contract + relevant integration), docs/ADR updated, demo clip recorded,
issue closed. Each milestone must end in something that runs or a doc that can be shown.
