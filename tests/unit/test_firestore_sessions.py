"""The Firestore session store's error handling. Its behavior is in the SessionStore contract suite,
against the emulator; these cover the failures the emulator can't produce. All values synthetic."""

from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.auth.exceptions import DefaultCredentialsError

from adapters.gcp.firestore_sessions import FirestoreSessionStore, firestore_session_store
from core.sessions import Session, SessionUnavailable, Turn

SECRET_TURN = "Mi tarjeta termina en <CREDIT_CARD_1>"


def session(version: int = 0) -> Session:
    return Session(
        id="a" * 32,
        sub="user-123",
        created_at=0.0,
        expires_at=100.0,
        turns=[Turn(SECRET_TURN, "Respuesta")],
        version=version,
    )


def failing_store(error: Exception) -> FirestoreSessionStore:
    client = MagicMock()
    document = client.collection.return_value.document.return_value
    document.get.side_effect = error
    document.create.side_effect = error
    query = client.collection.return_value.where.return_value.select.return_value.limit.return_value
    query.stream.side_effect = error
    return FirestoreSessionStore(client)


@pytest.mark.parametrize(
    "error",
    [ServiceUnavailable(f"quoting {SECRET_TURN}"), DefaultCredentialsError("no credentials")],
    ids=["unreachable", "no-credentials"],
)
def test_every_store_failure_is_unavailable_and_says_nothing(error: Exception) -> None:
    store = failing_store(error)
    for call in (
        lambda: store.load("a" * 32, 0.0),
        lambda: store.save(session()),
        lambda: store.delete_expired(0.0, 5),
    ):
        with pytest.raises(SessionUnavailable) as raised:
            call()
        assert str(raised.value) == ""  # the cause can quote a session; the error never does


def test_a_failed_save_leaves_the_version_alone() -> None:
    unsaved = session()
    with pytest.raises(SessionUnavailable):
        failing_store(ServiceUnavailable("down")).save(unsaved)
    assert unsaved.version == 0


def test_the_project_is_required() -> None:
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        firestore_session_store("")
