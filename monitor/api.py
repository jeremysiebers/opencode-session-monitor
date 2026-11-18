"""GET-only OpenCode CLI client with structurally frozen argv.

The only supported transport is::

    opencode api <operationId> [--param k=v ...]

Operation ids are literals selected by the named methods below and checked
against a frozen allowlist inside the single private ``_call`` choke point.
There is deliberately no public function that accepts an operation id, HTTP
method, path, request body or arbitrary flag from configuration or input, so a
new caller cannot reach any mutating operation through this module.

Auth is handled entirely by the CLI itself (it reads its own local service
state); no credential is ever read, copied or passed by this monitor.
"""
from __future__ import annotations

import json
import re
import subprocess
import time

from .metrics import NullMetricsRecorder
from .redaction import safe_error

# Exactly the read operations this monitor is permitted to issue.
ALLOWED_OPERATIONS = frozenset({
    "server.info",
    "session.list",
    "session.active",
    "session.get",
    "session.message.list",
    "session.permission.list",
})

_OP_SERVER_INFO = "server.info"
_OP_SESSION_LIST = "session.list"
_OP_SESSION_ACTIVE = "session.active"
_OP_SESSION_GET = "session.get"
_OP_SESSION_MESSAGE_LIST = "session.message.list"
_OP_SESSION_PERMISSION_LIST = "session.permission.list"

_ALLOWED_PARAM_KEYS = frozenset({"sessionID", "limit", "order", "cursor", "type"})
_ALLOWED_ORDER = frozenset({"asc", "desc"})

# Belt-and-suspenders tokens that must never appear in a constructed argv.
# These are checked at runtime; none of them is emitted by a frozen template.
_FORBIDDEN_ARGV_TOKENS = frozenset({
    "-d", "--data", "--header", "-H", "-X", "--request", "--method",
    "POST", "PUT", "DELETE", "PATCH",
})

_SESSION_RE = re.compile(r"^ses_[A-Za-z0-9_-]{1,64}$")


class MonitorApiError(Exception):
    """Raised for any transport, auth, timeout or response-shape problem.

    The collector maps this to UNKNOWN for the affected team; it never exposes
    raw response bodies or credentials.
    """


class MalformedApiResponse(MonitorApiError):
    """CLI exited 0 but stdout was not one complete JSON document.

    This is the signature of the INC027 §11 failure (rc 0, empty stderr, stdout
    cut mid-string). It is a subclass of MonitorApiError so every existing
    fail-closed path is unchanged, but it is distinct from transport/auth errors
    so the collector can apply exactly one bounded retry with a smaller page.
    Truncated JSON is never parsed into partial state.
    """


class OpencodeApi:
    """Thin GET-only wrapper around the ``opencode api`` CLI.

    ``runner`` is injectable so offline tests can substitute a recording fake
    CLI. In production it defaults to :func:`subprocess.run`.
    """

    def __init__(self, binary, server_url=None, timeout=15.0, runner=None,
                 metrics=None, clock=None):
        if not isinstance(binary, str) or not binary or "\x00" in binary:
            raise ValueError("binary must be a non-empty absolute path string")
        if server_url is not None and not isinstance(server_url, str):
            raise ValueError("server_url must be a string or None")
        self.binary = binary
        self.server_url = server_url or None
        self.timeout = float(timeout)
        self._runner = runner if runner is not None else self._default_runner
        # INC027e METRICS: additive, neutral by default. The recorder is only
        # asked to observe the frozen GET call; it can never alter argv, the
        # timeout, the return flow or any exception raised below.
        self._metrics = metrics if metrics is not None else NullMetricsRecorder()
        self._clock = clock if callable(clock) else time.monotonic

    @staticmethod
    def _default_runner(argv, timeout):
        """Run the frozen argv with no shell and no inherited stdin.

        Stdout/stderr are captured as raw bytes. They are decoded explicitly as
        ``utf-8-sig`` in :meth:`_decode_stdout`, so a UTF-8 BOM emitted by a
        native Windows ``opencode.exe`` can never break ``json.loads``. There is
        deliberately no shell wrapper of any kind: ``argv[0]`` is the
        user-supplied absolute executable path (INC027e).
        """
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            check=False,
            timeout=timeout,
        )

    @staticmethod
    def _decode_stdout(raw):
        """Decode CLI output as UTF-8, stripping a leading BOM if present.

        The native Windows interop source may emit a UTF-8 BOM before the JSON
        document; POSIX sources do not. Both are handled by one shared choke
        point. Non-text input is returned unchanged so ``json.loads`` fails
        closed (there is never a partial/fabricated payload).
        """
        if isinstance(raw, bytes):
            return raw.decode("utf-8-sig", errors="replace")
        if isinstance(raw, str):
            return raw.lstrip("\ufeff")
        return raw

    @staticmethod
    def _validate_session_id(session_id):
        if not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
            raise MonitorApiError("invalid session id")

    def _call(self, operation, params):
        """Single choke point that builds and executes a frozen GET argv.

        Each observation is timed with an injectable monotonic clock and passed
        to the optional recorder in ``finally``. Instrumentation is total: any
        metrics fault is swallowed here and can never turn a good GET into an
        error or change the returned payload/exception.
        """
        started = self._clock()
        error_class = None
        recorded = isinstance(operation, str) and operation in ALLOWED_OPERATIONS
        try:
            if operation not in ALLOWED_OPERATIONS:
                raise MonitorApiError("operation refused")
            if not isinstance(params, dict):
                error_class = "refused"
                raise MonitorApiError("params refused")
            for key, value in params.items():
                if key not in _ALLOWED_PARAM_KEYS:
                    error_class = "refused"
                    raise MonitorApiError("parameter refused")
                if not isinstance(value, str) or not value or "\x00" in value or value.startswith("-"):
                    error_class = "refused"
                    raise MonitorApiError("parameter value refused")

            argv = [self.binary, "api"]
            if self.server_url:
                argv += ["--server", self.server_url]
            argv.append(operation)
            for key in sorted(params):
                argv += ["--param", "%s=%s" % (key, params[key])]

            # Final structural guard: refuse if any frozen token is a forbidden flag.
            for token in argv:
                if token in _FORBIDDEN_ARGV_TOKENS:
                    error_class = "refused"
                    raise MonitorApiError("forbidden argv token refused")

            try:
                result = self._runner(argv, self.timeout)
            except subprocess.TimeoutExpired:
                error_class = "timeout"
                raise MonitorApiError("api timeout")
            except OSError as exc:
                error_class = "transport"
                raise MonitorApiError("api transport error: " + safe_error(str(exc)))
            except MonitorApiError:
                error_class = "other"
                raise
            except Exception as exc:  # noqa: BLE001 - one failure must not crash the collector
                error_class = "other"
                raise MonitorApiError("api error: " + safe_error(str(exc)))

            stdout = getattr(result, "stdout", b"") or b""
            stderr = getattr(result, "stderr", b"") or b""
            code = getattr(result, "returncode", 1)
            stderr_text = self._decode_stdout(stderr)
            if code != 0:
                # rc!=0 always fails closed; the affected source/team becomes UNKNOWN.
                error_class = "rc_nonzero"
                raise MonitorApiError("api status %s %s" % (safe_error(str(code)), safe_error(stderr_text)))
            stdout_text = self._decode_stdout(stdout)
            if not isinstance(stdout_text, str):
                error_class = "malformed_json"
                raise MonitorApiError("malformed api response")
            try:
                payload = json.loads(stdout_text)
            except (ValueError, TypeError):
                # Truncated / partial stdout is never treated as a complete reply.
                # The distinct type lets the collector retry once with a smaller
                # page; if that also fails, this still fails closed to UNKNOWN.
                error_class = "malformed_json"
                raise MalformedApiResponse("malformed api response")
            if not isinstance(payload, dict):
                error_class = "other"
                raise MonitorApiError("api response not an object")
            return payload
        finally:
            if recorded:
                try:
                    elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
                    self._metrics.record_call(operation, elapsed_ms, error_class)
                except Exception:  # noqa: BLE001 - metrics must never break a GET
                    pass

    # ---- the only public operations -------------------------------------------------

    def server_info(self):
        return self._call(_OP_SERVER_INFO, {})

    def session_list(self, cursor=None, limit=None):
        """One read-only ``session.list`` page.

        ``cursor`` and ``limit`` are already in the frozen ``_ALLOWED_PARAM_KEYS``
        allowlist (INC027d) and are validated by the single ``_call`` choke point:
        values must be non-empty strings that do not look like flags. Callers can
        therefore never smuggle a new operation or mutating flag through paging.
        """
        params = {}
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
                raise MonitorApiError("invalid session list limit")
            params["limit"] = str(limit)
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise MonitorApiError("invalid session list cursor")
            params["cursor"] = cursor
        return self._call(_OP_SESSION_LIST, params)

    def session_active(self):
        return self._call(_OP_SESSION_ACTIVE, {})

    def session_get(self, session_id):
        self._validate_session_id(session_id)
        return self._call(_OP_SESSION_GET, {"sessionID": session_id})

    def session_message_list(self, session_id, limit=20, order="desc"):
        self._validate_session_id(session_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 20):
            raise MonitorApiError("invalid message limit")
        if order not in _ALLOWED_ORDER:
            raise MonitorApiError("invalid message order")
        return self._call(
            _OP_SESSION_MESSAGE_LIST,
            {"sessionID": session_id, "limit": str(limit), "order": order},
        )

    def session_permission_list(self, session_id):
        self._validate_session_id(session_id)
        return self._call(_OP_SESSION_PERMISSION_LIST, {"sessionID": session_id})


def next_list_cursor(payload):
    """Extract the next-page cursor from a ``session.list`` payload.

    Returns a non-empty string when another page exists, otherwise ``None``
    (terminal). Accepts both the ``cursor``-as-string shape used by the operator
    E3 walk and the ``cursor.next`` / top-level ``next`` envelope shapes, without
    ever inventing a cursor value.
    """
    if not isinstance(payload, dict):
        return None
    cursor = payload.get("cursor")
    if isinstance(cursor, str):
        return cursor or None
    if isinstance(cursor, dict):
        nxt = cursor.get("next")
        if isinstance(nxt, str) and nxt:
            return nxt
        return None
    nxt = payload.get("next")
    if isinstance(nxt, str) and nxt:
        return nxt
    return None
