"""Session rules (ADR 0008): owners, lifetime, what's kept, and numbering across a conversation."""

import json

import pytest

from adapters.shared.memory_sessions import MemorySessionStore
from core.identity import ANONYMOUS, Caller, Tier
from core.pii import PIISpan
from core.redaction import Redactor, unredacted
from core.sessions import (
    ANONYMOUS_TTL_SECONDS,
    HISTORY_TURNS,
    MASTER_KEY_BYTES,
    MAX_PLACEHOLDERS,
    SIGNED_IN_TTL_SECONDS,
    SWEEP_LIMIT,
    Session,
    SessionRefused,
    Sessions,
    SessionUnavailable,
    to_record,
)

NOW = 1_000_000.0
ANA = Caller(Tier.SIGNED_IN, sub="user-123")
LUIS = Caller(Tier.STEP_UP, sub="user-456")
EMAIL = "ana@example.com"
MASTER_KEY = bytes(range(MASTER_KEY_BYTES))  # synthetic


def sessions(
    store: MemorySessionStore | None = None, now: float = NOW, master_key: bytes = MASTER_KEY
) -> Sessions:
    store = store if store is not None else MemorySessionStore()
    return Sessions(store, master_key, clock=lambda: now)


def stored(store: MemorySessionStore, caller: Caller, now: float = NOW) -> Session:
    session = sessions(store, now).open(caller, None)
    sessions(store, now).save(session)
    return session


class ValueDetector:
    """Finds the given synthetic values: enough to see the numbering."""

    def __init__(self, *values: str) -> None:
        self.values = values

    def detect(self, text: str) -> list[PIISpan]:
        found = ((value, text.find(value)) for value in self.values)
        return [PIISpan("PERSON", at, at + len(value), 1.0) for value, at in found if at != -1]

    def detect_patterns(self, text: str) -> list[PIISpan]:
        return []


# --- a new session ----------------------------------------------------------------------------


def test_a_new_session_has_a_random_id() -> None:
    first, second = sessions().open(ANA, None), sessions().open(ANA, None)
    assert first.id != second.id
    assert len(first.id) == 32


def test_a_missing_or_short_master_key_stops_the_start() -> None:
    for key in (b"", bytes(MASTER_KEY_BYTES - 1)):
        with pytest.raises(ValueError, match="master key"):
            Sessions(MemorySessionStore(), key)


def test_a_signed_in_session_lasts_30_days_and_an_anonymous_one_a_day() -> None:
    assert sessions().open(ANA, None).expires_at == NOW + SIGNED_IN_TTL_SECONDS
    assert sessions().open(ANONYMOUS, None).expires_at == NOW + ANONYMOUS_TTL_SECONDS


def test_opening_stores_nothing_until_it_is_saved() -> None:
    store = MemorySessionStore()
    session = sessions(store).open(ANA, None)
    assert store.load(session.id, NOW) is None


# --- who may use it ---------------------------------------------------------------------------


def test_the_owner_gets_their_session_back_at_any_tier() -> None:
    store = MemorySessionStore()
    session = stored(store, ANA)
    stepped_up = Caller(Tier.STEP_UP, sub=ANA.sub)
    assert sessions(store).open(stepped_up, session.id).id == session.id


@pytest.mark.parametrize(
    ("owner", "caller"),
    [
        pytest.param(ANA, LUIS, id="another-customer"),
        pytest.param(ANA, ANONYMOUS, id="anonymous-into-a-customers"),
        pytest.param(ANONYMOUS, ANA, id="customer-into-an-anonymous-one"),
    ],
)
def test_anyone_else_is_refused(owner: Caller, caller: Caller) -> None:
    store = MemorySessionStore()
    session = stored(store, owner)
    with pytest.raises(SessionRefused):
        sessions(store).open(caller, session.id)


def test_an_anonymous_visitor_continues_an_anonymous_session() -> None:
    store = MemorySessionStore()
    session = stored(store, ANONYMOUS)
    assert sessions(store).open(ANONYMOUS, session.id).id == session.id


@pytest.mark.parametrize(
    "session_id",
    ["", "abc", "A" * 32, "g" * 32, "a" * 33, "../../other/doc", "a" * 31 + "\n"],
)
def test_a_malformed_id_never_reaches_the_store(session_id: str) -> None:
    class NoLoad(MemorySessionStore):
        def load(self, session_id: str, now: float) -> Session | None:
            raise AssertionError("a malformed ID reached the store")

    assert sessions(NoLoad()).open(ANA, session_id).id != session_id


def test_an_unknown_or_expired_session_starts_a_new_one() -> None:
    store = MemorySessionStore()
    session = stored(store, ANA)
    later = NOW + SIGNED_IN_TTL_SECONDS
    assert sessions(store, later).open(ANA, session.id).id != session.id
    assert sessions(store).open(ANA, "b" * 32).id != "b" * 32


# --- what it keeps ----------------------------------------------------------------------------


def test_it_keeps_only_the_latest_turns() -> None:
    session = sessions().open(ANA, None)
    for i in range(HISTORY_TURNS + 2):
        session.add_turn(unredacted(f"pregunta {i}"), f"respuesta {i}")
    assert len(session.turns) == HISTORY_TURNS
    assert session.turns[0].question == "pregunta 2"
    assert session.turns[-1].answer == f"respuesta {HISTORY_TURNS + 1}"


def test_a_saved_session_never_holds_the_value() -> None:
    store = MemorySessionStore()
    session = sessions(store).open(ANA, None)
    numbering = sessions(store).numbering(session)
    redacted = Redactor(ValueDetector(EMAIL)).redact(f"Mi correo es {EMAIL}", numbering)
    session.add_turn(redacted, "De acuerdo.")
    sessions(store).save(session)
    record = json.dumps(to_record(session))
    assert EMAIL not in record
    assert "<PERSON_1>" in record


def test_a_saved_session_never_holds_a_key() -> None:
    store = MemorySessionStore()
    service = sessions(store)
    session = service.open(ANA, None)
    numbering = service.numbering(session)
    numbering.number("PERSON", "ana")
    service.save(session)
    record = json.dumps(to_record(session))
    assert MASTER_KEY.hex() not in record
    assert numbering._key.hex() not in record
    assert "key" not in to_record(session)


def test_the_stored_hmac_cannot_be_checked_without_the_master_key() -> None:
    service = sessions()
    session = service.open(ANA, None)
    service.numbering(session).number("US_SSN", "536904399")
    stored_digest = next(iter(session.placeholders))

    guess = Session(id=session.id, sub=None, created_at=NOW, expires_at=NOW)
    wrong_master = sessions(master_key=bytes(MASTER_KEY_BYTES))
    wrong_master.numbering(guess).number("US_SSN", "536904399")
    assert stored_digest not in guess.placeholders


# --- numbering across a conversation ----------------------------------------------------------


def test_a_value_keeps_its_placeholder_on_every_turn_after_a_reload() -> None:
    store = MemorySessionStore()
    redactor = Redactor(ValueDetector("Ana", "Luis"))
    service = sessions(store)

    session = service.open(ANA, None)
    assert redactor.redact("Soy Ana", service.numbering(session)).text == "Soy <PERSON_1>"
    service.save(session)

    session = service.open(ANA, session.id)
    assert redactor.redact("Mi hermano es Luis", service.numbering(session)).text == (
        "Mi hermano es <PERSON_2>"
    )
    service.save(session)

    session = service.open(ANA, session.id)
    assert redactor.redact("Soy Ana y él es Luis", service.numbering(session)).text == (
        "Soy <PERSON_1> y él es <PERSON_2>"
    )


def test_each_type_counts_on_its_own() -> None:
    service = sessions()
    numbering = service.numbering(service.open(ANA, None))
    assert numbering.number("PERSON", "ana") == 1
    assert numbering.number("EMAIL_ADDRESS", "ana") == 1
    assert numbering.number("PERSON", "luis") == 2


def test_the_same_value_has_a_different_hmac_in_each_session() -> None:
    service = sessions()
    first, second = service.open(ANA, None), service.open(ANA, None)
    service.numbering(first).number("PERSON", "ana")
    service.numbering(second).number("PERSON", "ana")
    assert set(first.placeholders).isdisjoint(second.placeholders)


def test_after_rotating_the_master_key_a_value_gets_a_new_number_and_none_collide() -> None:
    session = sessions().open(ANA, None)
    assert sessions().numbering(session).number("PERSON", "ana") == 1
    rotated = sessions(master_key=bytes(MASTER_KEY_BYTES)).numbering(session)
    assert rotated.number("PERSON", "ana") == 2
    assert rotated.number("PERSON", "luis") == 3


def test_past_the_cap_a_new_value_gets_a_new_number_and_is_not_remembered() -> None:
    service = sessions()
    session = service.open(ANA, None)
    numbering = service.numbering(session)
    for i in range(MAX_PLACEHOLDERS):
        numbering.number("PERSON", f"nombre {i}")
    assert numbering.number("PERSON", "ana") == MAX_PLACEHOLDERS + 1
    assert numbering.number("PERSON", "ana") == MAX_PLACEHOLDERS + 2
    assert len(session.placeholders) == MAX_PLACEHOLDERS
    assert numbering.number("PERSON", "nombre 0") == 1


def test_without_a_session_numbers_last_one_message() -> None:
    redactor = Redactor(ValueDetector("Ana"))
    assert redactor.redact("Soy Ana").text == "Soy <PERSON_1>"
    assert redactor.redact("Soy Ana").text == "Soy <PERSON_1>"


# --- deleting expired sessions ----------------------------------------------------------------


def test_saving_a_new_session_deletes_a_few_expired_ones() -> None:
    store = MemorySessionStore()
    for _ in range(SWEEP_LIMIT + 1):
        stored(store, ANONYMOUS)
    later = NOW + ANONYMOUS_TTL_SECONDS
    stored(store, ANONYMOUS, later)
    assert store.delete_expired(later, limit=100) == 1  # the one past the sweep's limit


def test_saving_an_existing_session_does_not_sweep() -> None:
    class Counting(MemorySessionStore):
        sweeps = 0

        def delete_expired(self, now: float, limit: int) -> int:
            self.sweeps += 1
            return super().delete_expired(now, limit)

    store = Counting()
    session = stored(store, ANA)
    assert store.sweeps == 1
    sessions(store).save(session)
    assert store.sweeps == 1


def test_a_failed_sweep_does_not_fail_the_save() -> None:
    class SweepDown(MemorySessionStore):
        def delete_expired(self, now: float, limit: int) -> int:
            raise SessionUnavailable()

    store = SweepDown()
    session = stored(store, ANA)
    assert store.load(session.id, NOW) == session


def test_a_failed_save_reaches_the_caller() -> None:
    class Down(MemorySessionStore):
        def save(self, session: Session) -> None:
            raise SessionUnavailable()

    service = sessions(Down())
    with pytest.raises(SessionUnavailable):
        service.save(service.open(ANA, None))


# --- market (a conversation is about one market's policy) -----------------------------------------


def test_a_conversation_continues_in_its_own_market() -> None:
    store = MemorySessionStore()
    first = sessions(store).open(ANA, None, "MX")
    sessions(store).save(first)

    again = sessions(store).open(ANA, first.id, "MX")

    assert again.id == first.id and again.market == "MX"


def test_another_market_starts_a_new_conversation(caplog: pytest.LogCaptureFixture) -> None:
    store = MemorySessionStore()
    mexico = sessions(store).open(ANA, None, "MX")
    sessions(store).save(mexico)

    with caplog.at_level("INFO"):
        us = sessions(store).open(ANA, mexico.id, "US")

    assert us.id != mexico.id and us.market == "US" and us.version == 0
    assert "session_market_changed" in caplog.text and "MX" not in caplog.text


def test_a_session_stored_before_markets_takes_the_current_one() -> None:
    store = MemorySessionStore()
    old = sessions(store).open(ANA, None)
    sessions(store).save(old)
    record = json.loads(store._records[old.id])
    del record["market"]  # as Firestore holds the sessions saved before this change
    store._records[old.id] = json.dumps(record)

    opened = sessions(store).open(ANA, old.id, "US")

    assert opened.id == old.id and opened.market == "US"
