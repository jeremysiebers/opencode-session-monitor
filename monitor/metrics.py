"""Per-source, bounded, PII-free per-cycle instrumentation (INC027e METRICS).

This module is **instrumentation only**. Nothing here is on the status/READY/
stale decision path: every public method is internally guarded and a fault
self-disables the recorder, so a metrics problem can never turn a good poll into
UNKNOWN or crash a poll thread.

Bounded memory and PII:

* integer counters only, plus at most ``LATENCY_SAMPLE_CAP`` latency samples per
  operation (six frozen operations);
* no session id, title, message text, filesystem path, credential, request or
  response body is ever stored;
* the single free-form value is ONE capped and redacted sample of an
  unrecognized ``session.get`` outcome literal per cycle.

Percentiles are **nearest-rank** over the sorted retained samples and are
computed once, at :meth:`MetricsRecorder.end_cycle`.

``end_cycle`` returns a detached deep copy of a plain, JSON-serialisable dict.
Callers must treat it as immutable; mutating it can never change recorder state
and the recorder never retains a reference to the returned object.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from .redaction import cap_text, redact

# The six frozen GET operations (must stay identical to api.ALLOWED_OPERATIONS).
OPERATIONS = (
    "server.info",
    "session.list",
    "session.active",
    "session.get",
    "session.message.list",
    "session.permission.list",
)

ERROR_CLASSES = (
    "timeout",
    "rc_nonzero",
    "transport",
    "malformed_json",
    "refused",
    "other",
)

OUTCOME_KINDS = (
    "succeeded",
    "failed",
    "interrupted",
    "missing_key",
    "null",
    "unrecognized_str",
    "other_type",
)

STATUSES = ("UNKNOWN", "ERROR", "WAITING_PERMISSION", "BUSY", "READY", "IDLE")

UNKNOWN_REASON_CLASSES = (
    "outcome_missing",
    "session_error_timeout",
    "session_error_other",
    "archived",
    "retained_unknown",
)

LATENCY_SAMPLE_CAP = 512
MAX_JSONL_BYTES = 4 * 1024 * 1024
MAX_REASON_CHARS = 160
MAX_SAMPLE_CHARS = 40

CYCLE_OK = "OK"
CYCLE_ERROR = "ERROR"

# PII scrubbing for the only two free-form metrics values (discovery reason and
# one unrecognized-outcome sample): POSIX/Windows path tokens and exact session
# ids are replaced before the value is capped. Metrics never store a path or id.
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s;\"']*|/[^\s;\"':]+")
_SESSION_ID_RE = re.compile(r"\bses_[A-Za-z0-9_-]+")


def _sanitize_text(text, limit):
    """Redact secrets, scrub paths/session ids, then cap (metrics-only helper)."""
    if not isinstance(text, str):
        text = str(text)
    text = _PATH_RE.sub("[path]", text)
    text = _SESSION_ID_RE.sub("[id]", text)
    return cap_text(redact(text), limit)


def percentile_nearest_rank(sorted_values, pct):
    """Nearest-rank percentile of an already sorted list (None when empty)."""
    if not sorted_values:
        return None
    try:
        rank = int((int(pct) * len(sorted_values) + 99) // 100)
    except (TypeError, ValueError):
        return None
    rank = min(max(rank, 1), len(sorted_values))
    return sorted_values[rank - 1]


def classify_outcome(present, value, known):
    """Bucket one ``session.get`` outcome WITHOUT changing collector decisions.

    Absent key -> ``missing_key``; explicit null -> ``null``; non-string ->
    ``other_type``; an unknown string -> ``unrecognized_str``; otherwise the
    known literal (``succeeded`` / ``failed`` / ``interrupted``).
    """
    if not present:
        return "missing_key"
    if value is None:
        return "null"
    if not isinstance(value, str):
        return "other_type"
    if value not in known:
        return "unrecognized_str"
    return value


def classify_unknown_reason(snapshot):
    """Bucket an UNKNOWN SessionSnapshot reason without altering it.

    Purely additive: reads the already-derived ``status``/``reason`` fields.
    """
    if getattr(snapshot, "incomplete", False) or getattr(snapshot, "membership", "PRESENT") != "PRESENT":
        return "retained_unknown"
    reason = getattr(snapshot, "reason", "") or ""
    if reason == "session archived":
        return "archived"
    if reason.startswith("session error:"):
        if "timeout" in reason:
            return "session_error_timeout"
        return "session_error_other"
    if reason.startswith("outcome missing"):
        return "outcome_missing"
    return "retained_unknown"


def _utc_iso(epoch):
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OSError, ValueError, OverflowError, TypeError):
        return ""


class MetricsRecorder:
    """One bounded per-source recorder. One recorder per source/poll thread."""

    def __init__(self, source_key="default", poll_interval=3.0,
                 stale_threshold=15.0, clock=None, utcnow=None):
        self.source_key = source_key if isinstance(source_key, str) and source_key else "default"
        try:
            self.poll_interval = float(poll_interval)
        except (TypeError, ValueError):
            self.poll_interval = 3.0
        try:
            self.stale_threshold = float(stale_threshold)
        except (TypeError, ValueError):
            self.stale_threshold = 15.0
        self._clock = clock if callable(clock) else time.monotonic
        self._utcnow = utcnow if callable(utcnow) else time.time
        self._lock = threading.Lock()
        self._disabled = False
        self._cycle_id = 0
        self._started_mono = None
        self._started_utc = None
        self._last_finished_mono = None
        self._last = None
        self._clear_cycle()

    # ---- internal helpers ----------------------------------------------------

    def _clear_cycle(self):
        self._calls_total = 0
        self._by_operation = {op: 0 for op in OPERATIONS}
        self._errors = {name: 0 for name in ERROR_CLASSES}
        self._hydration_gets = 0
        self._hydration_failures = 0
        self._message_list_fallbacks = 0
        self._latencies = {op: [] for op in OPERATIONS}
        self._max_latency = {op: None for op in OPERATIONS}
        self._outcomes = {kind: 0 for kind in OUTCOME_KINDS}
        self._missing_busy = 0
        self._missing_idle = 0
        self._unrecognized_sample = None
        self._primaries_seen = 0
        self._polled = 0
        self._status_counts = {status: 0 for status in STATUSES}
        self._unknown_reasons = {name: 0 for name in UNKNOWN_REASON_CLASSES}
        self._discovery = {
            "pages": 0, "complete": False, "overflow": False,
            "primaries": 0, "reason": "",
        }

    def _disable(self):
        self._disabled = True

    def _latency_stats(self, operation):
        ordered = sorted(self._latencies[operation])
        count = self._by_operation[operation]
        observed_max = self._max_latency[operation]
        if observed_max is None:
            observed_max = ordered[-1] if ordered else None
        return {
            "count": count,
            # Percentiles are nearest-rank over the bounded retained window
            # (at most LATENCY_SAMPLE_CAP samples); ``max`` is the true maximum
            # over every observation in the cycle (O(1) memory).
            "p50": percentile_nearest_rank(ordered, 50),
            "p95": percentile_nearest_rank(ordered, 95),
            "max": observed_max,
            "samples": len(ordered),
            "truncated": count > len(ordered),
        }

    # ---- lifecycle -----------------------------------------------------------

    def begin_cycle(self):
        """Start a new per-cycle accumulation window. Returns the cycle id."""
        if self._disabled:
            return 0
        try:
            with self._lock:
                self._cycle_id += 1
                self._started_mono = self._clock()
                self._started_utc = self._utcnow()
                self._clear_cycle()
                return self._cycle_id
        except Exception:  # noqa: BLE001 - instrumentation must never propagate
            self._disable()
            return 0

    def end_cycle(self, state=CYCLE_OK):
        """Freeze the cycle into a detached JSON dict (or None when disabled)."""
        if self._disabled:
            return None
        try:
            with self._lock:
                if self._started_mono is None:
                    return None
                finished_mono = self._clock()
                finished_utc = self._utcnow()
                wall = max(0.0, float(finished_mono) - float(self._started_mono))
                wall_plus_poll = wall + self.poll_interval
                snapshot = {
                    "cycle": {
                        "id": self._cycle_id,
                        "state": state if state in (CYCLE_OK, CYCLE_ERROR) else CYCLE_OK,
                        "started_utc": _utc_iso(self._started_utc),
                        "finished_utc": _utc_iso(finished_utc),
                        "wall_seconds": round(wall, 3),
                        "seconds_since_finish": 0.0,
                        "poll_interval_seconds": self.poll_interval,
                        "wall_plus_poll_seconds": round(wall_plus_poll, 3),
                        "stale_threshold_seconds": self.stale_threshold,
                        # Explicit BLOCK rule: >= (never auto-tuned by the monitor).
                        "stale_breached": wall_plus_poll >= self.stale_threshold,
                    },
                    "calls": {
                        "total": self._calls_total,
                        "by_operation": dict(self._by_operation),
                        "errors_by_class": dict(self._errors),
                        "hydration_gets": self._hydration_gets,
                        "hydration_failures": self._hydration_failures,
                        "message_list_fallbacks": self._message_list_fallbacks,
                    },
                    "latency_ms": {op: self._latency_stats(op) for op in OPERATIONS},
                    "outcomes": dict(
                        self._outcomes,
                        missing_while_busy=self._missing_busy,
                        missing_while_idle=self._missing_idle,
                        unrecognized_sample=self._unrecognized_sample,
                    ),
                    "sessions": {
                        "primaries_seen": self._primaries_seen,
                        "polled": self._polled,
                        "status_counts": dict(self._status_counts),
                        "unknown_reason_classes": dict(self._unknown_reasons),
                    },
                    "discovery": dict(self._discovery),
                }
                self._last = snapshot
                self._last_finished_mono = finished_mono
                self._started_mono = None
                return copy.deepcopy(snapshot)
        except Exception:  # noqa: BLE001 - instrumentation must never propagate
            self._disable()
            return None

    def snapshot(self):
        """Last COMPLETED cycle (fresh seconds_since_finish) or None."""
        if self._disabled or self._last is None:
            return None
        try:
            with self._lock:
                snapshot = copy.deepcopy(self._last)
                finished = self._last_finished_mono
            if finished is not None:
                try:
                    age = max(0.0, float(self._clock()) - float(finished))
                    snapshot["cycle"]["seconds_since_finish"] = round(age, 3)
                except Exception:  # noqa: BLE001
                    pass
            return snapshot
        except Exception:  # noqa: BLE001
            return None

    @property
    def disabled(self):
        return self._disabled

    # ---- counters ------------------------------------------------------------

    def record_call(self, operation, ms, error_class=None):
        """One ``OpencodeApi._call`` observation: op + latency + error class.

        Called from the poll thread for discovery/hydration calls and from every
        ``monitor-fetch-*`` worker for the INC027f per-session fan-out. The
        shared lock keeps every counter and latency sample exact under that
        concurrency; this method never touches status state.
        """
        if self._disabled:
            return
        try:
            latency = None
            if isinstance(ms, (int, float)) and not isinstance(ms, bool):
                latency = round(max(0.0, float(ms)), 3)
            with self._lock:
                self._calls_total += 1
                if operation in self._by_operation:
                    self._by_operation[operation] += 1
                    buffer = self._latencies[operation]
                    if latency is not None:
                        current = self._max_latency[operation]
                        if current is None or latency > current:
                            self._max_latency[operation] = latency
                        if len(buffer) < LATENCY_SAMPLE_CAP:
                            buffer.append(latency)
                if error_class:
                    name = error_class if error_class in self._errors else "other"
                    self._errors[name] += 1
        except Exception:  # noqa: BLE001
            self._disable()

    def note_hydration(self, succeeded):
        """One session.get hydration attempt (discovery-stage only)."""
        if self._disabled:
            return
        try:
            with self._lock:
                self._hydration_gets += 1
                if succeeded is not True:
                    self._hydration_failures += 1
        except Exception:  # noqa: BLE001
            self._disable()

    def note_message_list_fallback(self):
        if self._disabled:
            return
        try:
            with self._lock:
                self._message_list_fallbacks += 1
        except Exception:  # noqa: BLE001
            self._disable()

    def note_outcome(self, kind, busy=None, sample=None):
        """Bucket one outcome-branch observation; never changes the decision."""
        if self._disabled:
            return
        try:
            with self._lock:
                if kind in self._outcomes:
                    self._outcomes[kind] += 1
                if kind in ("missing_key", "null"):
                    if busy is True:
                        self._missing_busy += 1
                    elif busy is False:
                        self._missing_idle += 1
                if (kind == "unrecognized_str" and sample is not None
                        and self._unrecognized_sample is None):
                    self._unrecognized_sample = _sanitize_text(sample, MAX_SAMPLE_CHARS)
        except Exception:  # noqa: BLE001
            self._disable()

    def note_primaries(self, count):
        if self._disabled:
            return
        try:
            number = int(count)
            with self._lock:
                if number > 0:
                    self._primaries_seen += number
        except Exception:  # noqa: BLE001
            self._disable()

    def note_polled(self):
        if self._disabled:
            return
        try:
            with self._lock:
                self._polled += 1
        except Exception:  # noqa: BLE001
            self._disable()

    def note_status(self, status):
        if self._disabled:
            return
        try:
            with self._lock:
                if status in self._status_counts:
                    self._status_counts[status] += 1
        except Exception:  # noqa: BLE001
            self._disable()

    def note_unknown_reason(self, reason_class):
        if self._disabled:
            return
        try:
            with self._lock:
                if reason_class in self._unknown_reasons:
                    self._unknown_reasons[reason_class] += 1
        except Exception:  # noqa: BLE001
            self._disable()

    def note_discovery(self, pages, complete, reason=""):
        if self._disabled:
            return
        try:
            with self._lock:
                self._discovery["pages"] = int(pages)
                self._discovery["complete"] = bool(complete)
                text = reason if isinstance(reason, str) else str(reason or "")
                self._discovery["reason"] = _sanitize_text(text, MAX_REASON_CHARS)
        except Exception:  # noqa: BLE001
            self._disable()

    def note_discovery_primaries(self, count):
        if self._disabled:
            return
        try:
            number = int(count)
            with self._lock:
                if number > 0:
                    self._discovery["primaries"] += number
        except Exception:  # noqa: BLE001
            self._disable()

    def note_overflow(self):
        if self._disabled:
            return
        try:
            with self._lock:
                self._discovery["overflow"] = True
        except Exception:  # noqa: BLE001
            self._disable()


class NullMetricsRecorder:
    """Neutral no-op recorder: used when no recorder is injected."""

    source_key = "default"

    def begin_cycle(self):
        return 0

    def end_cycle(self, state=CYCLE_OK):
        return None

    def snapshot(self):
        return None

    def record_call(self, operation, ms, error_class=None):
        return None

    def note_hydration(self, succeeded):
        return None

    def note_message_list_fallback(self):
        return None

    def note_outcome(self, kind, busy=None, sample=None):
        return None

    def note_primaries(self, count):
        return None

    def note_polled(self):
        return None

    def note_status(self, status):
        return None

    def note_unknown_reason(self, reason_class):
        return None

    def note_discovery(self, pages, complete, reason=""):
        return None

    def note_discovery_primaries(self, count):
        return None

    def note_overflow(self):
        return None


class JsonlMetricsWriter:
    """Opt-in, append-only, fixed-size-capped JSONL writer (one line/cycle).

    Disabled by default: it is only constructed when ``--metrics-jsonl PATH`` is
    supplied. Every failure (bad path, unwritable file, size cap reached,
    unserialisable snapshot) disables the writer instead of raising, and writes
    happen strictly after ``end_cycle`` so file I/O can never inflate a cycle's
    measured wall time.
    """

    def __init__(self, path, max_bytes=MAX_JSONL_BYTES):
        if not isinstance(path, str) or not path:
            raise ValueError("metrics JSONL path must be a non-empty string")
        self.path = path
        try:
            self.max_bytes = max(0, int(max_bytes))
        except (TypeError, ValueError):
            self.max_bytes = MAX_JSONL_BYTES
        self._lock = threading.Lock()
        self._disabled = False
        self._last_cycle = {}
        try:
            self._size = os.path.getsize(path)
        except OSError:
            self._size = 0

    @property
    def disabled(self):
        return self._disabled

    @property
    def size(self):
        return self._size

    def write(self, source_key, snapshot):
        """Append one line for one completed cycle; False when not written.

        The archived line carries a top-level ``"source"`` = the same source key
        used for dedupe, so one shared JSONL file remains unambiguous even when
        two per-source recorders both report cycle id 1. The supplied snapshot
        (and therefore /status.json semantics) is never mutated.
        """
        if self._disabled or not isinstance(snapshot, dict):
            return False
        cycle = snapshot.get("cycle")
        cycle_id = cycle.get("id") if isinstance(cycle, dict) else None
        if not isinstance(cycle_id, int) or isinstance(cycle_id, bool):
            return False
        source = source_key if isinstance(source_key, str) and source_key else "default"
        with self._lock:
            if self._disabled:
                return False
            if self._last_cycle.get(source, -1) >= cycle_id:
                return False  # never duplicate a completed cycle
            try:
                record = dict(snapshot)
                record["source"] = source
                line = json.dumps(record, sort_keys=True, separators=(",", ":"))
                raw = (line + "\n").encode("utf-8")
            except (TypeError, ValueError):
                self._disabled = True
                return False
            if self._size + len(raw) > self.max_bytes:
                self._disabled = True  # fixed cap: stop, never grow past it
                return False
            try:
                with open(self.path, "ab") as handle:
                    handle.write(raw)
            except OSError:
                self._disabled = True
                return False
            self._size += len(raw)
            self._last_cycle[source] = cycle_id
            return True
