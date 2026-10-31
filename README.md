# spanish-support-chatbot

A Spanish-language support assistant for a fictional bank serving Spanish speakers in the US and
Latin America. It answers from each market's policy docs with citations, takes confirmed actions
(card status, block card, open ticket), and hands off to a human when unsure.

The goal is to show full system design, not just a chatbot: identity tiers, PII guardrails, an
eval gate in CI, tracing, cost tracking, and the same app running natively on GCP, Azure and AWS,
all on free tiers with synthetic data only.

**Status:** M0, setup and a local bot. No app code yet.

- [Roadmap and milestones](docs/roadmap.md)
- [Infrastructure bootstrap](infra/BOOTSTRAP.md)

Licensed under [Apache 2.0](LICENSE).
