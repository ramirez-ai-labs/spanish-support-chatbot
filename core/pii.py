"""PII detection: what a detector reports, before the core decides what to redact (ADR 0008).

A `PIIDetector` adapter only finds spans. The core owns everything else (which types are
redacted, the score threshold, placeholders, the output window), so the rules stay cloud-free and
unit-tested. Spans carry positions and a type, never a copy of the value.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PIISpan:
    entity_type: str  # e.g. PERSON, EMAIL_ADDRESS, CREDIT_CARD, ES_NIF
    start: int
    end: int
    score: float


class DetectorUnavailable(Exception):
    """The detector failed. Redaction fails closed: the message goes no further."""
