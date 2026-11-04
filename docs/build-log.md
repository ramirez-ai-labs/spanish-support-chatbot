# Build log

How this system was built, in order: what each step added, why it came then, and what it measured.
The [roadmap](roadmap.md) says what's done, the [design doc](design.md) how it works now, the
[ADRs](adr/README.md) why each big choice was made. What went wrong along the way is in the
[friction log](friction-log.md).

One rule shaped the order ([ADR 0005](adr/0005-something-runs-every-day.md)): **something runs every
day.** The whole system was designed first, then built as the thinnest slice that works, adding a
piece of infrastructure only when the next slice needed it. Every PR left the bot working.

## M0 · Local bot and guardrails (Oct 31, [`v0.0.1`][v0.0.1])

**Goal:** a bot that streams Spanish answers locally, inside a repo that is safe to make public.

1. **Plan before code.** The roadmap, CLAUDE.md and a bootstrap guide came first ([#1], [#3]),
   then a pass to replace every real account identifier with placeholders ([#5]): the repo is
   public, so nothing private ever lands in it.
2. **The first slice** ([#7]): FastAPI streaming server-sent events from Gemini, with a
   cloud-free `core/` behind an `LLMProvider` port, and a mock model so tests never need a key.
3. **CI from day one** ([#8], [#13]): lint, types, import rules (`core/` can't import a cloud SDK),
   tests and an image smoke test on every PR, plus conventional PR titles that label the release
   notes.
4. **The first five ADRs** ([#12]): monorepo, one shared Neon, Gemini on every cloud, serverless
   containers instead of Kubernetes, and "something runs every day".

## M1 · Walking skeleton on GCP (Nov 1–2, [`v0.1.0`][v0.1.0], [`v0.1.1`][v0.1.1])

**Goal:** a deployed page that answers a Spanish question, rejects invalid tokens, and produces a
trace and a usage record for every call, deployed by CI.

**Keyless, least-privilege CI before any app infrastructure.**
- A locally applied `bootstrap` stack: state bucket and Workload Identity Federation, so CI never
  holds a key ([#27]).
- Two accounts: `tf-plan` (read-only, every PR) and `tf-apply` (only for jobs on `main` in the
  approval-gated `prod` environment; Google checks the environment, not just GitHub) ([#28]).
- `tf-apply` can manage secret *containers* but never read a secret *value* ([#30]), and can grant
  only a fixed list of runtime roles, which a CI test proves by trying the forbidden ones ([#34]).

**Then the app, one piece per PR, each live before the next.**

| Step | PR | What it added |
| --- | --- | --- |
| Deploy | [#38], [#39] | Cloud Run as its own runtime account, deployed after approval, with a smoke test |
| Tracing | [#43], [#47] | One trace per request in Cloud Trace, continuing the browser's `traceparent`, with no message text on spans |
| Token check | [#46] | Firebase ID tokens verified on every call (RS256 pinned); no token = anonymous tier |
| Contract suites | [#48], [#49] | One test suite per port that every adapter must pass; a `ToolProvider` with the tier rule, ahead of the first tool |
| Image hygiene | [#52] | Trivy, CodeQL and an image size budget (226 MB at the time) |
| Neon | [#53], [#56], [#59] | A scripted project ([ADR 0006](adr/0006-neon-outside-terraform.md)), SQL migrations, an insert-only `usage_writer` role, and one usage row per call with its shadow cost computed at query time |
| State isolation | [#54], [#57] | The plan account can't read the stack that stores the Turnstile secret, proven by a PR check |
| Bot check | [#60], [#61] | Turnstile on every message, failing closed; CORS for the page; a signed identity so the deploy job's smoke test can still ask a real question |
| The page | [#62] | Cloudflare Pages with a strict CSP, starting the trace in the browser |
| Content safety | [#66] | Gemini's filters at the strictest threshold, and a Spanish refusal that replaces a blocked answer |

**`v0.1.1`: sign-in, and the settings that guard CI itself.**
- GitHub settings as code: a `main` ruleset with nine required checks, and the `prod` approval
  admins can't bypass ([#69]).
- Email-link sign-in through Identity Platform, giving the signed-in and step-up tiers ([#71]).
- Sign-in emails moved behind the same bot check as chat ([#76], [#77], [#78]). The API sends the
  link with a role that can only send email, and the page's browser key is limited to two methods,
  so a direct call to send email is refused by Google. A deploy smoke test checks both.

## M2 · Retrieval (Nov 2–3, [`v0.2.0`][v0.2.0])

**Goal:** answers cite the right market's policy chunk, for the US, Mexico, El Salvador and Spain.

1. **Design first** ([#91], [ADR 0007](adr/0007-rag-design.md)). Checking the libraries before
   writing code changed three things:
   - the model must be **multilingual** e5 (`e5-small-v2` is English-only)
   - pgvector filters *after* an HNSW scan, so a market filter can silently return too few chunks;
     the fix is iterative scans, which need pgvector 0.8
   - the image would grow enough to threaten the free 0.5 GB registry
2. **The schema, applied before any code uses it** ([#92]): the `chunks` table, an HNSW index created
   only if pgvector is 0.8 or later (Neon runs 0.8.0), and two roles: `indexer` (writes `chunks`
   only) and `app_gcp` (reads `chunks` only).
3. **The knowledge** ([#93]): 28 synthetic Spanish policy docs, six topics in each market plus four
   for all, written so the same topic really differs by market (SSN or ITIN vs. INE and CURP vs. DUI
   vs. DNI; ACH vs. SPEI vs. remittances vs. SEPA). A test checks no doc contains anything shaped like
   a real card, ID, email or phone number.
4. **Embeddings in the container** ([#95]): multilingual-e5-small, int8 ONNX, pinned by revision and
   checksum, with no PyTorch.
5. **Credentials, then values, then code** ([#96], [#97]): a secret per role, and an indexing account
   Google trusts only for one workflow file on `main`.
6. **The indexer** ([#98]): one chunk per section, embedding only new or changed chunks (keyed by a
   hash that includes the model), one transaction per run.
7. **The search** ([#100]): a `KnowledgeStore` port whose contract suite proves no chunk ever comes
   from another market, against real pgvector.
8. **Promotion through `dev`** ([#86]): the bootstrap script creates the Neon `dev` branch, and the
   `index` workflow indexes it, checks it, and only then indexes `main`. The checks are that the store
   matches the docs exactly, every market has chunks, and 20 fixed questions find their doc
   (`knowledge/smoke.yaml`). The same smoke set runs on every PR with the real model, in memory.
   Its first run found a real miss: Spain's ATM-fee section was titled just "Cajeros", so the
   Latin American phrasing "cajero automático" didn't find it.
9. **Retrieval in the chat** ([#88]): with a market, the API retrieves 4 chunks off the event loop,
   sends a `sources` event, then makes the same single streamed call, with the chunks after the
   unchanged system prompt. Unknown markets get a 400, `GET /v1/markets` serves the list, and a
   failed retrieval stops the chat (`knowledge_unavailable`) rather than answering without the
   policy. `market` stays optional until the page sends it (#89), so the live page kept working.
   "I don't know" comes from the prompt, not the score threshold (0.82, which drops only noise):
   on real Gemini, an enchilada recipe and a Spanish mortgage got "No tengo esa información" plus
   the offer of an agent, even with weak sources attached.
10. **The page** ([#89]): a country selector built from `GET /v1/markets`, with no default: it
    asks before the first question and remembers the choice. Each answer shows its sources under
    it, collapsed, with the first doc in the summary, so a greeting's weak matches stay out of the
    way. The CSP didn't change.
11. **Where each answer's wait went** ([#109], [#110]): the page starts the bot check while the
    visitor types instead of on send, and reports how long it still waited (`X-Client-Timing`),
    which the API puts on the request span as `client.check_ms`. One trace now covers the click to
    the first token, including the part that happens before the request exists.
12. **`market` required** ([#113]): only once the page sending it was live, since the deploy
    updates the API a minute before the page and open tabs keep the old script. A message without
    one gets `400 market_required`, after the token and bot checks, so their 401 and 403 still
    come first.

**Measured along the way**

| What | Value |
| --- | --- |
| Image, uncompressed / compressed | 247 MB → **594 MB** / **222 MB** with e5 |
| Model load on Cloud Run | **4.3 s** at startup (logged as `embedder_loaded`) |
| API memory at idle | ~200 MB → **455 MiB**, so Cloud Run went from 512 to 768 MiB |
| One embedding | ~70 ms on one vCPU |
| Corpus | 28 docs → **84 chunks** (18 per market, 12 for all) |
| Re-index with nothing changed | 0 embedded, 0 deleted, 84 unchanged |
| A Mexican account question | top hit `mx-abrir-cuenta / Identificación`, cosine 0.920 |
| Same question, El Salvador | top hit `sv-abrir-cuenta`, the other market's rules |
| Score spread for good matches | 0.83–0.92, which is why the "I don't know" threshold is calibrated on data |
| Smoke set, real model | 20 of 20 find their doc in the top 4; 19 first, 1 second (a 0.007 near-tie) |
| Lowest correct match vs. highest off-topic question | **0.851** vs. **0.874** ("Dame una receta de enchiladas"): no score threshold separates them |

**The demo,** checked on every deploy: "¿Qué documentos necesito para abrir una cuenta?" asked as
a US and as a Mexican customer. Each stream starts with sources from its own market (or all)
only, then the answer, then `done`. The close-out is [#90].

[v0.0.1]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.0.1
[v0.1.0]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.1.0
[v0.1.1]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.1.1
[v0.2.0]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/releases/tag/v0.2.0

[#1]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/1
[#3]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/3
[#5]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/5
[#7]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/7
[#8]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/8
[#12]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/12
[#13]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/13
[#27]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/27
[#28]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/28
[#30]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/30
[#34]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/34
[#38]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/38
[#39]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/39
[#43]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/43
[#46]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/46
[#47]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/47
[#48]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/48
[#49]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/49
[#52]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/52
[#53]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/53
[#54]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/54
[#56]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/56
[#57]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/57
[#59]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/59
[#60]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/60
[#61]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/61
[#62]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/62
[#66]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/66
[#69]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/69
[#71]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/71
[#76]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/76
[#77]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/77
[#78]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/78
[#86]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/issues/86
[#88]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/issues/88
[#89]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/issues/89
[#90]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/issues/90
[#91]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/91
[#92]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/92
[#93]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/93
[#95]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/95
[#96]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/96
[#97]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/97
[#98]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/98
[#100]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/100
[#109]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/109
[#110]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/110
[#113]: https://github.com/ramirez-ai-labs/spanish-support-chatbot/pull/113
