# Spanish Support Chatbot — Roadmap & Milestones

Oct 30, 2026 · @Victor Ramirez

## Goal

Ship one live, end-to-end Spanish support chatbot on GCP by January 10, 2027, then build the same system natively on Azure and AWS by February 7, and expose it to staff copilots through MCP servers by February 21.

- **What it proves:** full system design and delivery: online and offline paths, identity, guardrails, evals, observability, cost, and scale tradeoffs.
- **Constraint:** $0 to run. Free tiers only, synthetic customer data only, Gemini free tier as the LLM on every cloud.
- **Portability:** one shared knowledge layer on Neon; the cloud-specific runtime sits behind adapters.
- **Pace:** evenings and weekends, 8 to 10 hours a week; something runs at the end of every session.
- **Scenario:** a bank serving Spanish speakers, in the US or any Spanish-speaking country, wants a Spanish support assistant that answers from policy docs, takes simple actions, and hands off to a human when unsure.

## Requirements

Twelve requirements, each owned by a milestone. The scale and latency numbers are design targets for the scenario, tested with a load test.

| Requirement | Target | Delivered in |
| --- | --- | --- |
| Answer from docs | Right market's policy (e.g., US, MX, CO, SV, PE, ES), with sources shown; handles regional Spanish | M2 |
| Take actions | Card status, block card, open ticket; actions need confirmation | M3 |
| Human handoff | On low confidence, repeated questions, or frustration | M3 |
| Identity | Anonymous, signed-in and step-up tiers; JWT checked on every call; tools check ownership | M1, M3 |
| Privacy | No PII reaches the model, including each market's ID numbers (SSN, CURP, cédula, DUI, DNI) | M3 |
| Cost visibility | Tokens per call by cloud, model and purpose; shadow-cost dashboard | M1, M5 |
| Observability | One trace from browser to model; time to first token measured | M1, M5 |
| Quality gate | A worse prompt, model or index fails CI | M4 |
| Scale | 50k conversations a day; answers start in under 3 seconds | M5 |
| Cost to run | $0 on free tiers | All |
| Portability | Same app on GCP, Azure and AWS; only adapters change | M7, M8 |
| MCP access | Knowledge, ops data and bank tools available to staff copilots over MCP, with the same identity, ownership checks and audit trail | M1 (port), M9 |

## Architecture decisions

The client, model, guardrails, orchestration and knowledge layers are shared; identity, serving, sessions, telemetry and secrets are native to each cloud behind adapters.

| Layer | GCP | Azure | AWS |
| --- | --- | --- | --- |
| Client | Cloudflare Pages + Turnstile + Web Analytics (shared) | Same | Same |
| Identity | Firebase Auth | Entra External ID | Cognito user pools |
| Step-up | Email one-time code or re-login, checked via `auth_time` (shared rule) | Same | Same |
| API / serving | Cloud Run, us-east4 | Container Apps, East US | Lambda (container image in ECR) + Web Adapter for streaming, us-east-1 |
| Guardrails | Presidio (shared) | Presidio + optional Content Safety | Presidio |
| Orchestration | Python agent controller (shared) | Same | Same |
| Model | Gemini Flash free tier + e5-small in the container (shared) | Same | Same |
| Knowledge | Neon pgvector, AWS US East 1, read-only role per cloud (shared) | Same | Same |
| Sessions | Firestore | Cosmos DB (provisioned) | DynamoDB (provisioned) |
| Traces and logs | OpenTelemetry → Cloud Trace + Logging | OpenTelemetry → Application Insights | OpenTelemetry → CloudWatch + X-Ray |
| Cost data | BigQuery + central `llm_usage` in Neon | Log Analytics + Neon | CloudWatch + Neon |
| Secrets | Secret Manager | Container Apps secrets | SSM Parameter Store |
| Deploy | GitHub Actions + Workload Identity Federation | GitHub Actions + federated credentials | GitHub Actions + IAM OIDC role |
| Tools and MCP | ToolProvider port; tools in-process for customers; MCP servers on Cloud Run (streamable HTTP) for staff (shared design) | Same | Same |

**Cost design:** the `LLMProvider` adapter records input, output, cached and thinking tokens plus `purpose` and `prompt_parts` on every call. Cost is computed at query time from a dated `model_prices` table, so free-tier usage also shows a production "shadow cost".

## Engineering standards

One monorepo, one SDLC and one infrastructure tool for all three clouds, set up in M0 and M1 so every later milestone follows them automatically.

### Repo structure

A single public monorepo in `ramirez-ai-labs`. The shared core never imports a cloud SDK (enforced by `import-linter` in CI), and each cloud image installs only its own extras.

```
spanish-support-chatbot/
├── core/                  # cloud-free: orchestration, guardrails, retrieval, prompts
│   └── ports.py           # LLMProvider, IdentityProvider, SessionStore, ...
├── adapters/
│   ├── shared/            # Gemini, Neon pgvector, Presidio, e5
│   ├── gcp/               # Firebase Auth, Firestore, Cloud Trace, Secret Manager
│   ├── azure/             # Entra External ID, Cosmos DB, App Insights
│   └── aws/               # Cognito, DynamoDB, CloudWatch/X-Ray, SSM
├── apps/                  # thin entry points: gcp-cloudrun, azure-containerapps, aws-lambda
├── ui/                    # Cloudflare Pages chat UI
├── indexing/              # offline pipeline into Neon
├── migrations/            # SQL schema: chunks, llm_usage, model_prices
├── evals/                 # per-market test sets, metrics, security tests
├── infra/terraform/       # bootstrap, modules, stacks
├── tests/                 # unit, contract (one suite every adapter passes), integration
├── docs/                  # roadmap.md, design.md, adr/, friction-log.md
├── .github/workflows/     # ci, deploy-gcp, deploy-azure, deploy-aws, index, eval, drift
├── docker-compose.yml     # local API + Postgres with pgvector + mock LLM
└── pyproject.toml         # extras: [gcp], [azure], [aws]
```

Existing lab repos stay separate; their eval code is installed from pinned git tags.

### SDLC

| Area | Standard | Tooling (free) |
| --- | --- | --- |
| Planning | Milestone = GitHub milestone; task = issue | GitHub Issues + Projects |
| Decisions | One ADR per major choice | `docs/adr/` |
| Branching | Trunk-based; short branches; PR into protected `main` | Branch protection |
| Commits and versions | Conventional commits; a semver tag per milestone (`v0.1.0` = M1) | Generated release notes |
| Review | PR template with checklist; self-review plus an AI review pass | `.github/pull_request_template.md` |
| Testing | Unit, adapter contract, integration, post-deploy smoke tests | pytest, GitHub Actions |
| Quality gates | Lint, type check, tests, import rules and the eval gate must pass to merge | Ruff, mypy, import-linter |
| Security | Secret, dependency, code and image scanning | Secret scanning, Dependabot, CodeQL, Trivy |
| Containers | One slim multi-stage image per cloud, models baked in | Docker; Kubernetes out of scope (see ADR) |
| Deploy and rollback | Deploy from CI only; roll back by shifting traffic to the last good revision | Cloud Run revisions, Container Apps revisions, Lambda aliases |
| Done | Tests pass, docs updated, demo clip recorded, issue closed | PR checklist |

### Infrastructure as code

Everything is Terraform, applied by CI; the web consoles are for looking only.

1. **Manual, once:** accounts, billing, the Gemini key in AI Studio, AWS credit activities. Documented in `infra/BOOTSTRAP.md`.
2. **Bootstrap Terraform, run locally once:** a private, versioned GCS state bucket and keyless GitHub OIDC trust, GCP first in M1; Azure and AWS are added in M7 and M8.
3. **Main Terraform, run by CI:** separate stacks with their own state: `shared` (GitHub, Cloudflare, Neon), `gcp`, `azure`, `aws`, each with `dev` and `prod` variables.

- PRs post `terraform plan` as a comment; merging runs `apply`, with approval required for `prod`.
- A weekly scheduled plan catches drift from manual console changes.
- Not in Terraform: app version rollouts (CI deploy step), database schema (`migrations/`), test users (seed script), the Gemini key's value (pasted once into each secret store).

## Milestones at a glance

&#91;embedded content: roadmap · 9 milestones, 1 gate\]

M0 to M6 build the GCP showcase; the January 10 gate is the point where it's demo-ready for interviews. M7 and M8 rebuild it natively on Azure and AWS, sharing the same knowledge layer; M9 exposes it to staff copilots over MCP.

## Milestone tasks

Working rule: something runs every day. Design the whole system up front, build the thinnest slice that works, then add infrastructure only when the next slice needs it. Each session ends with the bot still working and one small change merged. Identity, telemetry and cost tracking start in M1, not at the end.

### M0 · Setup (Nov 2–8)

Done when: accounts are safe, the repo has its guardrails, and a local chatbot answers in Spanish through Gemini. Kept small on purpose: a few evenings.

**Demo:** start the stack with Docker Compose, ask "¿Cómo bloqueo mi tarjeta?" and get a streamed Spanish answer on your laptop.

- [x] Day 1: $1 budget alert on the GCP billing account and $1 spend caps on Cloud Run and the Gemini API; tag everything `project=showcase` (MFA moved to M1; Azure and AWS budgets to M7 and M8)
- [x] Day 1: GCP projects `chatbot-demo` (billing on) and `chatbot-gemini` (no billing); Gemini API key; start `infra/BOOTSTRAP.md` for manual steps
- [x] Day 1: monorepo `spanish-support-chatbot` in `ramirez-ai-labs` with Apache 2.0 license, `.gitignore`, `.env.example` and the folder skeleton, merged through your first PR
- [x] Day 2: local chatbot: minimal FastAPI `/v1/chat` streaming from Gemini behind the `LLMProvider` port, run with `docker compose`
- [x] Day 2: one-page test UI (or curl) that shows the streamed answer
- [x] Day 3: SDLC basics: branch protection on `main`, PR template, conventional commits, GitHub milestones M0 to M9
- [x] Day 3: basic CI: Ruff, mypy, pytest, `import-linter` as required checks; secret scanning and Dependabot on
- [x] Day 3: first ADRs: monorepo, shared Neon, Gemini on every cloud, serverless containers instead of Kubernetes, "something runs every day"

### M1 · Design doc and walking skeleton on GCP (Nov 9–15)

Done when: a deployed page answers a Spanish question through Gemini, the API rejects invalid tokens, every call produces a trace and a usage event, and CI deploys on push.

**Demo:** ask in Spanish and get a streamed answer; sign in and show the token check, the trace and the token count for that call.

- [x] Bootstrap Terraform run locally, GCP only: GCS state bucket and GitHub OIDC trust for GCP
- [ ] MFA on GitHub and Google first (the `prod` approval is only as safe as the GitHub login), then Neon and Cloudflare before the `shared` stack uses them. Azure and AWS get MFA in M7 and M8
- [ ] `shared` Terraform stack via CI: GitHub settings, Cloudflare Pages and Turnstile, Neon project in AWS US East 1 with pgvector, `dev` branch and roles (`indexer`, `app_gcp`, `usage_writer`)
- [ ] `gcp` Terraform stack: Cloud Run service, Artifact Registry, Firestore, Secret Manager, BigQuery dataset, budget
- [ ] Deploy the M0 local chatbot to Cloud Run first, then add the pieces below one per session
- [ ] `docs/design.md`: requirements, layered diagram with the identity layer, first tradeoffs table
- [ ] Slim multi-stage Docker image with e5 and Presidio models baked in; Trivy and CodeQL scans in CI
- [ ] Firebase Auth email sign-in; JWT validation behind the `IdentityProvider` adapter; no token = anonymous tier
- [ ] `ToolProvider` port in `core/` with an in-process adapter, so tools can later move behind MCP without changing the agent
- [ ] Contract test suite started: every adapter for a port must pass the same tests
- [ ] Chat UI on Cloudflare Pages: streaming, `traceparent` header, Turnstile, Web Analytics
- [ ] OpenTelemetry in the API, exporting to Cloud Trace and Cloud Logging
- [ ] `migrations/` for `llm_usage` and `model_prices`; usage event in `LLMProvider` with tokens, model, `purpose`, `prompt_parts`, latency; async write
- [ ] CI deploy via Workload Identity Federation, post-deploy smoke test, tag `v0.1.0`

### M2 · RAG and indexing pipeline (Nov 16–29)

Done when: answers cite the right market's policy chunk, starting with three markets: US, Mexico and one Central or South American country.

**Demo:** the same question as a US customer and a Mexican customer gets different answers, each with sources.

- [ ] 20 to 40 synthetic Spanish FAQ and policy docs, tagged by market (e.g., US, MX, PE) or all; the market list is config, so adding a country is data, not code
- [ ] Indexing job in GitHub Actions: clean, chunk by headings, metadata, e5 `passage:` embeddings, upsert by content hash
- [ ] Index into the Neon `dev` branch first; promote to `main` only after checks pass
- [ ] pgvector table plus HNSW index; market filter on every query, taken from the user's profile or a UI selector
- [ ] e5 embedder in the API container with the `query:` prefix; citations shown in the UI

### M3 · Guardrails, agent and access control (Nov 30–Dec 13)

Done when: the bot redacts PII, takes one confirmed action only for a step-up-verified account owner, and hands off when unsure.

**Demo:** a typed SSN is redacted; blocking a card is refused when anonymous and allowed after step-up.

- [ ] Presidio with a Spanish model and ID patterns per market (US SSN, Mexico CURP, Colombia cédula, El Salvador DUI, Peru DNI), added as markets are added
- [ ] PII handling: redact right after the token check; no request-body logging and no message text on spans before redaction; consistent placeholders for names (`<PERSON_1>`); scan model output too
- [ ] Tools return masked data only (e.g., card ending 4417), never full numbers
- [ ] Log redaction events by type, never the value
- [ ] Agent controller: step cap of 5, per-conversation token cap, tool allowlist, confirmation before actions
- [ ] Fast path: no-tool questions get one model call and stream; the tool loop runs only when needed; no separate router model call
- [ ] Parallel pre-work after the token check: redaction, query embedding and session load together (`asyncio.gather`)
- [ ] Lean prompts: 3 to 5 chunks, trimmed history, compact system prompt kept identical at the start for prompt caching; Gemini thinking off on the hot path
- [ ] Mock tools: `check_card_status`, `block_card`, `open_ticket`
- [ ] Tiered access: anonymous = policy answers; signed in = read-only tools; step-up = action tools
- [ ] Tools check scope and ownership against the user's `sub`; the LLM never sees tokens
- [ ] Step-up via email one-time code or re-login, enforced by `auth_time` within 5 minutes
- [ ] Staff role for the handoff queue via Firebase custom claims
- [ ] Audit log of every action: who, what, when, decision
- [ ] Human-handoff rule: low retrieval score, repeated question or frustration
- [ ] Firestore sessions keyed to `sub`, with random IDs and a 30-day TTL

### M4 · Evaluation quality gate (Dec 14–20)

Done when: a worse prompt, model or index fails CI, and eval cost is tracked separately.

**Demo:** a bad prompt change is blocked by CI; the bakeoff table shows why the model was chosen.

- [ ] Per-market test sets, about 50 questions each with expected sources, covering regional vocabulary (e.g., celular vs. móvil, cuenta de cheques vs. cuenta corriente)
- [ ] Retrieval metrics from rag-evaluation-lab (pinned git tag): Recall@k, MRR
- [ ] Answer quality via LLM-as-judge from chatbot-evaluation-system (pinned git tag), with a judge model different from the candidates
- [ ] Security tests: cross-user access, prompt injection against tools, and PII leaks including tricky cases (SSN with spaces or no dashes, numbers written out in words, every market's ID format)
- [ ] Eval calls tagged `purpose=eval` in `llm_usage`
- [ ] CI gate: block merge if scores drop below the last baseline; make it a required check
- [ ] Model bakeoff: free-tier Gemini Flash and Flash-Lite (optionally one or two Bedrock models on AWS credits), same test sets, retrieved chunks and prompt; scored on quality, grounding, tool accuracy, time to first token and shadow cost
- [ ] `eval --model <name>` CI job so the bakeoff reruns whenever a new model ships
- [ ] ADR: chosen model, the cheapest and fastest that clears the quality bar

### M5 · Hardening, dashboards and cost controls (Dec 21–Jan 3)

Done when: limits, alerts and a cost and performance dashboard are live, with a load-test result.

**Demo:** the dashboard with cost per conversation, time to first token by stage, and the load-test result.

- [ ] Per-user rate limits by tier; Cloud Run max-instance cap; retry with backoff for Gemini 429s
- [ ] Per-user daily token budget by tier
- [ ] Cold starts: load e5 and Presidio at startup, quantized ONNX e5, startup CPU boost, warm-up ping that also wakes Neon, one reused HTTP client and DB pool
- [ ] Answer cache for near-identical public policy questions, per market; never for signed-in tool results
- [ ] Early stream event ("Buscando en las políticas…") and citations shown before the answer; no buffering in the stream
- [ ] One OpenTelemetry span per step so time to first token breaks down by stage
- [ ] Turn-level logs to BigQuery: latency, tokens, retrieval scores, handoffs, redaction counts, outcome
- [ ] Looker Studio dashboard: shadow cost per conversation and per resolved conversation, cost by model and purpose, `prompt_parts` breakdown, cache hit rate, p50 and p95 time to first token by stage, redactions by type
- [ ] Alerts on shadow-cost or token spikes and on error rate
- [ ] k6 load test against a mocked LLM; record p95 latency against the 3-second target

### M6 · Showcase package (Jan 4–10)

Done when: a stranger can understand, run and judge the project from the repo.

**Demo:** the 5-minute video and the design doc; the GCP showcase is live.

- [ ] Final design doc: requirements, diagram, tradeoffs, identity, cost, observability, scaling plan, portability table
- [ ] 5-minute demo video in Spanish and English: identity tiers, a step-up action, the cost dashboard
- [ ] Friction log
- [ ] README with architecture image and quick start
- [ ] Resume bullet, LinkedIn post and Medium article

### M7 · Azure build (Jan 11–24)

Done when: the same UI talks to an Azure backend with the same answers, auth tiers and cost tracking.

**Demo:** the same UI and answers, now served from Azure.

- [ ] First: MFA on the Azure account and a $1 budget alert; Azure CLI upgraded
- [ ] Add Azure to the Terraform bootstrap: GitHub federated credentials; add the `app_azure` Neon role
- [ ] `azure` Terraform stack: resource group, Container Apps environment, Cosmos DB, Application Insights, budget
- [ ] First session: deploy the existing image to Container Apps with GCP-free adapters stubbed, so something runs on Azure on day one
- [ ] Container Apps in East US, image from GitHub Container Registry, deployed by CI
- [ ] Entra External ID adapter: same JWT checks, roles from app roles
- [ ] Cosmos DB session adapter with provisioned throughput, not serverless
- [ ] Azure adapters pass the shared contract test suite
- [ ] OpenTelemetry to Application Insights; usage events to Log Analytics and Neon
- [ ] Secrets in Container Apps secrets
- [ ] Optional: Content Safety in the guardrails layer
- [ ] ADR and design doc section: what changed on Azure and why

### M8 · AWS build (Jan 25–Feb 7)

Done when: the same UI talks to an AWS backend, and one dashboard compares cost and latency across all three clouds.

**Demo:** the same UI on AWS, plus one dashboard comparing cost and latency across all three clouds.

- [ ] First: MFA on the root account; IAM user for daily work with MFA, stop using root; $1 budget alert; AWS CLI upgraded
- [ ] Optional: earn the $100 credit activities, deleting any EC2 instance or database right after
- [ ] Add AWS to the Terraform bootstrap: GitHub IAM OIDC role; add the `app_aws` Neon role
- [ ] `aws` Terraform stack: ECR, Lambda, DynamoDB, Cognito, SSM parameters, budget
- [ ] First session: run the existing image on Lambda with stubbed AWS adapters, so something runs on AWS on day one
- [ ] Lambda from a container image in ECR (the models exceed the zip limit), Web Adapter for streaming, us-east-1
- [ ] Cognito adapter: same JWT checks, roles from `cognito:groups`
- [ ] DynamoDB session adapter with provisioned capacity, not on-demand
- [ ] AWS adapters pass the shared contract test suite
- [ ] OpenTelemetry to CloudWatch and X-Ray; usage events to CloudWatch and Neon
- [ ] Secrets in SSM Parameter Store
- [ ] Cross-cloud dashboard on `llm_usage` in Neon: cost, tokens and latency by cloud
- [ ] Decide before April 30, 2027 whether to upgrade AWS to the Paid plan
- [ ] ADR and design doc section: what changed on AWS and why

### M9 · MCP layer (Feb 8–21)

Done when: staff copilots can search policies, query cost and quality data, and use bank tools over MCP, with the same auth, ownership checks and audit log as the chatbot.

**Demo:** in Claude, a staff user asks "What was cost per conversation by cloud this week?" and "What's our card-replacement policy in Mexico?", and gets answers with citations.

- [ ] Knowledge MCP server (read-only): `search_policies(market, query)` over the shared Neon index, returning chunks with citations
- [ ] Ops MCP server (read-only, staff role): `llm_usage`, eval results and dashboard queries
- [ ] Bank tools MCP server: `check_card_status`, `block_card`, `open_ticket` as an MCP adapter behind the `ToolProvider` port; action tools require step-up
- [ ] OAuth-based MCP authorization reusing the identity layer: same JWT checks, roles and ownership rules
- [ ] Handoff copilot flow: human agents pull the redacted conversation summary and relevant policies over MCP
- [ ] Security: server allowlist, read-only by default, tool descriptions and results treated as untrusted and run through guardrails, every call audited
- [ ] Deploy the servers on Cloud Run with streamable HTTP via Terraform; customer hot path stays in-process
- [ ] Contract tests: in-process and MCP tool adapters pass the same suite
- [ ] ADR and design doc section: why tools sit behind a port, and when MCP versus in-process calls are used

## System design practice (ongoing)

Two timed 45-minute sessions a week, out loud, alongside the build.

Use the same framework every time:

1. Clarify requirements
2. Estimate the numbers
3. Sketch the architecture, split into online and offline paths
4. Design the data flow
5. Cover GenAI concerns: retrieval, guardrails, evaluation, cost, latency
6. List failure modes and fixes
7. Plan the rollout

Prompts, one per week, then repeat:

- [ ] Multilingual support bot for 10M users
- [ ] RAG platform shared by 50 internal teams
- [ ] Ingestion pipeline for 1M PDFs, including scans
- [ ] Eval system that blocks bad prompt or model changes
- [ ] An agent that safely takes actions in a customer's systems

## Risks

| Risk | What happens | Fallback |
| --- | --- | --- |
| Interview lands mid-build | Project isn't finished | Each milestone ends demoable; present what's live plus the design doc |
| Gemini free-tier limits or model changes | 429 errors, or a model leaves the free tier | Retry with backoff; switch to Flash-Lite; provider sits behind an adapter |
| Token use grows unnoticed | Free-tier quota runs out; production estimate balloons | Usage events from M1, token caps, spike alerts, `prompt_parts` breakdown |
| Holiday weeks (M5) | Hardening slips | Push M7 and M8 back a week; never cut M4 or M6 |
| Presidio misses a market's ID format | PII reaches the model | A custom pattern per market plus tests in the eval suite |
| Auth gap lets one user reach another's data or tools | Data leak or unauthorized action | Ownership checks in every tool; cross-user tests in the eval suite |
| SMS step-up costs money | Surprise charges | Email one-time codes or TOTP only |
| Shared Neon limits | Three apps exhaust compute or connections | Pooled connections; watch the shared usage page; pause unused projects |
| AWS Free plan ends April 30, 2027 | AWS build stops | Upgrade to the Paid plan before then, or keep AWS as a recorded demo |
| Azure 12-month services expire (Oct 2027) | Charges start | Build only on always-free services |
| MCP tool poisoning or over-broad access | Injected instructions in tool output, or staff tools reaching customer data | Allowlisted servers, read-only by default, guardrails on tool output, same ownership checks and audit log |
