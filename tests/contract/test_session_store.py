"""Contract for SessionStore: every adapter must pass every test here.

To add an adapter (Cosmos DB in M7, DynamoDB in M8), add one entry to ADAPTERS. All values are
synthetic.

Firestore runs against the emulator when FIRESTORE_EMULATOR_HOST is set, as in CI. Locally:

    IMAGE=gcr.io/google.com/cloudsdktool/google-cloud-cli:592.0.0-emulators
    docker run -d -p 8085:8085 $IMAGE gcloud emulators firestore start --host-port=0.0.0.0:8085
    FIRESTORE_EMULATOR_HOST=localhost:8085 uv run pytest tests/contract/test_session_store.py
"""

import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from adapters.shared.memory_sessions import MemorySessionStore
from core.ports import SessionStore
from core.sessions import Session, SessionConflict, Turn


def firestore_store() -> SessionStore:
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("FIRESTORE_EMULATOR_HOST is not set; start the emulator (see the docstring)")
    from google.cloud import firestore

    from adapters.gcp.firestore_sessions import FirestoreSessionStore

    # A collection per test, so no test sees another's sessions and nothing needs cleaning up.
    client = firestore.Client(project="demo-sessions")
    return FirestoreSessionStore(client, collection=f"sessions-{uuid.uuid4().hex}")


ADAPTERS: list[Any] = [
    pytest.param(MemorySessionStore, id="memory"),
    pytest.param(firestore_store, id="firestore"),
]
NOW = 1_000_000.0


@pytest.fixture(params=ADAPTERS)
def store(request: pytest.FixtureRequest) -> SessionStore:
    build: Callable[[], SessionStore] = request.param
    return build()


def session(session_id: str = "a" * 32, expires_at: float = NOW + 100) -> Session:
    return Session(
        id=session_id,
        sub="user-123",
        created_at=NOW,
        expires_at=expires_at,
        placeholders={"f" * 64: 2},
        counters={"PERSON": 2},
        turns=[Turn("¿Cómo bloqueo mi tarjeta, <PERSON_2>?", "Desde la app, en «Tarjetas».")],
    )


def test_a_saved_session_comes_back_whole(store: SessionStore) -> None:
    saved = session()
    store.save(saved)
    assert store.load(saved.id, NOW) == saved


def test_an_anonymous_session_keeps_no_owner(store: SessionStore) -> None:
    saved = session()
    saved.sub = None
    store.save(saved)
    loaded = store.load(saved.id, NOW)
    assert loaded is not None and loaded.sub is None


def test_an_unknown_session_is_none(store: SessionStore) -> None:
    assert store.load("b" * 32, NOW) is None


def test_saving_raises_the_version_by_one(store: SessionStore) -> None:
    saved = session()
    store.save(saved)
    assert saved.version == 1
    store.save(saved)
    assert saved.version == 2
    loaded = store.load(saved.id, NOW)
    assert loaded is not None and loaded.version == 2


def test_a_stale_save_is_refused_and_stores_nothing(store: SessionStore) -> None:
    saved = session()
    store.save(saved)
    first, second = store.load(saved.id, NOW), store.load(saved.id, NOW)
    assert first is not None and second is not None
    first.turns = [Turn("primera", "respuesta")]
    store.save(first)
    second.turns = [Turn("segunda", "respuesta")]
    with pytest.raises(SessionConflict):
        store.save(second)
    assert store.load(saved.id, NOW) == first


def test_creating_a_session_that_exists_is_refused(store: SessionStore) -> None:
    store.save(session())
    with pytest.raises(SessionConflict):
        store.save(session())


def test_saving_a_session_that_is_gone_does_not_bring_it_back(store: SessionStore) -> None:
    saved = session(expires_at=NOW)
    store.save(saved)
    assert store.delete_expired(NOW, limit=1) == 1
    with pytest.raises(SessionConflict):
        store.save(saved)


def test_an_expired_session_is_not_served_even_before_it_is_deleted(store: SessionStore) -> None:
    saved = session(expires_at=NOW)
    store.save(saved)
    assert store.load(saved.id, NOW - 1) is not None
    assert store.load(saved.id, NOW) is None


def test_delete_expired_removes_only_those_and_respects_the_limit(store: SessionStore) -> None:
    live = session("1" * 32, expires_at=NOW + 1)
    store.save(live)
    for session_id in ("2" * 32, "3" * 32, "4" * 32):
        store.save(session(session_id, expires_at=NOW))
    assert store.delete_expired(NOW, limit=2) == 2
    assert store.delete_expired(NOW, limit=2) == 1
    assert store.delete_expired(NOW, limit=2) == 0
    assert store.load(live.id, NOW) == live
