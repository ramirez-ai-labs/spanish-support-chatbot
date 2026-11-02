"""The knowledge base: markets and their policy docs, read from `knowledge/` (ADR 0007).

Markets are config, so adding a country is data, not code. Each doc is Markdown with front
matter, in the folder of its market (or `all`, for docs that apply everywhere).
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

ALL = "all"  # a doc for every market
FRONT_MATTER_KEYS = {"doc_id", "market", "title", "topic", "effective_date"}


@dataclass(frozen=True)
class Market:
    code: str  # e.g. "MX"; also the folder name under knowledge/docs/
    name: str  # shown in the page's selector
    currency: str


@dataclass(frozen=True)
class Doc:
    doc_id: str
    market: str  # a market code, or ALL
    title: str
    topic: str  # the same topic across markets, so their answers can be compared
    effective_date: dt.date
    body: str  # Markdown, without the front matter


def load_markets(path: Path) -> dict[str, Market]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))["markets"]
    markets = {
        entry["code"]: Market(entry["code"], entry["name"], entry["currency"]) for entry in entries
    }
    if len(markets) != len(entries):
        raise ValueError(f"{path}: a market code is listed twice")
    return markets


def parse_doc(text: str, source: str) -> Doc:
    """Parses one doc; raises ValueError naming `source` when it's malformed."""
    if not text.startswith("---\n"):
        raise ValueError(f"{source}: missing front matter")
    header, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise ValueError(f"{source}: front matter is not closed with ---")
    meta = yaml.safe_load(header) or {}
    if set(meta) != FRONT_MATTER_KEYS:
        raise ValueError(f"{source}: front matter keys must be exactly {sorted(FRONT_MATTER_KEYS)}")
    if not isinstance(meta["effective_date"], dt.date):
        raise ValueError(f"{source}: effective_date must be a date (YYYY-MM-DD)")
    return Doc(
        doc_id=str(meta["doc_id"]),
        market=str(meta["market"]),
        title=str(meta["title"]),
        topic=str(meta["topic"]),
        effective_date=meta["effective_date"],
        body=body.strip() + "\n",
    )


def load_docs(root: Path) -> list[Doc]:
    """Every doc under `root`, in a stable order. A doc's folder must be its market."""
    docs = []
    for path in sorted(root.glob("*/*.md")):
        doc = parse_doc(path.read_text(encoding="utf-8"), str(path))
        if path.parent.name != doc.market or path.stem != doc.doc_id:
            raise ValueError(f"{path}: must be at {doc.market}/{doc.doc_id}.md")
        docs.append(doc)
    return docs
