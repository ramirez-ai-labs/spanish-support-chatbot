# 0007. Retrieval: multilingual e5 in the container, heading chunks, and a market filter the port enforces

- **Status:** Accepted
- **Date:** 2026-11-01
- **Milestone:** M2 (#81)

## Context

M2 makes answers come from each market's policy docs, with sources. The roadmap already fixes the
outline: e5-small in the container, pgvector in the shared Neon database
([ADR 0002](0002-shared-neon-knowledge-layer.md)), chunks by heading, and a market filter on every
query. These constraints shape the details:

- **Spanish everywhere.** Questions and docs are Spanish, from six markets whose policies differ
  (fees, card blocking, ID types).
- **One model call on the hot path, and $0 to run.** An embedding API call per question would add
  latency, a quota and a dependency on every cloud.
- **The image stays small.** It's 247 MB today against a 300 MB budget. Artifact Registry is free
  only up to 0.5 GB, and it keeps the last three versions.
- **The wrong market's answer is the worst failure.** A Mexican customer told the US fee is worse
  than "I don't know".
- **CI never holds the owner role or the Neon API key** ([ADR 0006](0006-neon-outside-terraform.md)).

## Decision

**Embeddings**
- **Model:** `intfloat/multilingual-e5-small`, quantized to int8 ONNX. The `model_quantized.onnx`
  export is 118 MB, has 384 dimensions and reads up to 512 tokens. The plain `e5-small-v2` is
  English-only.
- **Runtime:** `onnxruntime` and `tokenizers`, with no PyTorch. Vectors use mean pooling, then L2
  normalization.
- **Pinned files:** the model is pinned by Hugging Face revision and SHA-256, and baked into the
  image at build time (#50).
- **One adapter for both jobs.** The indexer and the API use the same adapter, with `passage:` when
  indexing and `query:` at query time. Each chunk records the model ID, so a different model can't
  be queried against old vectors without re-indexing.

**Chunks**
- **One chunk per heading section** (`##` or `###`), with the heading path kept as metadata.
- **Long sections:** a section over about 350 tokens is split at paragraph boundaries, and the next
  piece repeats the previous paragraph. The target is 150 to 350 tokens.
- **Text embedded:** `passage: {title} — {heading path}\n\n{text}`. The title gives a short section
  its context.
- **Content hash:** covers the market, `doc_id`, heading path, text and model ID. An unchanged chunk
  is never re-embedded, and a model change re-embeds everything.

**Store and index**
- **The `chunks` table:** `doc_id`, `market`, `title`, `heading_path`, `content`, `content_hash`
  (unique), `embedding vector(384)`, `embedding_model` and `updated_at`. It has a btree index on
  `market` (#83).
- **HNSW index:** cosine distance, with pgvector's defaults `m = 16` and `ef_construction = 64`.
  These are plenty for under a thousand chunks.
- **Iterative scans:** each query sets `hnsw.iterative_scan = relaxed_order` and `ef_search = 40`,
  then re-sorts by distance. pgvector filters *after* the index scan, so without this a market
  filter can quietly return fewer chunks than asked for. The problem grows as markets are added.
- **Version check:** iterative scans need pgvector 0.8.0 or later. #83 checks Neon's version. If
  it's older, there is no HNSW index and queries use an exact scan, which costs about a
  millisecond at this size, until Neon upgrades.

**Market filter**
- **The port enforces it, not the caller.** `KnowledgeStore.search(query, market, k)` requires a
  market from `knowledge/markets.yaml` and refuses anything else. It matches
  `market = any([market, 'all'])`, and the contract suite proves no chunk ever comes from another
  market (#87).
- **Where the market comes from:** in M2, a selector on the page. The page asks for a market
  before the first question, rather than defaulting to the US, and remembers the choice. From M3,
  a signed-in user's profile market takes over.
- **It's a correctness boundary, not a security one.** Policy docs are public. Customer data is
  isolated by `sub` in M3.

**Retrieval and the answer**
- **Context:** the 4 nearest chunks, keeping only those above a minimum similarity. e5's cosine
  scores bunch together, so the threshold is calibrated on the smoke set in #86 and kept in
  config, not guessed here.
- **Still one model call**, with no router. The stable system prompt comes first, for caching.
  Then come the numbered sources, then the question. The prompt tells the model to answer only
  from the sources, and to say it doesn't know and offer a person when they don't cover the
  question. With no chunk above the threshold, the model gets no sources and does exactly that,
  which also keeps greetings natural.
- **Citations:** an `event: sources` arrives before the first delta. It lists the title, section
  and market of each chunk sent to the model, removing duplicates, with no chunk IDs or scores.

**Indexing and promotion**
- **Two branches:** the Neon `dev` branch is created once from `main` by the bootstrap script,
  after the roles exist, so the roles carry over. Migrations are applied to both branches.
- **The workflow:** a GitHub Actions job runs on pushes to `main` that touch `knowledge/` or
  `indexing/`. It indexes `dev`, checks it, then indexes `main` with the same commit.
  - The checks are per-market chunk counts, no chunk without a market, and the retrieval smoke
    set (#86).
  - Indexing is idempotent and deletes the chunks of removed docs, so `dev` converges without a
    branch reset, which would need the Neon API key.
- **Credentials:** the job logs in keylessly through Workload Identity Federation, as a new
  `indexer-ci` service account (bootstrap stack). It can read only the two `indexer` connection
  secrets, one per branch, and is trusted only for that workflow on `main` (#84). The `indexer`
  role can write only `chunks`.
- **No human approval for content:** docs are reviewed in their PR, and the checks gate
  promotion.

**Markets:** US, Mexico, and **Colombia** as the third, from the market list in CLAUDE.md.

## Alternatives considered

- **`e5-small-v2` (English).** It's smaller, but it's trained on English and would rank Spanish
  queries poorly.
- **A larger multilingual model (bge-m3, e5-base).** Better recall, but several times the image
  size and memory, outside the free registry and the 512 MiB instance. The M4 eval decides if
  small isn't good enough.
- **An embedding API (Gemini or a cloud's own).** No model in the image, but every question
  would make a second network call against a second quota, and the embeddings would tie the
  index to one provider.
- **Exact scan only, no HNSW.** Fast enough at this size. HNSW is kept because the roadmap calls
  for it and the filtered-recall problem is better solved now than found at scale. Exact scan
  remains the fallback.
- **One table or partial index per market.** Simple filtering, but adding a country would mean a
  migration. Markets are data.
- **Fixed-size token chunks.** Easy, but they cut policy rules mid-sentence and lose the heading
  that says which product a rule is about.
- **The indexer URL as a GitHub Actions secret.** Simpler, but a long-lived credential in GitHub,
  where everything else logs in keylessly.
- **Skipping the model when nothing matches,** with a fixed reply. Zero tokens, but it answers
  "hola" with "I don't know", and it would split the hot path into two behaviors to test.

## Consequences

- **The image grows by about 140 MB,** to roughly 390 MB: the model plus `onnxruntime` and
  `tokenizers`. #50 raises the size budget to what it measures.
- **The registry must stay under 0.5 GB with three versions,** so the model layer has to be
  byte-identical between builds and shared by every version: a pinned file, fixed timestamps, and
  its own early layer. #50 checks the registry's total size.
- **Cloud Run memory may need 1 GiB** instead of 512 MiB. #50 measures it; 1 GiB is still free at
  demo traffic.
- **The model loads at startup,** so cold starts get slower. #50 measures that too; M5 decides on
  warm-up pings.
- **Two branches to migrate.** `migrations/migrate.py` runs against `dev` and `main`, and
  `infra/BOOTSTRAP.md` says so.
- **The page shows the sources the model was given, not necessarily the ones it used.** The M4
  groundedness eval measures the gap.
- **Changing the embedding model means re-indexing,** which the content hash does on its own.
