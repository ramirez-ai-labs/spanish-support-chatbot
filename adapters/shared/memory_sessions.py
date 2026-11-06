"""SessionStore in memory, for tests and local runs. Records go through JSON, so whatever a real
store couldn't keep, this one can't either."""

import json
import threading

from core.sessions import Session, SessionConflict, from_record, to_record


class MemorySessionStore:
    def __init__(self) -> None:
        self._records: dict[str, str] = {}
        self._lock = threading.Lock()

    def load(self, session_id: str, now: float) -> Session | None:
        with self._lock:
            stored = self._records.get(session_id)
        if stored is None:
            return None
        session = from_record(json.loads(stored))
        return session if session.expires_at > now else None

    def save(self, session: Session) -> None:
        with self._lock:
            stored = self._records.get(session.id)
            current = json.loads(stored)["version"] if stored is not None else 0
            if current != session.version:
                raise SessionConflict()
            record = {**to_record(session), "version": session.version + 1}
            self._records[session.id] = json.dumps(record)
        session.version += 1

    def delete_expired(self, now: float, limit: int) -> int:
        with self._lock:
            expired = [
                session_id
                for session_id, stored in self._records.items()
                if json.loads(stored)["expires_at"] <= now
            ][:limit]
            for session_id in expired:
                del self._records[session_id]
        return len(expired)
