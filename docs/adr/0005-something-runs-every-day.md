# 0005. Something runs every day: incremental delivery

- **Status:** Accepted
- **Date:** 2026-10-31
- **Milestone:** All

## Context

The project is built in evenings and weekends, 8 to 10 hours a week, across four months. Large
batches of setup (all three clouds' infrastructure before any feature) would leave weeks with
nothing to demo, and an interview could land at any point.

## Decision

- Design the whole system up front (requirements, ports, data flow), but build the thinnest slice
  that works and add infrastructure only when the next slice needs it.
- Each session ends with the bot still working and one small PR merged.
- Each cloud's bootstrap is added in the milestone that first deploys there: GCP in M1, Azure in
  M7, AWS in M8. Budget alerts and MFA still cover all accounts from M0 day 1.
- CI enforces "still working": lint, types, import rules, tests and an image smoke test are
  required checks on `main`.

## Alternatives considered

- **Infrastructure first, features later:** a clean foundation, but long stretches with nothing
  that runs, and setup work for clouds that may slip.
- **Features first, infrastructure at the end:** fast early demos, but identity, telemetry and
  cost tracking bolted on late are exactly the parts that prove system design.

## Consequences

- There is always something to show, and an interview mid-build can use whatever is live.
- Some rework: early slices are simpler than their final form (for example, M0 has no auth).
  The up-front design keeps that rework inside adapters.
- Identity, telemetry and cost tracking still start in M1, not at the end.
- M4 (eval gate) and M6 (showcase) are never cut; schedule slips come out of M7 and M8.
