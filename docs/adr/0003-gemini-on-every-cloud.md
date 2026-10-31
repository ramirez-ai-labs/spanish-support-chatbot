# 0003. Gemini free tier on every cloud, Flash-Lite by default

- **Status:** Accepted
- **Date:** 2026-10-31
- **Milestone:** M0 (default model), M4 (bakeoff revisits)

## Context

The bot must run at $0 on three clouds, answer in regional Spanish, and stream quickly. Each cloud
has its own model service (Vertex AI, Azure OpenAI, Bedrock), all billed per token. Using a
different model per cloud would also make quality and cost comparisons between clouds
meaningless.

## Decision

- Gemini through the AI Studio free tier on every cloud, behind the `LLMProvider` port.
- The API key lives in a dedicated GCP project with **no billing account**, so the key cannot
  incur charges; at the free-tier limit it returns 429 instead of billing. The key value is
  stored once in each cloud's secret store.
- Default model `gemini-3.5-flash-lite` with thinking at `minimal` on the hot path. Measured on
  the free tier with the same Spanish question (M0):

  | Model | Time to first token | Thinking tokens |
  | --- | --- | --- |
  | `gemini-2.5-flash` | n/a: closed to new API keys | – |
  | `gemini-3.8-flash`, thinking `low` | 67 s | 296 |
  | `gemini-3.8-flash`, `thinking_budget=0` (ignored on Gemini 3) | 97 s | 268 |
  | `gemini-3.5-flash-lite`, thinking `minimal` | 0.5 s | 0 |

- `GEMINI_MODEL` overrides the default. Tests and CI use the mock LLM and never call Gemini.

## Alternatives considered

- **Each cloud's native model service:** the most native showcase, but paid, and model
  differences would mask cloud differences.
- **A larger default model:** better reasoning, but free-tier latency on Flash was over a minute
  to first token, far past the 3-second target.

## Consequences

- One quality bar and one prompt set across clouds; the M4 eval gate covers them all.
- Free-tier rate limits apply: retry with backoff on 429 (M5), and Flash-Lite as the fallback.
- Free-tier prompts may be used by Google to improve its products, which is acceptable only
  because the project uses synthetic data exclusively.
- Models leave the free tier or close to new keys without notice (2.5 Flash already has); the
  adapter isolates that, and the M4 bakeoff reruns when models change.
