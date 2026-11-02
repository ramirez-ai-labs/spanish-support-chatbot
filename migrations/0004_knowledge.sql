-- 0004: the knowledge store for retrieval (ADR 0007, #83): policy chunks with e5 embeddings, and
-- one least-privilege role per consumer. Roles are created with SQL, so they get only these grants.

-- The newest pgvector Neon offers: iterative index scans, which keep a market filter from
-- returning fewer chunks than asked for, need 0.8.0.
alter extension vector update;

create table chunks (
    id bigint generated always as identity primary key,
    doc_id text not null,
    market text not null check (market ~ '^([A-Z]{2}|all)$'), -- a code from knowledge/markets.yaml, or all
    title text not null,
    heading_path text[] not null default '{}',
    content text not null,
    content_hash text not null unique, -- market, doc, headings, text and model: unchanged chunks aren't re-embedded
    embedding vector(384) not null, -- multilingual-e5-small, L2-normalized
    embedding_model text not null, -- vectors are only ever compared with the same model's
    updated_at timestamptz not null default now()
);

create index chunks_market on chunks (market);
create index chunks_doc_id on chunks (doc_id);

-- HNSW only where queries can use iterative scans; otherwise an exact scan, which at this size
-- takes about a millisecond.
do $$
declare
    pgvector text := (select extversion from pg_extension where extname = 'vector');
begin
    if string_to_array(pgvector, '.')::int[] >= array[0, 8, 0] then
        create index chunks_embedding_hnsw on chunks
            using hnsw (embedding vector_cosine_ops) with (m = 16, ef_construction = 64);
    else
        raise notice 'pgvector % is older than 0.8.0: no HNSW index, exact scans (ADR 0007)', pgvector;
    end if;
end
$$;

-- The indexing job: keeps chunks in step with knowledge/, and nothing else.
create role indexer nologin;
grant usage on schema public to indexer;
grant select, insert, update, delete on chunks to indexer;

-- The API on GCP: reads chunks to answer, and nothing else.
create role app_gcp nologin;
grant usage on schema public to app_gcp;
grant select on chunks to app_gcp;
