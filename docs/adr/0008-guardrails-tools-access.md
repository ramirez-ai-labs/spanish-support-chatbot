# 0008. Guardrails, tools and access: redaction before anything reads the text, a capped tool loop, and confirmations the model can't give

- **Status:** Accepted
- **Date:** 2026-11-03
- **Amended:** 2026-11-05: action tools are offered to every signed-in caller, so step-up can be reached; expired sessions are refused on read and swept on create, and only a session's exact owner may use it; the placeholder HMAC's key comes from a master key in the secret store
- **Milestone:** M3 (#51)

## Context

M3 is where the bot first touches customer data: it redacts PII, calls tools on the customer's
own account, takes one confirmed action, and hands off to a person. The roadmap fixes the outline:
Presidio with a Spanish model, tiers from M1, step-up through `auth_time`, an audit log, and a
step cap of 5. These constraints shape the details:

- **No PII reaches the model.** Gemini's free tier may use prompts to improve Google's products
  (design §3.2). Redaction runs right after the token check, before anything else touches the text.
- **Every market has its own IDs.** The live markets are the US, Mexico, El Salvador and Spain;
  Colombia and Peru come later. Customers type their IDs in many shapes.
- **The hot path stays one model call** for questions that need no tool, with no router model,
  thinking at minimal and a stable system prompt.
- **The model is not a security boundary.** A message, a retrieved chunk or a tool result can
  carry instructions (prompt injection), so the model may ask for anything. Every guarantee has to
  hold even if the model is fooled.
- **Budgets.** The image is 222 MB compressed, and the registry's free tier (0.5 GB) holds the
  live image and its rollback. The API uses 455 MiB at idle with e5, under a 768 MiB limit.
- **The tier rule already exists** (`core/identity.py`, `core/tools.py`, M1). A tool's kind sets
  its tier, and `ToolProvider.call` takes `confirmed`.

**What checking the libraries found.** Presidio 2.2.366 and spaCy 3.8, run on synthetic Spanish
text on the dev machine (Windows). #51 measures again in the Linux image.

| Finding | Detail |
| --- | --- |
| Model size | `es_core_news_sm` 12 MB, `md` 40 MB, `lg` 542 MB (wheels) |
| Names | `sm` missed both test names and tagged «Hola» as a place; `md` caught both |
| Built in for `es` | Card numbers (Luhn), email, phone, IBAN, Spain's NIF and NIE |
| **Not caught in `es`** | US SSN (Presidio's recognizer is English-only), Mexico's CURP and CLABE, El Salvador's DUI (only as a phone number at score 0.40), Peru's DNI, Colombia's cédula |
| Places | «El Salvador», «México», «Los Ángeles» are tagged LOCATION |
| Speed | ~7 ms a message with `md` |
| Memory | **+310 MB** loaded: ~78 MB of imports, ~235 MB for spaCy and the model; dropping the parser saves nothing |
| Disk | `md` 52 MB, spaCy and its libraries ~120 MB, `phonenumbers` 21 MB |
| **A network call** | On first use, `tldextract` (used to check emails and URLs) downloads the Public Suffix List: 2.8 s on the first message, and an outbound call from the hot path |

## Decision

### Redaction

- **Engine:** `presidio-analyzer` with `es_core_news_md` 3.8.0, both pinned and baked into the
  image (#51), with Spanish as the only language.
- **Where it runs:** after the token and bot checks (they don't read the text), and before
  retrieval, the session write and the model.
  - The query is embedded from the **redacted** text: the vector goes to Neon, and a vector of
    the raw text is derived from the PII.
  - The session load reads no text, so it runs in parallel with redaction.
  - This changes the roadmap's "redaction, query embedding and session load together" to
    "redaction and session load together, then embedding". It costs about 7 ms.
- **What is redacted** (an allowlist of entity types, so a new Presidio recognizer can't change it
  silently):

  | Type | How |
  | --- | --- |
  | `PERSON` | spaCy NER, with runs of capitals read a second time in normal casing, since the NER misses them |
  | `EMAIL_ADDRESS`, `CREDIT_CARD` (Luhn), `IBAN_CODE`, `ES_NIF`, `ES_NIE` | Presidio, built in. Ours adds cards with dots or slashes, also checked by Luhn |
  | `PHONE_NUMBER` | Presidio, with regions US, MX, SV and ES |
  | `US_SSN`, `US_ITIN` | Presidio's recognizers, registered for Spanish with Spanish context words («seguro social», «ITIN»). Ours adds an SSN as 9 bare digits with no context, in any range that was ever issued |
  | `MX_CURP` | Ours: the 18-character format and its check digit |
  | `MX_CLABE` | Ours: 18 digits, optionally separated, and their check digit |
  | `SV_DUI` | Ours: 8 digits, an optional hyphen or space and the check digit |

  Colombia's cédula and Peru's DNI arrive with their markets. Their formats are weak (Peru has an
  optional check character, Colombia none), so they need context words («cédula», «DNI»).
- **Every recognizer runs on every message,** not just the selected market's: a Mexican customer in
  the US market can type a CURP.
- **Not redacted:** `LOCATION`, `ORGANIZATION`, `DATE_TIME`, `NRP` and `URL`. Countries, cities,
  dates and amounts are what policy questions are about («enviar 500 dólares a El Salvador»).
  Leaving `URL` out also removes its false hits inside email addresses.
- **Score threshold 0.4,** where phone numbers land without context. Over-redacting a number costs
  an answer that's a little less specific; under-redacting sends PII to the model.
  - So any 9-digit number in a range an SSN can use is redacted, even in an answer: it has no check
    digit to tell it from a reference number.
- **Plain text first.** Before detection, the text loses what can hide a value from the patterns:
  other scripts' digits and fullwidth forms become ASCII, unusual spaces and hyphens become plain
  ones, and invisible characters go. Detection and everything after it see this text, and so does
  the output scan. It isn't NFKC, which would turn «1.º» into «1.o».
- **Words that aren't names.** The NER tags some banking words and greetings as people («Tarjeta de
  Crédito», «hola»). A `PERSON` span that's only one of a short list stays, so the question and its
  query vector keep the word. A span with a name in it («Hola Ana») still goes. The list holds only
  words that aren't also surnames, and a contract test runs the smoke questions and doc headings
  through the real NER.
- **Known limits:**
  - Values spelled out («cinco tres seis…», «maria arroba example punto com») aren't caught. M4's
    PII set measures them.
  - A message all in capitals can lose an extra phrase to `<PERSON_n>`, since the NER reads the
    re-cased words less reliably. That's the cheap side of the tradeoff above.
- **Placeholders:** `<TYPE_n>`, such as `<PERSON_1>` or `<MX_CURP_1>`, numbered per
  conversation. The same value gets the same placeholder on every turn, so a conversation about
  `<PERSON_1>` stays coherent.
  - Numbering uses an HMAC-SHA256 of the normalized value. The session stores the HMAC and its
    number, never the value.
  - The HMAC's key is never stored. Each session's key is derived from one master key in the
    cloud's secret store (Secret Manager, later Key Vault and AWS Secrets Manager) and the session
    ID. A key kept beside the HMACs would let anyone reading the store test guesses, and a 9-digit
    SSN has few enough values to try them all.
  - The master key can be rotated at any time. It costs coherence, not safety: a value seen before
    gets a new number, and the session's counters keep two values from sharing one.
  - **Placeholders are never filled back in.** That would need the values stored, and the page
    already shows what the customer typed. The system prompt says placeholders are protected data
    and not to repeat them. Any that reach the answer become «[dato protegido]».
- **Model output is scanned too,** with the pattern recognizers only (IDs, cards, emails, phones).
  - The stream holds back its last 64 characters, so a number split across two chunks is still
    caught before it reaches the page.
  - NER isn't run on output: the model never sees a name, and NER on half a sentence is unreliable.
- **No network at runtime.** `tldextract` uses its bundled suffix-list snapshot, with fetching off,
  and a test runs the analyzer with sockets blocked.
- **Fail closed.** If the analyzer raises, the message is refused with
  `503 guardrails_unavailable`. Unredacted text never goes further.
- **Telemetry:** a `chat.redaction` span with counts by type and stage (`input` or `output`), and
  one log line such as `pii_redacted stage=input types=PERSON:1,EMAIL_ADDRESS:1`. Never the values.
- **Ports and code layout:**
  - A `PIIDetector` port returns spans (type, start, end, score). The core owns everything else:
    the entity allowlist, placeholder numbering, the output window and the check-digit functions.
    That keeps it cloud-free and unit-tested.
  - The Presidio adapter lives in `adapters/shared/presidio.py`.
  - Its contract suite covers each market's synthetic IDs: caught with a valid check digit, not
    caught with a wrong one. It also checks that names are caught, and that places, dates and
    amounts survive.

### Tools and the agent loop

- **Anonymous callers keep today's path:** no tools are offered, so it's one streamed call.
- **Signed-in callers:**
  - The same single call also carries the declarations from `list_tools(caller)`: every tool,
    action tools included, whether or not the caller has step-up right now.
  - If the model answers in text, it streams as now: still one call and no router.
  - If it asks for a read-only tool, the loop runs: check, call, send the result back, call the
    model again.
  - If it asks for an action tool, nothing runs, and the turn goes to confirmation (below).
- **Caps:**
  - 5 model calls per message (the step cap) and 3 tool calls per step.
  - A per-conversation input-token cap, which hands off when reached.
  - Each model call writes its own `llm_usage` row, with `tools` and `tool_results` in
    `prompt_parts`.
- **The core owns the loop.** The SDK's automatic function calling is off, so `check_allowed` runs
  before any tool does. Gemini returns opaque thought signatures with its function calls. The adapter
  returns the model's turn as an opaque object, and the core passes it back unchanged.
- **Port change:** `LLMProvider.stream` takes tools and history, and yields text deltas or
  `ToolCall(name, arguments)`. The mock LLM scripts tool calls, so tests and CI never call Gemini.
- **The three tools** are `check_card_status` (read-only), `block_card` and `open_ticket` (actions).
  - They work on synthetic data in a Neon `bank` schema, filled by a seed script. Ownership is
    checked against `sub`, and results are masked («tarjeta terminada en 4417»).
  - The app role can read cards and execute one database function per action. It never updates a
    table directly.
- **Tool results are untrusted input:**
  - They go back to the model as function responses, never into the system prompt.
  - Tool descriptions are fixed strings in code.
  - Results pass the output pattern scan.

### Confirmation and step-up

- **Being offered a tool isn't permission to run it.** Step-up lasts 5 minutes, so most signed-in
  customers don't have it when they ask to block a card. If the model weren't offered the tool, it
  couldn't propose the action, and the customer would never reach step-up. So any signed-in caller
  can propose an action, and anonymous callers still can't. Step-up is enforced only where the
  action runs: at confirm time.
- **The model proposes; it never confirms.** An action tool call doesn't run.
  - Instead, the API stores a pending action: a random 128-bit ID, the `sub`, the tool and its
    exact arguments, expiring in 5 minutes.
  - Ownership is checked now as well as at run time, so a proposal for someone else's card stores
    nothing and shows nothing. The check reads no more than `check_card_status` already shows a
    signed-in caller.
  - It then ends the turn with `event: confirm`. The summary is written by the server from the
    tool's own template («¿Bloquear la tarjeta terminada en 4417?»), not by the model.
- **Confirming is a button,** not a chat message. The page sends `POST /v1/actions/{id}` with
  `{"decision": "confirm" | "decline"}`, a fresh Turnstile token and the bearer token. Typing «sí»
  confirms nothing, so neither prompt injection nor a model mistake can produce a confirmation.
- **The API checks again at confirm time:**
  - the same `sub`
  - not expired, and not used before (single use)
  - the step-up tier **now**: `auth_time` within 5 minutes of this request
- **Without step-up,** the API answers `403 step_up_required`. The page sends a fresh sign-in link
  (email, as today), and back on the page, the same pending action can be confirmed while it hasn't
  expired. Refreshing a token keeps the original `auth_time`, so a refresh can't extend step-up.
- **Step-up method:** signing in again by email link. There's no SMS (a project constraint), and
  TOTP is left for later if it's needed.
- **The result message is server-written** from the tool's result, with no second model call.

### Audit

- **Table:** `audit_log` in Neon, written by an `audit_writer` role that can only INSERT (like
  `usage_writer`). No app role can UPDATE or DELETE it.
- **One row per state change:** `proposed`, `confirmed`, `declined`, `expired`, `denied` (with a
  reason code), `executed` or `failed`.
- **Columns:** `created_at` (database clock), `action_id`, `actor_sub`, `actor_tier`, `auth_time`,
  `tool`, `target` (masked, such as the card's last 4 digits), `arguments_sha256`, `decision`,
  `reason`, `cloud` and `trace_id`. No message text and no full numbers.
- **It fails closed.** The `confirmed` row is written before the tool runs; if that write fails, the
  action doesn't run (`503`). Usage rows are the opposite: they never fail a chat.

### Sessions

Placeholders, repeated questions and pending actions all need them.

- **Store:** Firestore, behind a `SessionStore` port with a contract suite, so Cosmos DB and
  DynamoDB can follow in M7 and M8.
- **IDs:** random and 128-bit, returned to the page.
  - A signed-in session is bound to its `sub` and refused to any other `sub`; it lasts 30 days.
  - An anonymous session has no owner, is refused to signed-in callers and lasts 1 day. Signing in
    starts a new session: continuing an anonymous one would let whoever knew its ID write into a
    customer's history, which reaches the model. The cost is that the model forgets what was asked
    before signing in.
- **Contents:** only redacted text. The model gets the last 6 turns.
- **Saves are version-checked.** A save applies only if the session is unchanged since it was
  loaded, so a stale request can't overwrite a newer turn. Firestore, Cosmos DB and DynamoDB can
  all enforce this.
- **Expiry:** Firestore's TTL policies have no free usage, so the store doesn't delete on a timer.
  - An expired session is refused on read, and the contract suite proves it for every adapter.
    That is the guarantee; deletion only reclaims space.
  - Saving a new session also deletes up to 5 expired ones. Deleting more than are created keeps
    the store from growing, stays inside the free deletes (20k a day) and needs no scheduler. A
    failed sweep costs nothing, since the next one retries.

### Handoff

- **No model call decides it.** The triggers are cheap and deterministic:
  1. the customer asks for a person («agente», «persona», «humano», «asesor»)
  2. a repeated question: the query vector, which retrieval already computed, has cosine ≥ 0.95
     with one of the last 3 questions
  3. frustration: two matches in a conversation from a short Spanish list («ya te dije»,
     «no sirve», «no me entiendes»), or a message mostly in capitals or ending in «!!!»
  4. low confidence: retrieval finds nothing above the threshold twice in a conversation
  5. the per-conversation token cap
- **What happens:** the answer ends with `event: handoff`, offering a person.
  - Accepting puts a ticket in the staff queue, in Firestore: the redacted transcript and the
    reason code.
  - Only signed-in customers get a ticket, since it needs an owner to reply to. Anonymous visitors
    are asked to sign in first.
  - Staff read the queue with the Firebase custom claim `staff`, set by a script, never
    self-service.
- **Calibration:** thresholds and word lists are config, calibrated by the M4 eval set. The trigger
  goes on the span as `chat.handoff_reason`.

## Alternatives considered

- **`es_core_news_sm`.** 12 MB, but it missed both names in the check and called «Hola» a place.
- **`es_core_news_lg`.** 542 MB, more than the registry's free tier on its own.
- **A transformer NER** (spaCy `trf`, a GLiNER PII model). Better recall, but hundreds of MB and
  slower on one CPU. The M4 PII eval decides whether `md` misses too much.
- **Regex only, no NER.** It catches IDs but not names.
- **Asking the model to ignore PII, or relying on the provider's filters.** By then the PII has
  already reached the model.
- **Redacting places and dates too.** That breaks the policy questions themselves.
- **Filling placeholders back into the answer.** It needs the values stored, for no gain: the
  customer already sees what they typed.
- **Confirming with a typed «sí».** That's text the model or an injected message can also produce.
- **Offering action tools only at step-up,** as `list_tools` first did. It shows the model less, but
  a customer past the 5-minute window gets no proposal and no way to step up. A prompt instruction
  or a `request_step_up` tool could send them to sign in again, but the model would have to get that
  right, and the action's exact arguments would be lost, so the customer would have to ask again.
- **A router model** for tools and handoff. A second call on every message.
- **Audit in Cloud Logging.** It differs per cloud, isn't append-only by grant, and logs can be
  excluded or expire. One shared table matches the three-cloud design and `llm_usage`.

## Consequences

- **The image grows by about 150 MB uncompressed.** The registry must still hold the live image
  and its rollback under 0.5 GB, so the dependency layer must be shared by both. #51 measures it.
- **Cloud Run goes from 768 MiB to 1 GiB.** 455 MiB plus 310 MB leaves no headroom under 768.
  - The free tier's 360,000 GiB-seconds a month is 100 hours of request time at 1 GiB: plenty for
    the demo.
  - The M5 load test counts it.
- **Cold starts get about 1.4 s longer** (the model load). #51 measures it on Cloud Run, and M5
  decides on warm-up.
- **Embedding waits for redaction** (~7 ms), which changes the roadmap's parallel pre-work line.
- **The `LLMProvider` port changes** (tools, history, `ToolCall`). Gemini, the mock and the
  contract suite all change with it.
- **`ToolProvider` separates offering from running,** in the same slice as the agent loop.
  - `list_tools` returns action tools to every signed-in caller. Its docstring and the contract
    test that expects one tool at the signed-in tier change with it.
  - `check_allowed` stops relying on `visible_to` and checks the tier itself, so `call` still
    refuses an action without step-up and confirmation. Its tests stay as they are.
- **The model sees tools the caller can't run yet, so a fooled model can propose an action.** That
  costs a proposal, not an action: the customer sees the server's summary and must still press the
  button with step-up. Proposals also add pending actions and audit rows, which the step cap and
  M5's per-user limits bound.
- **New database pieces,** each as a migration applied before the code that uses it:
  - the `audit_writer` role
  - the `bank` schema
  - `app_gcp`'s read and execute grants on it
- **Redaction quality is measured, not assumed.** M4 adds a synthetic PII set per market and
  counts misses and false redactions.
- **Some reference numbers will be redacted as phone numbers.** That's accepted, and M4 measures how
  often.
