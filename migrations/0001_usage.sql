-- 0001: LLM usage events and dated model prices (#20).
-- Cost is never stored: llm_usage_cost computes it at query time from the price in effect that day.

create extension if not exists vector; -- pgvector, for retrieval from M2

create table llm_usage (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    cloud text not null, -- gcp, azure, aws or local
    model text not null,
    purpose text not null check (purpose in ('chat', 'eval', 'index')),
    prompt_parts text[] not null default '{}', -- the names of the prompt's parts, never their text
    input_tokens integer not null check (input_tokens >= 0), -- includes cached_tokens
    cached_tokens integer not null default 0 check (cached_tokens between 0 and input_tokens),
    output_tokens integer not null check (output_tokens >= 0), -- visible answer, without thinking
    thinking_tokens integer not null default 0 check (thinking_tokens >= 0),
    latency_ms integer not null check (latency_ms >= 0),
    ttft_ms integer check (ttft_ms >= 0),
    outcome text not null check (outcome in ('done', 'error', 'incomplete')),
    trace_id text -- links the row to its trace in Cloud Trace
);

create index llm_usage_created_at on llm_usage (created_at);

-- List prices per 1M tokens, one row per model and date. A price change is a new row.
create table model_prices (
    model text not null,
    effective_from date not null,
    input_per_mtok numeric(12, 6) not null,
    cached_input_per_mtok numeric(12, 6) not null,
    output_per_mtok numeric(12, 6) not null,
    thinking_per_mtok numeric(12, 6) not null,
    source text not null,
    primary key (model, effective_from)
);

-- Shadow cost: what each call would cost at the paid list price in effect on its day. It shows
-- production cost even while the free tier bills $0. Models without a price get null.
create view llm_usage_cost as
select
    u.*,
    p.effective_from as price_date,
    (
        (u.input_tokens - u.cached_tokens) * p.input_per_mtok
        + u.cached_tokens * p.cached_input_per_mtok
        + u.output_tokens * p.output_per_mtok
        + u.thinking_tokens * p.thinking_per_mtok
    ) / 1000000 as shadow_cost_usd
from llm_usage as u
left join lateral (
    select *
    from model_prices as mp
    where mp.model = u.model and mp.effective_from <= u.created_at::date
    order by mp.effective_from desc
    limit 1
) as p on true;

-- The API's role for usage rows: INSERT only, so it can't read or change any row (ADR 0006).
-- Created with SQL, so it is not a neon_superuser member. Login and password are set by
-- infra/scripts/neon-role-password.sh, which stores its connection string in Secret Manager.
create role usage_writer nologin;
grant usage on schema public to usage_writer;
grant insert on llm_usage to usage_writer;
