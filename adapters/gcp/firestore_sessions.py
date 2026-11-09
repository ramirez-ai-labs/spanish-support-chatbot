"""SessionStore on Firestore: one document per session, in the project's `(default)` database.

The free tier covers only that database: 1 GiB stored, and 50k reads, 20k writes and 20k deletes a
day. Each turn costs one read to load and, to save, one read and one write in a transaction.

- **Version-checked saves.** A new session is created only if its ID is free, and a stored one is
  replaced in a transaction that first checks the version it was loaded at. A save that lost a race,
  or whose session was swept, stores nothing.
- **Expiry.** `load` checks `expires_at` itself, since a session can outlive its expiry until a
  sweep deletes it. The sweep's query uses Firestore's automatic single-field index on `expires_at`,
  and TTL policies stay off: they have no free usage.
- **Errors.** Anything from the client or its credentials becomes `SessionUnavailable`, and nothing
  here logs: a session holds redacted text, but still nothing a log needs.

With FIRESTORE_EMULATOR_HOST set, the client uses the emulator, as the contract tests do.
"""

from typing import Any

from google.api_core.exceptions import AlreadyExists, GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from core.sessions import Session, SessionConflict, SessionUnavailable, from_record, to_record

COLLECTION = "sessions"
TIMEOUT_SECONDS = 5.0  # per call, retries included: a turn waits on the load


class _Stale(Exception):
    """The stored version differs from the loaded one: raised inside a transaction to abort it."""


class FirestoreSessionStore:
    def __init__(self, client: firestore.Client, collection: str = COLLECTION) -> None:
        self._client = client
        self._sessions = client.collection(collection)

    def load(self, session_id: str, now: float) -> Session | None:
        try:
            snapshot = self._sessions.document(session_id).get(timeout=TIMEOUT_SECONDS)
        except (GoogleAPIError, GoogleAuthError) as exc:
            raise SessionUnavailable() from exc
        record = snapshot.to_dict() if snapshot.exists else None
        if record is None:
            return None
        session = from_record(record)
        return session if session.expires_at > now else None

    def save(self, session: Session) -> None:
        record = {**to_record(session), "version": session.version + 1}
        document = self._sessions.document(session.id)
        try:
            if session.version == 0:
                document.create(record, timeout=TIMEOUT_SECONDS)
            else:
                self._replace(document, record, session.version)
        except (AlreadyExists, _Stale) as exc:
            raise SessionConflict() from exc
        except (GoogleAPIError, GoogleAuthError) as exc:
            raise SessionUnavailable() from exc
        session.version += 1

    def _replace(
        self, document: firestore.DocumentReference, record: dict[str, Any], loaded: int
    ) -> None:
        @firestore.transactional
        def replace(transaction: firestore.Transaction) -> None:
            snapshot = document.get(transaction=transaction, timeout=TIMEOUT_SECONDS)
            stored = snapshot.to_dict() if snapshot.exists else None
            if stored is None or stored.get("version") != loaded:
                raise _Stale()  # changed or deleted since it was loaded
            transaction.set(document, record)

        replace(self._client.transaction())

    def delete_expired(self, now: float, limit: int) -> int:
        try:
            expired = list(
                self._sessions.where(filter=FieldFilter("expires_at", "<=", now))
                .select(["expires_at"])  # only the IDs are needed, not the turns
                .limit(limit)
                .stream(timeout=TIMEOUT_SECONDS)
            )
            if expired:
                batch = self._client.batch()
                for snapshot in expired:
                    batch.delete(snapshot.reference)
                batch.commit(timeout=TIMEOUT_SECONDS)
        except (GoogleAPIError, GoogleAuthError) as exc:
            raise SessionUnavailable() from exc
        return len(expired)


def firestore_session_store(project_id: str) -> FirestoreSessionStore:
    """The store for this project's `(default)` database, as the runtime account."""
    if not project_id:
        raise ValueError("Set GOOGLE_CLOUD_PROJECT for the Firestore session store")
    return FirestoreSessionStore(firestore.Client(project=project_id, database="(default)"))
