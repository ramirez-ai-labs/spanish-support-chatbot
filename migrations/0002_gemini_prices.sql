-- 0002: Gemini 3.5 Flash-Lite, Standard paid tier, USD per 1M tokens. Thinking is billed as output.
-- Source: https://ai.google.dev/gemini-api/docs/pricing (page last updated 2026-10-30).
insert into model_prices (
    model, effective_from, input_per_mtok, cached_input_per_mtok, output_per_mtok,
    thinking_per_mtok, source
) values (
    'gemini-3.5-flash-lite', '2026-10-30', 0.30, 0.03, 2.50, 2.50,
    'https://ai.google.dev/gemini-api/docs/pricing (updated 2026-10-30)'
);
