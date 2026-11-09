# 0006. Neon is created by a script, and its roles live in SQL migrations

- **Status:** Accepted
- **Date:** 2026-11-01
- **Milestone:** M1 (project), with #20 (roles and grants)

## Context

The plan was for the `shared` Terraform stack to create the Neon project, its branches and one
least-privilege role per consumer (`indexer`, `app_gcp`, `usage_writer`, see
[ADR 0002](0002-shared-neon-knowledge-layer.md)). Checking the provider before using it turned up
four problems:

- **Live passwords in state.** On every refresh, the provider's role resource calls Neon's
  "reveal password" endpoint and saves the **current** password in state. The project resource
  does the same with the owner's connection string. Our state bucket is readable by `tf-plan`,
  which every PR can use, so any PR could read the database passwords. Resetting them by hand
  doesn't help: the next refresh copies the new value back.
- **Roles that aren't least privilege.** Neon gives every role created through its API, console
  or CLI (which is all a provider can use) membership in `neon_superuser`: `CREATEDB`,
  `CREATEROLE`, `BYPASSRLS`, `REPLICATION`. Neon's own guidance is to create application roles
  with SQL, which get only what they are granted.
- **An admin key in CI.** Neon has no read-only API key, so even planning would need an
  organization admin key, which no PR job should see.
- **The provider is in transition.** `kislerdm/neon` is archived; Neon is taking it over as
  `neondatabase/neon`, which has no release yet.

## Decision

- **The project is created once by a checked-in script**, `infra/scripts/neon-bootstrap.sh`, run
  from the owner's laptop like the bootstrap stack. It creates the project (AWS us-east-1,
  Postgres 17, compute fixed at 0.25 CU) with its `main` branch, the `chatbot` database and the
  `chatbot_owner` role. It pipes the owner's connection string straight into the
  `neon-owner-url` Secret Manager secret without printing it, and it is safe to rerun.
- **Roles and grants are SQL in `migrations/`**, alongside the tables they protect. Each app role is
  created with SQL, so it gets only its own `GRANT`s. Its password is generated and set by a script
  that stores the role's connection string in its own secret. The password is never in a file,
  in state or in a log.
- **Only the project owner can read `neon-owner-url`.** Terraform creates the empty container and
  grants nobody access. The app never uses the owner role.
- **The Neon API key stays on the owner's laptop.** CI never needs it.
- **Terraform's `shared` stack keeps Cloudflare and GitHub**, whose providers don't put secrets in
  state.
- **The `dev` branch is created when indexing first needs it (M2).**

## Alternatives considered

- **Neon in Terraform with a separate state that `tf-plan` can't read**, planned only in the
  approved `prod` job. This keeps everything in Terraform, but the passwords still sit in a
  state file, readable by `tf-apply`. That breaks the rule that `tf-apply` never reads secret
  values, and the roles would still be `neon_superuser` members.
- **Wait for `neondatabase/neon`.** It may handle passwords better, but there is no release to
  check yet, and #20 and #22 depend on the database.

## Amendment, 2026-11-06: connection strings share secrets

Secret Manager's free tier covers 6 active versions per billing account, and each connection string
had its own secret: 6 for Neon alone, and 9 in all with the session key. So they're now merged by
who reads them, as JSON in one version:

| Secret | Holds | Read by |
| --- | --- | --- |
| `neon-owner-urls` | the owner on `main` and `dev` | the project owner |
| `neon-indexer-urls` | `indexer` on `main` and `dev` | `indexer-ci` |
| `neon-api-urls` | the API's roles, by name | `chatbot-run` |

- **No account can read anything new.** Each secret holds only what its reader could already read,
  and the database roles still separate what each connection can do.
- **Merged values rotate one at a time.** A rotation writes a new version holding every value, and
  only the one role's password changes. The old version is destroyed straight after, so a rotation
  briefly exceeds the free tier by one version.
- **Outside tools don't help.** HCP Vault Secrets has closed, self-hosting Vault needs an always-on
  server, and any hosted tool still needs a token stored somewhere.

## Consequences

- No database credential is in Terraform state, in CI or in a PR's reach, and the app's roles are
  least privilege by construction.
- The Neon project isn't declarative. Drift isn't caught by a plan, so the script is the record:
  it's checked in, idempotent and reviewed like code. There is one project with one branch to
  watch.
- Migrations need the owner connection string. In M1 the owner runs them from a laptop; running
  them from CI would need a migrator identity and is a later decision.
- When `neondatabase/neon` ships, it's worth rechecking. Moving the project into Terraform is
  possible if the provider stops putting passwords in state, but roles stay in SQL either way.
