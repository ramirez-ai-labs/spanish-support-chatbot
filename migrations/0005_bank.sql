-- 0005: the synthetic bank behind the customer tools (ADR 0008): cards, read by check_card_status.
-- Synthetic data only, filled by migrations/seed_bank.py. A card is stored by its last 4 digits and
-- never by its full number, so no full number exists to leak: the tools can only show «terminada
-- en 4417». The action tools' functions (block_card, open_ticket) come with them, in a later file.

create schema bank;

create table bank.cards (
    id uuid primary key default gen_random_uuid(),
    owner_sub text not null check (length(owner_sub) between 1 and 128), -- the identity token's sub
    market text not null check (market ~ '^[A-Z]{2}$'), -- a code from knowledge/markets.yaml
    kind text not null check (kind in ('debit', 'credit')),
    last4 text not null check (last4 ~ '^[0-9]{4}$'),
    status text not null default 'active' check (status in ('active', 'blocked')),
    status_changed_at timestamptz not null default now(),
    expires_on date not null, -- the last day it can be used; past it, the tool says it's expired
    created_at timestamptz not null default now(),
    -- How a customer and the tools name a card: one owner's last 4 digits are never ambiguous.
    unique (owner_sub, last4)
);

-- The API on GCP reads cards to answer check_card_status, and checks ownership against the caller's
-- sub in code. It never writes this table: block_card will run as a function that it can execute.
grant usage on schema bank to app_gcp;
grant select on bank.cards to app_gcp;
