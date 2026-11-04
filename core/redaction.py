"""Redaction: personal data out of the message before anything else reads it (ADR 0008).

A `PIIDetector` finds spans; this module decides what's removed and how:

- **Which types:** an allowlist, so a new Presidio recognizer can't change it silently. Places,
  organizations, dates, amounts and URLs stay, since policy questions are about them («enviar 500
  dólares a El Salvador»).
- **Threshold 0.4,** where phone numbers land without context. Over-redacting costs an answer a
  little less specific; under-redacting sends PII to the model.
- **Placeholders** `<TYPE_n>`, numbered in order of appearance, the same value always getting the
  same number. Numbered per message for now; sessions number them per conversation, by HMAC.
- **Check digits** for the IDs Presidio doesn't know in Spanish (CURP, CLABE, DUI). The adapter's
  recognizers call these, so a number with a wrong check digit is left alone.

`Redacted` is the only type the chat service accepts, so raw text can't reach retrieval or the
model by mistake. Counts are by type: never the values.

The model's answer is scanned too (`OutputScanner`), with the pattern recognizers only: the model
never sees a name, and NER on half a sentence is unreliable. Anything found, and any placeholder
the model repeats, becomes «[dato protegido]». Placeholders are never filled back in: that would
need the values stored, and the customer already sees what they typed.
"""

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from core.pii import PIISpan
from core.ports import PIIDetector

REDACTED_TYPES = frozenset(
    {
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "CREDIT_CARD",
        "IBAN_CODE",
        "US_SSN",
        "US_ITIN",
        "MX_CURP",
        "MX_CLABE",
        "SV_DUI",
        "ES_NIF",
        "ES_NIE",
    }
)
MIN_SCORE = 0.4

PROTECTED = "[dato protegido]"  # what the customer sees in place of anything caught in an answer
PLACEHOLDER = re.compile(r"<[A-Z][A-Z_]*_\d+>")  # one of ours, echoed by the model
# Characters of the answer held back until more arrive, so an ID or a placeholder split across two
# chunks is still whole when it's scanned. Longer than any ID we catch (an IBAN with spaces: 42).
HOLDBACK = 64


@dataclass(frozen=True)
class Redacted:
    """Text with its personal data replaced. Build it with `Redactor`, or `unredacted` in tests."""

    text: str
    counts: Mapping[str, int] = field(default_factory=dict)  # entity type -> how many removed

    def summary(self) -> str:
        return summary(self.counts)


def summary(counts: Mapping[str, int]) -> str:
    """For logs and spans: «EMAIL_ADDRESS:1,PERSON:2». Types and counts only."""
    return ",".join(f"{kind}:{n}" for kind, n in sorted(counts.items()) if n)


def unredacted(text: str) -> Redacted:
    """Text that needs no redaction: for tests and local runs without a detector."""
    return Redacted(text)


def _normalized(value: str) -> str:
    """The same ID typed with spaces, hyphens or another case gets the same placeholder."""
    return re.sub(r"[\s.\-]", "", value).casefold()


def _chosen(spans: list[PIISpan]) -> list[PIISpan]:
    """Spans to replace, without overlaps: the most certain first, then the longest."""
    kept: list[PIISpan] = []
    candidates = [s for s in spans if s.entity_type in REDACTED_TYPES and s.score >= MIN_SCORE]
    for span in sorted(candidates, key=lambda s: (-s.score, -(s.end - s.start), s.start)):
        if all(span.end <= k.start or span.start >= k.end for k in kept):
            kept.append(span)
    return sorted(kept, key=lambda s: s.start)


class Redactor:
    def __init__(self, detector: PIIDetector) -> None:
        self._detector = detector

    def redact(self, text: str) -> Redacted:
        """Raises `core.pii.DetectorUnavailable` if the detector fails: the caller fails closed."""
        spans = _chosen(self._detector.detect(text))
        numbers: dict[tuple[str, str], int] = {}
        per_type: Counter[str] = Counter()
        pieces, cursor = [], 0
        for span in spans:
            key = (span.entity_type, _normalized(text[span.start : span.end]))
            if key not in numbers:
                per_type[span.entity_type] += 1
                numbers[key] = per_type[span.entity_type]
            pieces += [text[cursor : span.start], f"<{span.entity_type}_{numbers[key]}>"]
            cursor = span.end
        pieces.append(text[cursor:])
        counts = Counter(span.entity_type for span in spans)
        return Redacted("".join(pieces), dict(counts))

    def output_scanner(self) -> "OutputScanner":
        """A scanner for one answer, with the detector's pattern recognizers only."""
        return OutputScanner(self._detector.detect_patterns)


# Check digits. Each takes the value as typed (any case, with or without separators).

_CURP_ALPHABET = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"


def curp_is_valid(value: str) -> bool:
    """Mexico's CURP: 18 characters, the last a check digit over the first 17 (RENAPO)."""
    curp = re.sub(r"\s", "", value).upper()
    if len(curp) != 18 or any(c not in _CURP_ALPHABET for c in curp[:17]) or not curp[17].isdigit():
        return False
    total = sum(_CURP_ALPHABET.index(c) * (18 - i) for i, c in enumerate(curp[:17]))
    return (10 - total % 10) % 10 == int(curp[17])


def clabe_is_valid(value: str) -> bool:
    """Mexico's CLABE: 18 digits, the last a check digit with weights 3, 7, 1 (Banxico)."""
    digits = re.sub(r"[\s\-]", "", value)
    if len(digits) != 18 or not digits.isdigit():
        return False
    total = sum(int(d) * (3, 7, 1)[i % 3] % 10 for i, d in enumerate(digits[:17]))
    return (10 - total % 10) % 10 == int(digits[17])


def dui_is_valid(value: str) -> bool:
    """El Salvador's DUI: 8 digits and a check digit, with weights 9 down to 2."""
    digits = re.sub(r"[\s\-]", "", value)
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = sum(int(d) * (9 - i) for i, d in enumerate(digits[:8]))
    return (10 - total % 10) % 10 == int(digits[8])


class OutputScanner:
    """Scans the model's answer as it streams, releasing only text that has been checked.

    `feed` takes each chunk and returns what's safe to show now; `finish` releases the rest. The
    last HOLDBACK characters stay until more text arrives, and the cut never falls inside a match,
    so nothing is released half-scanned. `scan` is the detector's pattern-only method; it raises
    if it fails, and the caller ends the answer: held-back text is never released unchecked.
    """

    def __init__(self, scan: Callable[[str], list[PIISpan]]) -> None:
        self._scan = scan
        self._pending = ""
        self.counts: Counter[str] = Counter()  # what was replaced, by type ("PLACEHOLDER" too)

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        if len(self._pending) <= HOLDBACK:
            return ""
        return self._release(keep=HOLDBACK)

    def finish(self) -> str:
        return self._release(keep=0)

    def _release(self, keep: int) -> str:
        text = self._pending
        spans = _chosen(self._scan(text))
        spans += [
            PIISpan("PLACEHOLDER", m.start(), m.end(), 1.0)
            for m in PLACEHOLDER.finditer(text)
            if all(m.end() <= s.start or m.start() >= s.end for s in spans)
        ]
        spans.sort(key=lambda s: s.start)
        cut = len(text) - keep
        for span in spans:  # a match across the cut waits whole: it may not be complete yet
            if span.start < cut < span.end:
                cut = span.start
        pieces, cursor = [], 0
        for span in spans:
            if span.end > cut:
                break
            pieces += [text[cursor : span.start], PROTECTED]
            self.counts[span.entity_type] += 1
            cursor = span.end
        pieces.append(text[cursor:cut])
        self._pending = text[cut:]
        return "".join(pieces)
