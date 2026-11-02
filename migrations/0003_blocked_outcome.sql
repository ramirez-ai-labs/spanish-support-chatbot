-- 0003: record safety blocks (content_blocked): a new outcome, and which categories triggered it.
alter table llm_usage drop constraint llm_usage_outcome_check;
alter table llm_usage add constraint llm_usage_outcome_check
    check (outcome in ('done', 'error', 'incomplete', 'blocked'));
alter table llm_usage add column blocked_categories text[] not null default '{}';
