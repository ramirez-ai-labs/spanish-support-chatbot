## Why

<!-- The problem or milestone task this solves. Link the issue: Closes #… -->

## What changed

-

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest` pass
- [ ] The bot still runs: `docker compose up --build` and one `/v1/chat` request
- [ ] Docs / ADR updated if behaviour or architecture changed
- [ ] No secrets, account IDs or personal details (this repo is public)
- [ ] Synthetic data only
