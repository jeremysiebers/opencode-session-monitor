"""Recording fake ``opencode api`` CLI for fully offline tests.

It records every argv it is handed (so tests can prove no mutating call was
ever made) and returns canned envelopes keyed by operation and session id.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

from monitor.api import ALLOWED_OPERATIONS


class FakeCli:
    """Callable with the same signature as ``OpencodeApi._default_runner``."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.errors = {}
        # INC027f concurrency instrumentation (default off/neutral):
        # * in_flight / max_in_flight prove the bounded worker fan-out;
        # * delay simulates per-CLI latency without any network/process call;
        # * block_event, when set, holds each call until the test releases it.
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = 0.0
        self.block_event = None
        # INC027d cursor paging: list of page payload dicts, selected by the
        # cursor each previous page advertises. None -> legacy single-page mode.
        self.session_pages = None

    # -- concurrency-safe call boundary ---------------------------------------
    def _begin(self, argv):
        """Record argv + enter the in-flight window (thread-safe)."""
        with self._lock:
            self.calls.append(list(argv))
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight
        delay = self.delay
        if delay:
            time.sleep(delay)
        event = self.block_event
        if event is not None:
            # Bounded wait so a test bug cannot hang the suite forever.
            event.wait(10.0)

    def _end(self):
        with self._lock:
            self.in_flight -= 1

    # -- configuration helpers -------------------------------------------------
    def set(self, key, value):
        """Set a canned response. Key is an op string or (op, sessionID) tuple."""
        self.responses[key] = value
        return self

    def error(self, key, exception):
        """Make a matching call raise ``exception`` (transport/timeout/error)."""
        self.errors[key] = exception
        return self

    def set_active(self, session_ids):
        return self.set("session.active", {
            "data": {sid: {"type": "running"} for sid in session_ids}
        })

    def set_session_list(self, session_ids):
        self.session_pages = None
        return self.set("session.list", {"data": [{"id": sid} for sid in session_ids]})

    def set_session_pages(self, pages):
        """Install a cursor-paged session.list sequence.

        ``pages`` is a list of payload dicts, e.g.::

            [
                {"data": [{"id": "ses_a"}], "cursor": "cur-1"},
                {"data": [{"id": "ses_b"}]},          # no cursor -> terminal
            ]

        The next request with ``cursor=cur-1`` returns page 2. An unknown or
        repeated cursor resolves to an empty terminal page, which lets tests
        exercise cycle/terminal handling.
        """
        self.session_pages = pages
        return self

    @staticmethod
    def _paged_list_page(pages, cursor):
        if not pages:
            return {"data": []}
        if not cursor:
            return pages[0]
        for index, page in enumerate(pages):
            advertised = page.get("cursor")
            if isinstance(advertised, dict):
                advertised = advertised.get("next")
            if advertised == cursor:
                return pages[index + 1] if index + 1 < len(pages) else {"data": []}
        return {"data": []}

    def set_session(self, sid, info):
        return self.set(("session.get", sid), {"data": info})

    def set_messages(self, sid, messages):
        return self.set(("session.message.list", sid), {"data": messages})

    def set_permissions(self, sid, data):
        return self.set(("session.permission.list", sid), {"data": data})

    # -- call handling ---------------------------------------------------------
    @staticmethod
    def _parse(argv):
        operation = None
        params = {}
        for index, token in enumerate(argv):
            if token in ALLOWED_OPERATIONS:
                operation = token
            if token == "--param" and index + 1 < len(argv):
                key, _sep, value = argv[index + 1].partition("=")
                params[key] = value
        return operation, params

    def _default(self, operation, sid):
        if operation == "session.active":
            return {"data": {}}
        if operation == "session.list":
            return {"data": []}
        if operation == "session.permission.list":
            return {"data": []}
        if operation == "session.message.list":
            return {"data": []}
        if operation == "session.get":
            return {"data": {
                "id": sid,
                "outcome": "succeeded",
                "time": {"created": 1000, "updated": 1000},
                "title": "",
                "location": {"directory": "/w/unconfigured"},
            }}
        return {"data": {}}

    def __call__(self, argv, timeout=None):
        self._begin(argv)
        try:
            return self._handle(argv)
        finally:
            self._end()

    def _handle(self, argv):
        operation, params = self._parse(argv)
        sid = params.get("sessionID")
        key = (operation, sid) if sid else operation

        if key in self.errors:
            raise self.errors[key]
        if operation in self.errors:
            raise self.errors[operation]

        if operation == "session.list" and self.session_pages is not None:
            value = self._paged_list_page(self.session_pages, params.get("cursor"))
        elif key in self.responses:
            value = self.responses[key]
        elif operation in self.responses:
            value = self.responses[operation]
        else:
            value = self._default(operation, sid)

        stdout = value if isinstance(value, str) else json.dumps(value)
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class WindowsFakeCli(FakeCli):
    """A FakeCli that emulates a native-Windows ``opencode.exe`` stdout path.

    Adds the transport quirks observed/allowed by INC027e A1 without any real
    binary or OS call: UTF-8 BOM + CRLF-wrapped JSON, non-zero exit codes,
    injected timeouts/transport errors and arbitrarily large responses. The
    recorded ``argv`` is still the frozen GET argv (no shell wrapper is added).
    """

    def __init__(self, bom=False, crlf=False):
        super().__init__()
        self.bom = bom
        self.crlf = crlf
        self.returncodes = {}
        self.raises = {}

    def set_rc(self, key, rc):
        """Make calls for an op or (op, sessionID) exit with ``rc``."""
        self.returncodes[key] = rc
        return self

    def will_raise(self, key, exception):
        """Make calls for an op or (op, sessionID) raise ``exception``."""
        self.raises[key] = exception
        return self

    def _handle(self, argv):
        operation, params = self._parse(argv)
        sid = params.get("sessionID")
        key = (operation, sid) if sid else operation

        if key in self.raises:
            raise self.raises[key]
        if operation in self.raises:
            raise self.raises[operation]

        if operation == "session.list" and self.session_pages is not None:
            value = self._paged_list_page(self.session_pages, params.get("cursor"))
        elif key in self.responses:
            value = self.responses[key]
        elif operation in self.responses:
            value = self.responses[operation]
        else:
            value = self._default(operation, sid)

        stdout = value if isinstance(value, str) else json.dumps(value)
        if self.crlf:
            stdout = "\r\n" + stdout.replace("\n", "\r\n") + "\r\n"
        if self.bom:
            stdout = "\ufeff" + stdout
        rc = self.returncodes.get(key, self.returncodes.get(operation, 0))
        stderr = "windows get failed" if rc != 0 else ""
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)
