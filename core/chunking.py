"""Splits knowledge docs into retrieval chunks (ADR 0007).

One chunk per `##` section (and `###` under it), keeping the heading path, because each section
holds one rule. A section longer than MAX_WORDS is split at paragraph boundaries, and each piece
after the first repeats the previous paragraph, so a rule that spans two paragraphs is still whole
in one of them. The embedded text starts with the doc's title and the headings, so a short section
still says which product and market it is about.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from core.knowledge import Doc

MAX_WORDS = 250  # about 350 e5 tokens in Spanish, well under the model's 512


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    market: str
    title: str
    heading_path: tuple[str, ...]
    content: str

    def passage(self) -> str:
        """The text the embedder sees: title and headings for context, then the section."""
        return f"{self.title} — {' > '.join(self.heading_path)}\n\n{self.content}"

    def content_hash(self, model: str) -> str:
        """Changes when anything embedded changes, or the model does: then it's re-embedded."""
        key = [self.market, self.doc_id, self.title, list(self.heading_path), self.content, model]
        return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


def chunk_doc(doc: Doc) -> list[Chunk]:
    chunks: list[Chunk] = []
    for headings, paragraphs in _sections(doc.body):
        for piece in _split(paragraphs):
            chunks.append(Chunk(doc.doc_id, doc.market, doc.title, headings, "\n\n".join(piece)))
    return chunks


def _sections(body: str) -> list[tuple[tuple[str, ...], list[str]]]:
    sections: list[tuple[tuple[str, ...], list[str]]] = []
    h2 = h3 = ""
    for block in re.split(r"\n\s*\n", body.strip()):
        block = block.strip()
        heading = re.match(r"^(#{2,3}) (.+)$", block.splitlines()[0]) if block else None
        if heading:
            if heading.group(1) == "##":
                h2, h3 = heading.group(2).strip(), ""
            else:
                h3 = heading.group(2).strip()
            sections.append(((h2, h3) if h3 else (h2,), []))
            rest = "\n".join(block.splitlines()[1:]).strip()
            if rest:
                sections[-1][1].append(rest)
        elif block and sections:
            sections[-1][1].append(block)
    return [(headings, paragraphs) for headings, paragraphs in sections if paragraphs]


def _split(paragraphs: list[str]) -> list[list[str]]:
    pieces: list[list[str]] = [[]]
    for paragraph in paragraphs:
        current = pieces[-1]
        if current and _words(current + [paragraph]) > MAX_WORDS:
            pieces.append([current[-1], paragraph])  # repeat the previous paragraph
        else:
            current.append(paragraph)
    return pieces


def _words(paragraphs: list[str]) -> int:
    return sum(len(p.split()) for p in paragraphs)
