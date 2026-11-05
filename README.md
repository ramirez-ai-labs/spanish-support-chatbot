# spanish-support-chatbot

A Spanish-language support assistant for a fictional bank serving Spanish speakers in the US and
Latin America. It answers from each market's policy docs with citations, takes confirmed actions
(card status, block card, open ticket), and hands off to a human when unsure.

The goal is to show full system design, not just a chatbot: identity tiers, PII guardrails, an
eval gate in CI, tracing, cost tracking, and the same app running natively on GCP, Azure and AWS,
all on free tiers with synthetic data only.

**Status:** M2 shipped: answers come from each market's policy docs (US, MX, SV, ES), with
sources under each answer. M3 is in progress: personal data is redacted before retrieval and the model, and in answers
as they stream; tools, step-up and handoff are next. Try it at
https://spanish-support-chatbot.pages.dev:
- **Behind the page:** the chat page on Cloudflare Pages calls the API on Cloud Run.
- **Deploys:** both are deployed from CI after an approval.
- **On every message:**
  - a bot check
  - retrieval from that market's policy chunks in Neon pgvector
  - Gemini's content filters
  - one trace from the browser to the model
  - one usage row with its shadow cost

M2 (retrieval and indexing) shipped as
[`v0.2.0`](https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.2.0).
M1 shipped as [`v0.1.0`](https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.1.0),
and M0 (local bot, CI, guardrails) as
[`v0.0.1`](https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.0.1).
[`v0.1.1`](https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.1.1) added
optional sign-in by email link, sent by the API behind the same bot check as chat.

## Run it locally

Needs Docker, or [uv](https://docs.astral.sh/uv/) for the non-Docker path.

```bash
cp .env.example .env            # add GEMINI_API_KEY, or set LLM_PROVIDER=mock to skip it
docker compose up --build
curl -N http://localhost:8000/v1/chat -H 'Content-Type: application/json' \
  -d '{"message": "¿Cómo bloqueo mi tarjeta?", "market": "MX"}'
```

Every message names a market from `GET /v1/markets`. Compose indexes the docs in memory with e5
at startup, so answers come with sources, but a `.env` copied from `.env.example` turns that off
(`EMBEDDER=none`, `KNOWLEDGE_STORE=none`): set them to `e5` and `memory` to keep it.

The reply streams back as server-sent events. Without Docker:
`uv sync && uv run uvicorn --factory apps.api.main:create_app` (load `.env` first).

To use the chat page against the local API, serve [`ui/`](ui/) and open http://localhost:8080:

```bash
python3 -m http.server 8080 -d ui
```

Locally, the bot check is off and the page uses Cloudflare's always-pass test key. The deployed page
is at https://spanish-support-chatbot.pages.dev.

Checks (the same ones CI runs): `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest`.

Every adapter for a port passes the same contract suite in [`tests/contract/`](tests/contract/).
Tests that call a real service, like the Gemini adapter's, are marked `integration`. CI skips
them; run them locally with the key from `.env`: `uv run pytest -m integration`.

## Read the build

New here? In this order:

1. **[Design, §3](docs/design.md#3-architecture):** the target architecture, then what runs
   today and how data flows across each trust boundary.
2. **[Build log](docs/build-log.md):** how it was built, step by step, with the PRs and the
   numbers measured along the way.
3. **[Architecture decisions](docs/adr/README.md):** each major choice, its alternatives and
   its cost.
4. **[Friction log](docs/friction-log.md):** what went wrong, why, and the rule it left behind.
5. **[Design, §8](docs/design.md#8-cost-and-scale):** cost, free tiers, and build vs. buy against
   each cloud's managed services.
6. **[Roadmap](docs/roadmap.md):** what's done and what's next, milestone by milestone.

For running the infrastructure yourself: [infra/BOOTSTRAP.md](infra/BOOTSTRAP.md).

Licensed under [Apache 2.0](LICENSE).
