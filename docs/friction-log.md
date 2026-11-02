# Friction log

What went wrong while building this, why, and what changed because of it. Each entry ends with
the rule it left behind, most of which now live in CLAUDE.md, a script or a test. Newest first.

No entry contains a secret's value; where a credential was exposed, it was rotated and the old
one confirmed dead.

## M2 · Retrieval

**A secret scanner flagged a password in CI** ([#98])
- **What:** GitGuardian flagged the password of the throwaway Postgres container CI starts for
  tests. It guarded nothing real, but a red security check on a public repo is still a red check.
- **Fix:** both test databases now use no password at all: the CI container trusts the runner, and
  the local one is published on `127.0.0.1` only. The branch was rewritten so the string never
  reached `main`.
- **Rule:** no credential literals in the repo, not even fake ones.

**The password script refused the new roles** ([#97])
- **What:** setting passwords for `app_gcp` and `indexer` failed with a usage message: the script
  allowlists roles, and the PR that added the roles didn't update it.
- **Why it was harmless:** the script refused before touching Neon, and the wrapper stopped before
  storing anything, so nothing was half-set.
- **Rule:** a change that adds a role checks every place that names roles.

**The same text got slightly different vectors** ([#95])
- **What:** the real-model contract suite caught that a passage embedded inside a padded batch
  came out different (cosine 0.998) from the same passage alone. The int8 model quantizes a
  batch as a whole, so padding shifts the numbers.
- **Fix:** passages are embedded one at a time. At ~70 ms each, the whole corpus still takes
  seconds.
- **Rule:** run contract suites against the real adapter, not just a fake.

**No `onnxruntime` for the development Mac** ([#95])
- **What:** the current `onnxruntime` has no wheel for Intel Macs on macOS 12; the last one is
  from 2024.
- **Fix:** it installs everywhere else (Linux, CI, Windows, Apple silicon); on that Mac the
  model's tests skip and the model runs in Docker. Production wasn't pinned to an old runtime to
  suit one laptop.

**The plan assumed the wrong model** ([#91])
- **What:** the roadmap said "e5-small", and the common `e5-small-v2` is English-only, which
  would rank Spanish questions poorly. Checked before any code: the design uses
  `multilingual-e5-small`.
- **Rule:** check the library and model details before writing the ADR, not after.

## M1 · Walking skeleton

**Four timeouts that weren't there** ([#94])
- **What:** a code review found four ways a request could hang or misbehave:
  - two sends could share one Turnstile challenge and leave a button disabled forever
  - the Gemini call had no timeout, so a stalled stream held the request for 300 s
  - an unreachable Google key endpoint returned 401, signing users out
  - the usage write's deadline didn't cover its own retry
- **Fix:** a challenge queue and timeout, a 30 s model timeout, a `503 auth_unavailable` that keeps
  the session, and a deadline sized for the retry.

**A page left open sent an expired bot-check token** ([#75])
- **What:** Turnstile tokens expire after 5 minutes, and the page took one when it loaded. A
  message sent 25 minutes later was refused.
- **Fix:** the page asks for a fresh token on every send.

**Sign-in emails bypassed the bot check** ([#76], [#77], [#78])
- **What:** the page asked Google directly to send the sign-in link, so anyone with the page's
  public key could send sign-in emails to strangers.
- **Fix:** the API sends the link behind Turnstile, with a role that can only send email, and the
  browser key is limited to two methods. A smoke test proves Google refuses a direct send.

**The first write after Neon idled failed** ([#74])
- **What:** Neon closes connections when its compute scales to zero, so the first usage write
  after a quiet spell hit a dead connection.
- **Fix:** reconnect once and retry; the log line `usage_write_reconnect` shows it working.

**CI couldn't create the sign-in config** ([#73])
- **What:** initializing Identity Platform needs a Firebase permission that the apply account
  shouldn't have.
- **Fix:** the config moved to the locally applied bootstrap stack. Running it from a laptop also
  needs `GOOGLE_CLOUD_QUOTA_PROJECT`, since those APIs reject user credentials without a quota
  project.

**A deploy ran before its migration** ([#67])
- **What:** the content-safety deploy wrote a column that migration 0003 hadn't added yet. One
  usage row was lost; the chat itself was fine.
- **Rule:** anything the code depends on lands and is applied first: a migration gets its own PR and
  is applied before the code; a secret's container, then its value, then the code. Now in CLAUDE.md.

**A default bucket permission bypassed state isolation** ([#57])
- **What:** the plan account was meant to be unable to read the stack that stores the Turnstile
  secret, through a conditional permission. But Cloud Storage's default "project viewers can read
  objects" binding granted the same access unconditionally.
- **Fix:** the bucket's permissions are now authoritative, which removes the default, and a PR
  check proves the plan account gets 403.
- **Rule:** prove a permission boundary with a test that tries to cross it.

**Neon refused a hashed password** ([#58])
- **What:** setting a role's password as a SCRAM hash, so the plain one never left the laptop,
  failed: Neon's control plane intercepts `ALTER ROLE ... PASSWORD` and needs the plain value.
- **Fix:** the script sends it over the owner's TLS connection and pipes the result straight into
  Secret Manager, never printing it.

**A database password reached a terminal** (M1)
- **What:** a command that mixed a heredoc with a secret piped on stdin printed the Neon owner's
  connection string inside a Python error.
- **Fix:** the owner password was rotated (`neon-bootstrap.sh --rotate-owner`) and the old one
  confirmed refused.
- **Rule:** a secret only ever travels on stdin, into a program that lives in a file.

**An API key ended up in shell history, and one was truncated** (M1)
- **What:** the Gemini key was once pasted on a command line, and an earlier copy in `.env` was
  missing characters, which surfaced as a 401 in the deploy smoke test.
- **Fix:** the exposed key was rotated. Keys are now added to Secret Manager from `.env` by
  script, and the smoke test names the failing check and the provider's status code ([#40]).

**Cloud Run dropped most traces** ([#43], [#47])
- **What:** Cloud Run's front end marks most requests "not sampled", and spans exported after the
  response were lost once CPU was throttled.
- **Fix:** the sampler ignores that flag, spans are flushed before the request ends, and FastAPI's
  own telemetry is off, since its span ends too late and its logs can include request text.

**A model call failed once with 503** (M1)
- **What:** a transient Gemini 503 failed a deploy's smoke test; rerunning passed.
- **Rule:** the free tier has no SLA, which is one reason the hot path will get retries and limits
  in M5.

[#40]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/40
[#43]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/43
[#47]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/47
[#57]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/57
[#58]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/58
[#67]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/67
[#73]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/73
[#74]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/74
[#75]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/75
[#76]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/76
[#77]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/77
[#78]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/78
[#91]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/91
[#94]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/94
[#95]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/95
[#97]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/97
[#98]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/98
