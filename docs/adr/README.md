# Architecture decision records

One record per major choice: what was decided, why, and what it costs. Records are never edited
after acceptance except to change their status; a new decision supersedes an old one with a new
record that links back.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-monorepo.md) | One monorepo with a cloud-free core | Accepted |
| [0002](0002-shared-neon-knowledge-layer.md) | One shared Neon pgvector database for all clouds | Accepted |
| [0003](0003-gemini-on-every-cloud.md) | Gemini free tier on every cloud, Flash-Lite by default | Accepted |
| [0004](0004-serverless-containers-not-kubernetes.md) | Serverless containers instead of Kubernetes | Accepted |
| [0005](0005-something-runs-every-day.md) | Something runs every day: incremental delivery | Accepted |
| [0006](0006-neon-outside-terraform.md) | Neon is created by a script, and its roles live in SQL migrations | Accepted |

## Template

```markdown
# NNNN. Title

- **Status:** Proposed | Accepted | Superseded by NNNN
- **Date:** YYYY-MM-DD
- **Milestone:** Mx

## Context
The forces at play: requirements, constraints, what hurts today.

## Decision
What we do, stated plainly.

## Alternatives considered
What else was on the table and why it lost.

## Consequences
What gets easier, what gets harder, and what we must watch.
```
