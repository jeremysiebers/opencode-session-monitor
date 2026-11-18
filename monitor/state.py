"""Persistent dedup baseline plus in-memory ready-pending state.

INC027d: every key is namespaced by ``(source_key, session_id)`` so two sources
or two primary sessions can never share a READY baseline. The on-disk format is
STATE_SCHEMA 2 (nested ``last_seen``) with an automatic read migration from
schema 1 (flat map, loaded under a legacy default source). Writes are always
schema 2 and atomic (temp file + fsync + os.replace).

Signatures accept an optional ``source_key`` so the pre-INC027d single-argument
callers keep working unchanged (they use the legacy default source). Nothing
here writes to OpenCode state, the home directory or any Git worktree.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

STATE_SCHEMA = 2
LEGACY_SCHEMA = 1
LEGACY_SOURCE = "default"


class StateStore:
    """Atomically persisted ``(source_key, session_id) -> last_seen_message_id``."""

    def __init__(self, path, source_key=None):
        if not isinstance(path, str) or not path:
            raise ValueError("state path must be a non-empty string")
        self._path = path
        self._source_key = source_key if isinstance(source_key, str) and source_key else LEGACY_SOURCE
        self._lock = threading.RLock()
        self._last_seen = {}
        self._load()

    def _key(self, session_id, source_key=None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id required")
        source = source_key if isinstance(source_key, str) and source_key else self._source_key
        return (source, session_id)

    def _load(self):
        with self._lock:
            if not os.path.exists(self._path):
                return
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                # Unreadable/corrupt state: start empty. The first eligible
                # observation initialises a baseline without raising READY, so a
                # corrupt file can never produce a duplicate READY.
                return
            if not isinstance(data, dict):
                return
            last_seen = data.get("last_seen")
            if not isinstance(last_seen, dict):
                return
            schema = data.get("schema", LEGACY_SCHEMA)
            if schema == STATE_SCHEMA:
                # Nested: {source: {session_id: message_id}}
                for source, mapping in last_seen.items():
                    if not isinstance(source, str) or not isinstance(mapping, dict):
                        continue
                    for sid, mid in mapping.items():
                        if isinstance(sid, str) and sid and isinstance(mid, str):
                            self._last_seen[(source, sid)] = mid
            elif schema == LEGACY_SCHEMA:
                # Flat v1 map: {session_id: message_id} -> legacy default source.
                for sid, mid in last_seen.items():
                    if isinstance(sid, str) and sid and isinstance(mid, str):
                        self._last_seen[(LEGACY_SOURCE, sid)] = mid

    def last_seen(self, session_id, source_key=None):
        """Return the stored baseline (str) or None if never observed."""
        with self._lock:
            return self._last_seen.get(self._key(session_id, source_key))

    def record(self, session_id, message_id, source_key=None):
        """Persist a new baseline atomically, then update the in-memory copy.

        The in-memory value advances only after a successful durable write, so a
        failed write cannot silently consume a detection (which would allow a
        restart to raise a duplicate READY).
        """
        if not isinstance(message_id, str):
            raise ValueError("message_id required")
        key = self._key(session_id, source_key)
        with self._lock:
            updated = dict(self._last_seen)
            updated[key] = message_id
            self._atomic_write(self._nested(updated))
            self._last_seen = updated

    @staticmethod
    def _nested(mapping):
        """Render the flat composite-key map as a schema-2 document."""
        nested = {}
        for (source, sid), mid in mapping.items():
            nested.setdefault(source, {})[sid] = mid
        return {"schema": STATE_SCHEMA, "last_seen": nested}

    def _atomic_write(self, data):
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".last_seen.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


class ReadyPending:
    """In-memory mapping ``(source_key, session_id) -> triggering message id``.

    Deliberately not persisted: losing it on restart is safe because the
    persisted baseline already prevents the same answer being raised again.
    Optional ``source_key`` arguments default to the legacy default source so
    the pre-027d single-argument callers keep working.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    @staticmethod
    def _key(session_id, source_key=None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id required")
        source = source_key if isinstance(source_key, str) and source_key else LEGACY_SOURCE
        return (source, session_id)

    def set(self, session_id, message_id, source_key=None):
        with self._lock:
            self._pending[self._key(session_id, source_key)] = message_id

    def clear(self, session_id, source_key=None):
        with self._lock:
            self._pending.pop(self._key(session_id, source_key), None)

    def has(self, session_id, source_key=None):
        with self._lock:
            return self._key(session_id, source_key) in self._pending

    def retain(self, live_keys):
        """GC pending entries not in ``live_keys`` (composite keys), at final drop only."""
        keep = set(live_keys)
        with self._lock:
            for key in list(self._pending):
                if key not in keep:
                    self._pending.pop(key, None)

    def snapshot(self):
        with self._lock:
            return dict(self._pending)
