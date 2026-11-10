"""core.chat with sessions (ADR 0008): history as separate turns, placeholders numbered per
conversation, and memory that never costs a chat. All values are synthetic."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence

import pytest

from adapters.shared.memory_sessions import MemorySessionStore
from core.chat import ChatService, Prepared, Sources
from core.identity import ANONYMOUS, Caller, Tier
from core.pii import PIISpan
from core.redaction import PROTECTED, Redactor
from core.safety import ContentBlocked
from core.sessions import Session, SessionConflict, Sessions, SessionUnavailable, Turn
from core.usage import TokenUsage

ANA = "ana@example.com"
LUIS = "luis@example.com"
SIGNED_IN = Caller(Tier.SIGNED_IN, "user-123")
KEY = b"k" * 32


class EmailDetector:
    """Finds the synthetic addresses: enough to see how placeholders are numbered."""

    def detect(self, text: str) -> list[PIISpan]:
        spans = []
        for email in (ANA, LUIS):
            start = text.find(email)
            if start != -1:
                spans.append(PIISpan("EMAIL_ADDRESS", start, start + len(email), 1.0))
        return spans

    def detect_patterns(self, text: str) -> list[PIISpan]:
        return self.detect(text)


class RecordingLLM:
    """Answers with `reply`, and keeps what each call was given."""

    model = "recording"

    def __init__(self, reply: str = "Claro.") -> None:
        self.reply = reply
        self.calls: list[tuple[str, tuple[Turn, ...]]] = []

    async def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence[Turn] = (),
    ) -> AsyncIterator[str]:
        self.calls.append((user_message, tuple(history)))
        yield self.reply


class BlockingLLM(RecordingLLM):
    async def stream(
        self,
        system: str,
        user_message: str,
        usage: TokenUsage | None = None,
        history: Sequence[Turn] = (),
    ) -> AsyncIterator[str]:
        yield "Empiezo"
        raise ContentBlocked("output", ("TEST",))


class FailingStore(MemorySessionStore):
    """A store that's down: loads, saves or both raise as an adapter would."""

    def __init__(self, load: bool = False, save: bool = False) -> None:
        super().__init__()
        self.fail_load, self.fail_save = load, save

    def load(self, session_id: str, now: float) -> Session | None:
        if self.fail_load:
            raise SessionUnavailable()
        return super().load(session_id, now)

    def save(self, session: Session) -> None:
        if self.fail_save:
            raise SessionUnavailable()
        super().save(session)


def service(
    llm: RecordingLLM, store: MemorySessionStore | None = None
) -> tuple[ChatService, MemorySessionStore]:
    store = store if store is not None else MemorySessionStore()
    chat = ChatService(llm, redactor=Redactor(EmailDetector()), sessions=Sessions(store, KEY))
    return chat, store


def turn(
    chat: ChatService, message: str, caller: Caller = ANONYMOUS, session_id: str | None = None
) -> tuple[Prepared, str]:
    """One message through `prepare` and `reply`, as the API runs them."""
    prepared = chat.prepare(message, caller, session_id)

    async def consume() -> str:
        events = [e async for e in chat.reply(prepared.redacted, None, prepared.session)]
        return "".join(e for e in events if not isinstance(e, Sources))

    return prepared, asyncio.run(consume())


def session_id(prepared: Prepared) -> str:
    assert prepared.session is not None
    return prepared.session.id


def test_the_next_turn_gets_the_last_one_as_its_own_message() -> None:
    llm = RecordingLLM("Puedes abrirla en línea.")
    chat, _ = service(llm)
    first, _ = turn(chat, "¿Cómo abro una cuenta?")
    turn(chat, "¿Y cuánto tarda?", session_id=session_id(first))

    [(_, none), (prompt, history)] = llm.calls
    assert none == ()
    assert history == (Turn("¿Cómo abro una cuenta?", "Puedes abrirla en línea."),)
    assert prompt == "¿Y cuánto tarda?"  # the earlier turn is never pasted into the message


def test_a_value_keeps_its_placeholder_on_every_turn() -> None:
    llm = RecordingLLM()
    chat, _ = service(llm)
    first, _ = turn(chat, f"Mi correo es {ANA}.")
    second, _ = turn(chat, f"Cambia {ANA} por {LUIS}.", session_id=session_id(first))

    assert first.redacted.text == "Mi correo es <EMAIL_ADDRESS_1>."
    assert second.redacted.text == "Cambia <EMAIL_ADDRESS_1> por <EMAIL_ADDRESS_2>."


def test_the_store_keeps_only_redacted_text() -> None:
    # The model's answer names an address too: what's kept is what the visitor saw.
    chat, store = service(RecordingLLM(f"Te escribiremos a {LUIS}."))
    first, answer = turn(chat, f"Mi correo es {ANA}.")

    assert answer == f"Te escribiremos a {PROTECTED}."
    stored = json.dumps(store._records)
    assert ANA not in stored and LUIS not in stored
    saved = store.load(session_id(first), 0)
    assert saved is not None
    assert saved.turns == [Turn("Mi correo es <EMAIL_ADDRESS_1>.", answer)]


def test_signing_in_mid_conversation_starts_a_new_one(caplog: pytest.LogCaptureFixture) -> None:
    llm = RecordingLLM()
    chat, _ = service(llm)
    anonymous, _ = turn(chat, "¿Cómo abro una cuenta?")

    with caplog.at_level(logging.INFO):
        signed_in, _ = turn(chat, "Ya entré.", SIGNED_IN, session_id(anonymous))

    assert session_id(signed_in) != session_id(anonymous)
    assert llm.calls[-1][1] == ()  # nothing from before signing in reaches the model
    assert "session_refused" in caplog.text


def test_signing_out_mid_conversation_starts_a_new_one() -> None:
    llm = RecordingLLM()
    chat, _ = service(llm)
    signed_in, _ = turn(chat, "¿Cuál es mi saldo?", SIGNED_IN)
    anonymous, _ = turn(chat, "Hola", ANONYMOUS, session_id(signed_in))

    assert session_id(anonymous) != session_id(signed_in)
    assert llm.calls[-1][1] == ()


@pytest.mark.parametrize("bad_id", ["", "nope", "A" * 32, "0" * 32, "../" * 10])
def test_a_malformed_or_unknown_id_starts_a_new_conversation(bad_id: str) -> None:
    chat, _ = service(RecordingLLM())
    prepared, _ = turn(chat, "Hola", session_id=bad_id)

    assert prepared.session is not None and prepared.session.version == 1
    assert session_id(prepared) != bad_id


def test_a_store_that_is_down_costs_only_the_memory(caplog: pytest.LogCaptureFixture) -> None:
    llm = RecordingLLM()
    store = FailingStore(load=True)
    chat, _ = service(llm, store)

    with caplog.at_level(logging.WARNING):
        prepared, answer = turn(chat, f"Mi correo es {ANA}.", session_id="a" * 32)

    assert prepared.session is None and answer == "Claro."
    assert prepared.redacted.text == "Mi correo es <EMAIL_ADDRESS_1>."  # redaction still ran
    assert store._records == {}
    assert "session_unavailable type=SessionUnavailable" in caplog.text


def test_a_failed_save_never_fails_the_chat(caplog: pytest.LogCaptureFixture) -> None:
    chat, _ = service(RecordingLLM(), FailingStore(save=True))

    with caplog.at_level(logging.WARNING):
        _, answer = turn(chat, "Hola")

    assert answer == "Claro."
    assert "session_save_error type=SessionUnavailable" in caplog.text


def test_a_save_that_lost_a_race_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    llm = RecordingLLM()
    chat, store = service(llm)
    first, _ = turn(chat, "Uno")
    # Two requests on the same conversation load the same version; the first to save wins.
    a = chat.prepare("Dos", ANONYMOUS, session_id(first))
    b = chat.prepare("Tres", ANONYMOUS, session_id(first))

    async def answer(prepared: Prepared) -> None:
        async for _ in chat.reply(prepared.redacted, None, prepared.session):
            pass

    asyncio.run(answer(a))
    with caplog.at_level(logging.INFO):
        asyncio.run(answer(b))

    assert "session_conflict" in caplog.text
    saved = store.load(session_id(first), 0)
    assert saved is not None and [t.question for t in saved.turns] == ["Uno", "Dos"]
    with pytest.raises(SessionConflict):  # the losing copy is stale, so it can't be saved later
        Sessions(store, KEY).save(b.session)  # type: ignore[arg-type]


def test_a_blocked_answer_is_never_kept() -> None:
    chat, store = service(BlockingLLM())
    prepared = chat.prepare("Hola", ANONYMOUS)

    async def consume() -> None:
        async for _ in chat.reply(prepared.redacted, None, prepared.session):
            pass

    with pytest.raises(ContentBlocked):
        asyncio.run(consume())
    assert store._records == {}


def test_usage_names_the_history_part_once_there_is_one() -> None:
    from adapters.shared.memory_usage import MemoryUsageSink

    sink = MemoryUsageSink()
    store = MemorySessionStore()
    chat = ChatService(RecordingLLM(), usage=sink, sessions=Sessions(store, KEY))
    first, _ = turn(chat, "Uno")
    turn(chat, "Dos", session_id=session_id(first))

    assert [r.prompt_parts for r in sink.records] == [
        ("system", "user"),
        ("system", "history", "user"),
    ]


def test_without_sessions_nothing_changes() -> None:
    llm = RecordingLLM()
    chat = ChatService(llm, redactor=Redactor(EmailDetector()))
    prepared, _ = turn(chat, f"Mi correo es {ANA}.")

    assert prepared.session is None
    assert prepared.redacted.text == "Mi correo es <EMAIL_ADDRESS_1>."
    assert llm.calls == [("Mi correo es <EMAIL_ADDRESS_1>.", ())]
