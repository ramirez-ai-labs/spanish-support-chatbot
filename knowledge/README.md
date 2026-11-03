# Knowledge base

Policy and FAQ docs for a **fictional bank**. Everything here is synthetic: the bank, its fees,
limits and rules are invented for the demo, and no doc contains a real account, card or ID number.
Some docs name real public institutions or payment systems (a regulator, SPEI, SEPA), only so the
answers read like each market's.

- `markets.yaml` lists the markets. Adding a country is data, not code.
- `docs/<market>/<doc_id>.md` is one doc, with `docs/all/` for docs that apply in every market.
- Front matter has exactly `doc_id`, `market`, `title`, `topic` and `effective_date`. Docs on the
  same `topic` in different markets are meant to differ; the market filter (ADR 0007) is tested on
  those differences.
- Sections are `##` headings: the indexer makes one chunk per section, so keep each section about
  one rule.

- `smoke.yaml` holds fixed questions per market and the doc each must find. The indexing workflow
  asks them before promoting the index to `main` (#86), and CI asks them on every PR. If a doc
  change breaks one, fix the doc (often its headings: use the words customers use) before
  changing the question.

`tests/unit/test_knowledge.py` checks every doc on every PR, and `tests/unit/test_index_checks.py`
checks the smoke set.
