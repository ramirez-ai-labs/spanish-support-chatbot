# 0001. One monorepo with a cloud-free core

- **Status:** Accepted
- **Date:** 2026-10-31
- **Milestone:** M0

## Context

The same chatbot ships natively on GCP, Azure and AWS, then to staff over MCP. The point is to show
that only adapters change between clouds. If each cloud lived in its own repo, the shared logic
(orchestration, prompts, guardrails, retrieval) would drift, and "only adapters change" could not
be checked.

## Decision

One public monorepo:

- `core/`: cloud-free orchestration and the ports (`LLMProvider`, `IdentityProvider`, ...)
- `adapters/{shared,gcp,azure,aws}/`: one implementation of each port per provider
- `apps/`: thin entry points per runtime, no business logic
- `infra/terraform/`, `evals/`, `tests/`, `docs/` alongside

The boundary is enforced, not just documented: `import-linter` fails CI if `core` imports a cloud
SDK, the web framework, `adapters` or `apps`, and layers must stay `apps -> adapters -> core`.
Every adapter for a port passes the same contract test suite. Each cloud image installs only its
own extras (`[gcp]`, `[azure]`, `[aws]`).

Existing lab repos (`rag-evaluation-lab`, `chatbot-evaluation-system`) stay separate and are
installed from pinned git tags, not vendored.

## Alternatives considered

- **Repo per cloud:** clean separation, but duplicates the core and hides drift.
- **Core as a published package plus thin cloud repos:** realistic at scale, but release overhead
  for one maintainer and slower iteration.

## Consequences

- One PR can change a port and all its adapters together; CI checks them in one run.
- A single CI and one set of standards apply to every cloud.
- Images must stay slim per cloud: extras and per-app Dockerfiles keep other clouds' SDKs out.
- The repo grows; folders are created only when a milestone needs them.
