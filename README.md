# spanish-support-chatbot

A Spanish-language support assistant for a fictional bank serving Spanish speakers in the US and
Latin America. It answers from each market's policy docs with citations, takes confirmed actions
(card status, block card, open ticket), and hands off to a human when unsure.

The goal is to show full system design, not just a chatbot: identity tiers, PII guardrails, an
eval gate in CI, tracing, cost tracking, and the same app running natively on GCP, Azure and AWS,
all on free tiers with synthetic data only.

**Status:** M0, setup and a local bot.

## Run it locally

Needs Docker, or [uv](https://docs.astral.sh/uv/) for the non-Docker path.

```bash
cp .env.example .env            # add GEMINI_API_KEY, or set LLM_PROVIDER=mock to skip it
docker compose up --build
curl -N http://localhost:8000/v1/chat -H 'Content-Type: application/json' \
  -d '{"message": "¿Cómo bloqueo mi tarjeta?"}'
```

The reply streams back as server-sent events. Without Docker:
`uv sync && uv run uvicorn --factory apps.api.main:create_app` (load `.env` first).

Checks (the same ones CI runs): `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest`.

- [Roadmap and milestones](docs/roadmap.md)
- [Infrastructure bootstrap](infra/BOOTSTRAP.md)

Licensed under [Apache 2.0](LICENSE).
