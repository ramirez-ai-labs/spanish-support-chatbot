# 0002. One shared Neon pgvector database for all clouds

- **Status:** Accepted
- **Date:** 2026-10-31
- **Milestone:** M1 (database), M2 (retrieval)

## Context

Every cloud build must answer from the same policy documents with the same citations, so answers
can be compared across clouds. The project must cost $0 to run. Each cloud's native vector option
(AlloyDB or Vertex AI Vector Search, Azure AI Search or Cosmos DB vector, OpenSearch or Aurora) has
no always-free tier that fits, and running three copies triples indexing and drift.

## Decision

One Neon Postgres project with `pgvector`, in AWS US East 1, shared by all clouds:

- One indexing pipeline writes it; apps only read.
- A least-privilege role per consumer: `indexer`, `app_gcp`, `usage_writer` in M1, then
  `app_azure` (M7) and `app_aws` (M8).
- Every query filters by market; the market list is data, not code.
- The same database holds `llm_usage` and `model_prices`, which makes the cross-cloud cost
  dashboard a single query.

## Alternatives considered

- **Native vector store per cloud:** the most "native" showcase, but no free tier, three indexes
  to keep in sync, and answers that can differ for reasons unrelated to the cloud.
- **Vectors in each app container (e.g., FAISS file):** free and fast, but reindexing means
  redeploying, and there is no shared usage table.

## Consequences

- Cross-cloud latency: GCP us-east4 and Azure East US reach AWS us-east-1 in a few milliseconds,
  but every retrieval is a network call. It is measured per stage in M5.
- Neon's free tier limits compute and connections, and three apps share them. Mitigations: pooled
  connections, warm-up pings, pausing unused branches.
- One database is a shared failure point; acceptable for a demo, and the M6 design doc describes
  the production alternative.
