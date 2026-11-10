"""Conversation sessions: who owns one, how long it lasts, and what it may hold (ADR 0008).

The rules live here so they're the same on Firestore, Cosmos DB and DynamoDB; a `SessionStore` only
keeps records.

- **Owner:** a session belongs to the `sub` that started it, or to nobody for an anonymous visitor,
  and only that exact caller may use it. So signing in starts a new session: continuing an anonymous
  one would let whoever knew its ID write into a customer's history, which later reaches the model.
- **Market:** a conversation is about one market's policy. A message for another market starts a
  new one, so the model never sees one country's answers beside another's sources.
- **Lifetime:** fixed from creation, 30 days signed in and 1 day anonymous. An expired session is
  never served, whether or not it has been deleted yet.
- **Contents:** only redacted text, the last 6 turns. `add_turn` takes a `Redacted` question, and
  the answer it takes has already passed the output scan.
- **Placeholders:** the same value gets the same `<TYPE_n>` on every turn. The session keeps an HMAC
  of the value and its number, never the value. The HMAC's key is derived from a master key in the
  cloud's secret store and the session's ID, and it's never stored. A key kept beside the HMACs
  would let anyone reading the store test guesses, and a 9-digit SSN doesn't take many.
- **Deletion:** Firestore's TTL policies have no free usage, so saving a new session also deletes a
  few expired ones. Deleting more than are created keeps the store from growing, with no scheduler.
"""

import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from core.identity import Caller
from core.ports import SessionStore
from core.redaction import Redacted

logger = logging.getLogger(__name__)

SIGNED_IN_TTL_SECONDS = 30 * 24 * 60 * 60
ANONYMOUS_TTL_SECONDS = 24 * 60 * 60
HISTORY_TURNS = 6  # what the model gets
# Keeps a session far under Firestore's 1 MiB document limit. Past it, a new value still gets a new
# number but isn't remembered: the conversation is a little less coherent, and nothing leaks.
MAX_PLACEHOLDERS = 500
MASTER_KEY_BYTES = 32
SWEEP_LIMIT = 5  # expired sessions deleted per new session

# 128 random bits. It comes from the client and becomes a document path in the store, so nothing
# else gets that far.
_SESSION_ID = re.compile(r"[0-9a-f]{32}")


class SessionRefused(Exception):
    """This session belongs to someone else. No detail: there's nothing the caller should learn."""


class SessionConflict(Exception):
    """The session changed or was deleted since it was loaded, so this save was not applied."""


class SessionUnavailable(Exception):
    """The store can't be reached. Carries no detail: the cause can quote a session's contents."""


@dataclass(frozen=True)
class Turn:
    """One question and its answer: both safe to store and to send to the model."""

    question: str
    answer: str


@dataclass
class Session:
    id: str
    sub: str | None  # None: an anonymous session, usable only by anonymous callers
    created_at: float
    expires_at: float
    placeholders: dict[str, int] = field(default_factory=dict)  # HMAC of a value -> its number
    counters: dict[str, int] = field(default_factory=dict)  # entity type -> numbers given out
    turns: list[Turn] = field(default_factory=list)
    market: str | None = None  # the market it's about; None only in sessions stored before it
    version: int = 0  # 0: not stored yet. The store raises it on each save

    def add_turn(self, question: Redacted, answer: str) -> None:
        """Keeps the latest turns only. `answer` is what the output scan released."""
        self.turns = [*self.turns, Turn(question.text, answer)][-HISTORY_TURNS:]


class PlaceholderNumbers:
    """Numbers a session's placeholders, under a key that isn't stored with it (`core.redaction`
    calls this `Numbering`). Rotating the master key costs coherence, not safety: a value seen
    before gets a new number, and the counters keep any two values from sharing one."""

    def __init__(self, session: Session, key: bytes) -> None:
        self._session = session
        self._key = key

    def number(self, entity_type: str, value: str) -> int:
        """The placeholder number for a normalized value, the same on every turn."""
        message = f"{entity_type}\0{value}".encode()
        digest = hmac.new(self._key, message, hashlib.sha256).hexdigest()
        session = self._session
        known = session.placeholders.get(digest)
        if known is not None:
            return known
        number = session.counters.get(entity_type, 0) + 1
        session.counters[entity_type] = number
        if len(session.placeholders) < MAX_PLACEHOLDERS:
            session.placeholders[digest] = number
        return number


def to_record(session: Session) -> dict[str, Any]:
    """Plain data for a store to keep. Every adapter stores this and returns it unchanged."""
    return asdict(session)


def from_record(record: Mapping[str, Any]) -> Session:
    return Session(
        id=record["id"],
        sub=record["sub"],
        created_at=record["created_at"],
        expires_at=record["expires_at"],
        placeholders=dict(record["placeholders"]),
        counters=dict(record["counters"]),
        turns=[Turn(turn["question"], turn["answer"]) for turn in record["turns"]],
        market=record.get("market"),  # absent in sessions stored before markets were kept
        version=record["version"],
    )


class Sessions:
    def __init__(
        self, store: SessionStore, master_key: bytes, clock: Callable[[], float] = time.time
    ) -> None:
        if len(master_key) < MASTER_KEY_BYTES:  # a missing or short secret must stop the start
            raise ValueError(f"The sessions' master key needs {MASTER_KEY_BYTES} bytes")
        self._store = store
        self._master_key = master_key
        self._clock = clock

    def open(self, caller: Caller, session_id: str | None, market: str | None = None) -> Session:
        """The caller's session, or a new one if the ID is missing, malformed, unknown or expired,
        or the session is about another market.

        Raises `SessionRefused` for someone else's, and `SessionUnavailable` if the store fails. A
        new session isn't stored until `save`, so a refused message writes nothing.
        """
        session = None
        if session_id is not None and _SESSION_ID.fullmatch(session_id):
            session = self._store.load(session_id, self._clock())
        if session is None:
            return self.start(caller, market)
        if session.sub != caller.sub:
            raise SessionRefused()
        if market is not None and session.market not in (None, market):
            logger.info("session_market_changed")  # the codes only matter to the caller
            return self.start(caller, market)
        if session.market is None:
            session.market = market  # stored before markets were kept: it takes this one
        return session

    def numbering(self, session: Session) -> PlaceholderNumbers:
        """What `Redactor.redact` needs to number this session's placeholders."""
        message = b"placeholders\0" + session.id.encode()
        key = hmac.new(self._master_key, message, hashlib.sha256).digest()
        return PlaceholderNumbers(session, key)

    def save(self, session: Session) -> None:
        """Stores the session. Raises `SessionConflict` if it changed since it was loaded, and
        `SessionUnavailable` if the store fails. A failed sweep costs nothing: the next one retries.
        """
        created = session.version == 0
        self._store.save(session)
        if created:
            try:
                self._store.delete_expired(self._clock(), SWEEP_LIMIT)
            except SessionUnavailable as exc:
                logger.warning("session_sweep_error type=%s", type(exc).__name__)

    def start(self, caller: Caller, market: str | None = None) -> Session:
        """A new session for this caller, not stored until `save`."""
        now = self._clock()
        ttl = ANONYMOUS_TTL_SECONDS if caller.sub is None else SIGNED_IN_TTL_SECONDS
        return Session(
            id=secrets.token_hex(16),
            sub=caller.sub,
            created_at=now,
            expires_at=now + ttl,
            market=market,
        )
