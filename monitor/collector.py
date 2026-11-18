"""Polling collector: discovery, per-session cards, dedup and fail-closed views.

INC027d model: ``source -> team/worktree -> 1..N primary sessions -> children``.

* One paged, deduplicated ``session.list`` walk per poll. A COMPLETE walk (terminal
  cursor before the cap, no cycle, well-formed items) admits new primaries; any
  INCOMPLETE condition fails closed: no new candidates, existing cards kept
  visible as UNKNOWN with ready_pending preserved.
* Each valid primary is its own stacked SessionSnapshot with independent status,
  message/activity, dedup baseline and Gezien. A session's UNKNOWN never disables
  a sibling. Children are advisory only and never become cards.
* Retention on COMPLETE discovery: PRESENT -> GRACE(2) -> DROPPED, with the
  last_seen baseline never deleted (a returning id cannot re-raise READY).

Status precedence (per session, first match wins)::

    UNKNOWN -> WAITING_PERMISSION -> BUSY -> ERROR -> READY -> IDLE

INC027i: a current action state (permission pending or active) outranks the
previous turn's terminal outcome, so a retry of a failed session is shown as
WAITING_PERMISSION/BUSY instead of ERROR. ERROR stays above READY because a
failed turn can emit an assistant message with no text; only a terminal failure
that is no longer active remains ERROR.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass, field

from .api import MalformedApiResponse, MonitorApiError, next_list_cursor
from .messages import is_assistant, message_id, message_time_ms
from .metrics import (
    NullMetricsRecorder, classify_outcome, classify_unknown_reason,
)
from .paths import same_path
from .redaction import DISPLAY_SUMMARY_CHARS, cap_text, safe_error, summarize_messages

UNKNOWN = "UNKNOWN"
ERROR = "ERROR"
WAITING_PERMISSION = "WAITING_PERMISSION"
BUSY = "BUSY"
READY = "READY"
IDLE = "IDLE"

# INC027j aggregate precedence: operational states first (action states, then
# BUSY, then IDLE), with a historical ERROR below every operational state and
# UNKNOWN last. ERROR is the aggregate only when no operational state exists
# and at least one session is actually ERROR; UNKNOWN stays filtered out of
# every aggregate per AC-d2 and is the aggregate only when all sessions are
# UNKNOWN. ``status_rank`` stays "lower = higher precedence".
STATUS_ORDER = (WAITING_PERMISSION, READY, BUSY, IDLE, ERROR, UNKNOWN)
MAX_REASON_CHARS = 400

# GET page size for session.message.list. INC027 §11 showed limit=20 returning
# ~260 KB stdout truncated mid-string on long-history sessions; order=desc keeps
# the newest assistant id first even on a small page.
MESSAGE_LIMIT = 5
MESSAGE_FALLBACK_LIMIT = 1

# Discovery caps — derived from measured CLI behaviour, not from arbitrary
# completeness limits:
#   * session.list --param limit=50 is HONORED; a full walk of a large
#     installation took ~18 calls / 1.6 s wall; p95 page latency ~97 ms;
#     cursor.next behaved correctly; no duplicate session ids were seen.
#   * Several hundred sessions across a dozen worktrees were walked; a
#     full walk returned a terminal cursor and no duplicate session ids.
# MAX_PAGES=64 gives >3x headroom over the observed ~18 pages so a legitimate
# growth burst still terminates; a terminal cursor is REQUIRED for COMPLETE and
# a repeated cursor (cycle) is INCOMPLETE. MAX_UNIQUE is derived from the same
# page geometry.
#
# Primary cap: a single worktree was measured with 23 live primaries, which an
# earlier value of 16 refused outright. 32 accepts that maximum with bounded
# headroom and stays far below MAX_UNIQUE. More than 32 primaries is still an
# explicit overflow -> that team UNKNOWN with no new candidates admitted
# (fail-closed), never a partial winner; sibling teams and other sources are
# unaffected. 32 is a documented, deliberate cap — not an auto-tuned value.
PAGE_LIMIT = 50
MAX_PAGES = 64
MAX_UNIQUE = MAX_PAGES * PAGE_LIMIT
MAX_PRIMARIES_PER_TEAM = 32
MAX_CHILDREN = 16
GRACE_LIMIT = 2

# Bounded fetch concurrency.
# Measurement on a 23-primary worktree: 83 CLI calls/cycle with
# 159-177 ms/call (~16.2-17.7 s wall+poll, above the 15 s stale threshold).
# The per-session fan-out is exactly three independent, read-only GETs
# (session.get + permission.list + message.list), so at most
# FETCH_CONCURRENCY of those run in parallel. This is a worker cap, not a
# deadline: it changes neither the call budget (1 + pages + 3 per admitted
# primary) nor any argv/timeout. Discovery, hydration and every state mutation
# (store.record, ready.set, info_cache writes) stay on the poll thread.
#
# Depth/width table at the 23-primary cap (measured 83 calls):
#   stage                         calls   width
#   session.active                    1   1 (poll thread)
#   session.list pages               13   1 (poll thread)
#   per-primary get/permission/msg   69   <= FETCH_CONCURRENCY workers
#   total                            83   3 calls per session, 8 sessions deep
FETCH_CONCURRENCY = 8

# v2 session.get outcome values understood by this monitor.
KNOWN_OUTCOMES = frozenset({"succeeded", "failed", "interrupted"})

# Retention states.
PRESENT = "PRESENT"
GRACE = "GRACE"
DROPPED = "DROPPED"

# Classification outcomes (pure functions below).
KIND_PRIMARY = "primary"
KIND_CHILD = "child"
KIND_UNKNOWN = "unknown"
KIND_SKIP = "skip"
KIND_NEEDS_HYDRATE = "needs_hydrate"


def status_rank(status):
    """Lower rank = higher precedence. Unknown codes rank lowest priority."""
    try:
        return STATUS_ORDER.index(status)
    except ValueError:
        return len(STATUS_ORDER)


def effective_status(snapshot, stale_threshold, now=None):
    """Return UNKNOWN when a snapshot is absent or older than the stale window."""
    if snapshot is None:
        return UNKNOWN
    if now is None:
        now = time.monotonic()
    if (now - snapshot.updated_at) > stale_threshold:
        return UNKNOWN
    return snapshot.status


def aggregate_status(sessions):
    """Team-level compatibility aggregate: highest-precedence non-UNKNOWN.

    Per AC-d2 there is NO 'any UNKNOWN -> whole team UNKNOWN' collapse and no
    single-winner min-rank over UNKNOWN: a healthy sibling keeps a healthy
    aggregate. With no sessions (or all UNKNOWN) the aggregate is UNKNOWN.
    INC027j precedence is WAITING_PERMISSION, READY, BUSY, IDLE, ERROR: a
    historical ERROR never hides a current operational (action/BUSY/IDLE)
    sibling. ERROR is the aggregate only when no operational state exists and
    at least one session is actually ERROR, and it stays visible as its own
    card and badge.
    """
    statuses = [s.status for s in sessions if getattr(s, "status", UNKNOWN) != UNKNOWN]
    if not statuses:
        return UNKNOWN
    return min(statuses, key=status_rank)


# ---------------------------------------------------------------------------
# Pure classification / visibility / deny helpers (independently unit-testable)
# ---------------------------------------------------------------------------

def classify_parent(item):
    """F1-corrected primary/child discriminator, from item metadata only.

    ``parentID`` ABSENT => primary candidate; a non-empty STRING => child;
    explicit null, empty/non-string or a non-dict item => UNKNOWN (fail closed).
    """
    if not isinstance(item, dict):
        return KIND_UNKNOWN
    if "parentID" not in item:
        return KIND_PRIMARY
    value = item["parentID"]
    if isinstance(value, str) and value:
        return KIND_CHILD
    return KIND_UNKNOWN


def extract_directory(item_or_info):
    """Return a non-empty directory string from item/session.get data, else None."""
    if not isinstance(item_or_info, dict):
        return None
    location = item_or_info.get("location")
    if isinstance(location, dict):
        directory = location.get("directory")
        if isinstance(directory, str) and directory:
            return directory
    directory = item_or_info.get("directory")
    if isinstance(directory, str) and directory:
        return directory
    return None


def extract_archived(item_or_info):
    """True = archived, False = not archived/absent, None = ambiguous/unknown."""
    if not isinstance(item_or_info, dict):
        return False
    for key in ("archived", "isArchived", "is_archived", "archive"):
        if key in item_or_info:
            value = item_or_info[key]
            if isinstance(value, bool):
                return value
            return None
    return False


def is_denied(session_id, deny_ids):
    """PD4: exact session-ID deny only — never title, agent name or substring."""
    return isinstance(session_id, str) and session_id in frozenset(deny_ids or ())


def classify_candidate(item, team_worktree, deny_ids, hydrated=None):
    """Return ``(kind, session_id)`` for one list item against one team.

    ``hydrated`` is an optional ``session_id -> info`` map used only when the
    list item lacks a directory (session.get hydration). Nothing is inferred
    from agent names and no session id is ever fabricated.
    """
    if not isinstance(item, dict):
        return KIND_UNKNOWN, None
    sid = item.get("id")
    if not isinstance(sid, str) or not sid:
        return KIND_UNKNOWN, None
    if is_denied(sid, deny_ids):
        return KIND_SKIP, sid
    archived = extract_archived(item)
    if archived is True:
        return KIND_SKIP, sid
    parent = classify_parent(item)
    directory = extract_directory(item)
    if directory is None and isinstance(hydrated, dict):
        directory = extract_directory(hydrated.get(sid))
    if parent == KIND_CHILD:
        return KIND_CHILD, sid
    if parent == KIND_UNKNOWN or archived is None:
        return KIND_UNKNOWN, sid
    # parentID absent, not archived, not denied -> primary iff worktree matches.
    if directory is None:
        return KIND_NEEDS_HYDRATE, sid
    if not same_path(directory, team_worktree):
        return KIND_SKIP, sid
    return KIND_PRIMARY, sid


# ---------------------------------------------------------------------------
# Snapshot / view value types
# ---------------------------------------------------------------------------

@dataclass
class SessionSnapshot:
    session_id: str
    source_key: str
    team_key: str
    team_label: str
    status: str = UNKNOWN
    reason: str = ""
    last_activity_ts: float | None = None
    title: str = ""
    summary: str = ""
    awaiting: bool = False
    updated_at: float = 0.0
    membership: str = PRESENT
    grace_count: int = 0
    child_ids: tuple = field(default_factory=tuple)
    child_busy: bool = False
    incomplete: bool = False


@dataclass
class TeamSnapshot:
    key: str
    label: str
    status: str = UNKNOWN
    reason: str = ""
    last_activity_ts: float | None = None
    title: str = ""
    summary: str = ""
    session_ids: tuple = field(default_factory=tuple)
    awaiting: bool = False
    updated_at: float = 0.0
    sessions: tuple = field(default_factory=tuple)
    discovery_complete: bool = True
    discovery_reason: str = ""
    overflow: bool = False
    # INC027e: which source this team belongs to (state/key namespace + badge).
    source_key: str = "default"


@dataclass
class DiscoveryResult:
    items: tuple = field(default_factory=tuple)
    complete: bool = False
    reason: str = ""
    pages: int = 0
    duplicates: tuple = field(default_factory=tuple)


@dataclass
class CollectionView:
    """One immutable snapshot used for BOTH cards and awaiting_count."""

    teams: tuple
    sessions: tuple
    awaiting_count: int
    any_unknown: bool
    incomplete_teams: tuple
    updated_at: float = 0.0


@dataclass(frozen=True)
class SessionFetch:
    """Read-only result of one primary's worker-thread fetch fan-out.

    ``permissions`` is the validated permission list, ``messages`` the bounded
    message list and ``archived`` the session.get short-circuit flag. Any
    ``Exception`` raised while fetching is stored in ``error`` and re-raised by
    ``_derive_session`` on the poll thread, so the existing per-session UNKNOWN
    path, reason text and metrics classification are preserved exactly.
    """

    info: dict | None = None
    permissions: list | None = None
    messages: list | None = None
    archived: bool = False
    error: Exception | None = None


class SnapshotRegistry:
    """ONE shared registry of the latest TeamSnapshot per (unique) team key.

    INC027e: all per-source poller threads write into one instance, guarded by
    one lock, and every ``collect_view()`` reads that single registry. Team keys
    are validated as globally unique across sources, so a plain key cannot
    collide across sources. The single-source INC027d case silently gets its own
    private registry, which keeps the original API/behaviour unchanged.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._snapshots = {}

    def set(self, key, snapshot):
        with self._lock:
            self._snapshots[key] = snapshot

    def get(self, key):
        with self._lock:
            return self._snapshots.get(key)

    def values(self):
        with self._lock:
            return list(self._snapshots.values())


def build_collection_view(teams, stale_threshold, now=None):
    """Build one consistent view from an iterable of TeamSnapshots.

    Shared by the single-source :class:`Monitor` and the multi-source
    :class:`MultiSourceMonitor` so cards, status JSON and awaiting_count are
    always derived from the same snapshot set.
    """
    sessions = []
    awaiting = 0
    any_unknown = False
    incomplete = []
    for team in teams:
        for session in team.sessions:
            status = effective_status(session, stale_threshold, now)
            if status == UNKNOWN:
                any_unknown = True
            if status in (WAITING_PERMISSION, READY):
                awaiting += 1
            sessions.append(session)
        if not team.discovery_complete or team.overflow:
            source = getattr(team, "source_key", "default")
            label = team.label
            if source and source != "default":
                label = "%s [%s]" % (label, source)
            banner = "%s: discovery incomplete — membership unverified" % source
            if team.discovery_reason:
                banner += " (" + team.discovery_reason + ")"
            incomplete.append((team.key, label, banner))
    return CollectionView(
        teams=tuple(teams), sessions=tuple(sessions), awaiting_count=awaiting,
        any_unknown=any_unknown, incomplete_teams=tuple(incomplete),
        updated_at=time.monotonic(),
    )


class Monitor:
    """Holds one source's config, API, shared state and snapshot writes.

    INC027e: an optional shared ``registry`` lets several per-source Monitors
    write into one snapshot registry and one StateStore/ReadyPending. Without
    it, behaviour is identical to the INC027d single-source monitor.
    """

    def __init__(self, config, api, store, ready, registry=None, metrics=None):
        self.config = config
        self.api = api
        self.store = store
        self.ready = ready
        # INC027e METRICS: additive, neutral by default. The recorder only
        # observes; it can never change a derived status, reason or call order.
        self.metrics = metrics if metrics is not None else NullMetricsRecorder()
        self.source_key = getattr(config, "source_key", "default") or "default"
        self.deny_ids = frozenset(getattr(config, "deny_session_ids", ()) or ())
        self._registry = registry if registry is not None else SnapshotRegistry()
        self._lock = self._registry._lock
        # Retained session snapshots keyed by (team_key, session_id) — private
        # to this source; team keys are globally unique so no cross-source mix.
        self._session_records = {}

    # ---- polling --------------------------------------------------------------------

    def _note(self, hook, *args, **kwargs):
        """Call one recorder hook; a metrics fault self-disables it, never the poll."""
        recorder = self.metrics
        try:
            getattr(recorder, hook)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - instrumentation must never affect status
            self.metrics = NullMetricsRecorder()

    def metrics_snapshot(self):
        """Last completed per-cycle metrics (None when disabled/absent)."""
        try:
            return self.metrics.snapshot()
        except Exception:  # noqa: BLE001 - metrics must never break a read
            return None

    def poll_once(self):
        """Run one full polling cycle. Per-source/team/session failures are contained.

        The cycle boundary is instrumentation-only: ``begin_cycle``/``end_cycle``
        never alter the polling body, and ``end_cycle`` runs in ``finally`` with
        ``state=ERROR`` for a partial cycle so a metrics fault cannot hide one.
        """
        self._note("begin_cycle")
        state = "OK"
        try:
            self._poll_once_body()
        except BaseException:
            state = "ERROR"
            raise
        finally:
            self._note("end_cycle", state)

    def _poll_once_body(self):
        active_ok = True
        active_data = {}
        active_reason = ""
        try:
            payload = self.api.session_active()
            data = payload.get("data")
            if not isinstance(data, dict):
                raise MonitorApiError("session.active map missing")
            active_data = data
        except MonitorApiError as exc:
            active_ok = False
            active_reason = safe_error(str(exc))

        if active_ok:
            discovery = self._walk_sessions()
        else:
            discovery = DiscoveryResult(
                (), False, "session.active unavailable: " + active_reason, 0, ()
            )
        self._note(
            "note_discovery", discovery.pages, discovery.complete, discovery.reason
        )

        context = {"active": active_data, "info_cache": {}}
        hydrated = self._hydrate_candidates(discovery.items, context)

        team_now = time.monotonic()
        for team in self.config.teams:
            try:
                snapshot = self._build_team_snapshot(team, discovery, hydrated, context)
            except Exception as exc:  # noqa: BLE001 - one team must not crash the collector
                snapshot = TeamSnapshot(
                    key=team.key, label=team.label, updated_at=team_now,
                    status=UNKNOWN, reason="collector error: " + safe_error(str(exc)),
                    discovery_complete=False,
                    discovery_reason=discovery.reason or "collector error",
                    source_key=self.source_key,
                )
            snapshot.updated_at = team_now
            with self._lock:
                self._registry.set(team.key, snapshot)

    def _walk_sessions(self):
        """One bounded, deduplicated, terminal-required ``session.list`` walk."""
        seen_cursors = set()
        seen_ids = set()
        items = []
        duplicates = []
        pages = 0
        cursor = None
        complete = False
        reason = ""

        while True:
            if pages >= MAX_PAGES:
                reason = "page cap %d reached" % MAX_PAGES
                break
            try:
                payload = self.api.session_list(cursor=cursor, limit=PAGE_LIMIT)
            except MonitorApiError as exc:
                reason = "session.list error: " + safe_error(str(exc))
                break
            data = payload.get("data")
            if not isinstance(data, list):
                reason = "session.list data missing or malformed"
                break
            pages += 1
            malformed = False
            for entry in data:
                if not isinstance(entry, dict):
                    malformed = True
                    continue
                sid = entry.get("id")
                if not isinstance(sid, str) or not sid:
                    malformed = True
                    continue
                if sid in seen_ids:
                    duplicates.append(sid)
                    continue  # dedup: never produce two cards for one id
                if len(seen_ids) >= MAX_UNIQUE:
                    reason = "unique-session cap %d reached" % MAX_UNIQUE
                    break
                seen_ids.add(sid)
                items.append(entry)
            if reason:
                break
            if malformed:
                reason = "malformed session.list item"
                break
            next_cursor = next_list_cursor(payload)
            if next_cursor is None:
                complete = True
                break
            if next_cursor in seen_cursors:
                reason = "cursor cycle detected"
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return DiscoveryResult(
            items=tuple(items), complete=complete, reason=reason,
            pages=pages, duplicates=tuple(duplicates),
        )

    def _hydrate_candidates(self, items, context):
        """session.get only where an absent-parent item lacks directory metadata.

        Results are cached in context['info_cache'] so the subsequent status
        poll does not issue a second get for the same id.
        """
        hydrated = {}
        cache = context["info_cache"]
        for item in items:
            if not isinstance(item, dict) or "parentID" in item:
                continue
            if extract_directory(item) is not None:
                continue
            sid = item.get("id")
            if not isinstance(sid, str) or not sid or sid in hydrated:
                continue
            try:
                info = self._get_info(sid, cache)
            except Exception:  # noqa: BLE001 - a failed hydration stays UNKNOWN
                self._note("note_hydration", False)
                continue
            self._note("note_hydration", True)
            hydrated[sid] = info
        return hydrated

    def _get_info(self, session_id, cache):
        if session_id in cache:
            return cache[session_id]
        payload = self.api.session_get(session_id)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MonitorApiError("session.get data missing")
        if data.get("id") != session_id:
            raise MonitorApiError("session.get id mismatch")
        cache[session_id] = data
        return data

    def _build_team_snapshot(self, team, discovery, hydrated, context):
        complete = discovery.complete
        reason = discovery.reason

        pinned = tuple(
            sid for sid in ((team.session_id,) if team.session_id else ())
            if not is_denied(sid, self.deny_ids)
        )
        discovered = []
        children_by_parent = {}
        unknown_ids = []
        for item in discovery.items:
            kind, sid = classify_candidate(item, team.worktree_dir, self.deny_ids, hydrated)
            if kind == KIND_PRIMARY:
                discovered.append(sid)
            elif kind == KIND_CHILD:
                parent = item.get("parentID")
                if isinstance(parent, str) and parent:
                    children_by_parent.setdefault(parent, []).append(sid)
            elif kind in (KIND_UNKNOWN, KIND_NEEDS_HYDRATE):
                unknown_ids.append(sid)

        discovered = list(dict.fromkeys(discovered))
        candidates = list(dict.fromkeys(list(pinned) + discovered))
        self._note("note_discovery_primaries", len(discovered))
        self._note("note_primaries", len(candidates))

        # Explicit overflow -> this team UNKNOWN, fail closed, other teams unaffected.
        if len(candidates) > MAX_PRIMARIES_PER_TEAM:
            self._note("note_overflow")
            complete = False
            reason = (reason + "; " if reason else "") + (
                "primary overflow (%d > %d)" % (len(candidates), MAX_PRIMARIES_PER_TEAM)
            )

        children_by_parent = {
            parent: tuple(ids[:MAX_CHILDREN])
            for parent, ids in children_by_parent.items()
        }
        # INC027e: pinned child anchors are advisory only (never cards). They
        # attach to the pinned primary so a known child is still shown even if
        # it is absent from an otherwise-COMPLETE discovery page.
        if team.session_id and getattr(team, "child_session_ids", ()):
            pinned_children = list(children_by_parent.get(team.session_id, ()))
            for cid in team.child_session_ids:
                if cid not in pinned_children:
                    pinned_children.append(cid)
            children_by_parent[team.session_id] = tuple(pinned_children[:MAX_CHILDREN])
        active = context["active"]

        if complete:
            live_ids = set(candidates)
        else:
            # No NEW candidates admitted. Keep retained cards + pinned anchors.
            existing = {sid for (key, sid) in self._session_records if key == team.key}
            live_ids = existing | set(pinned)

        # INC027f: one bounded parallel fetch for the whole COMPLETE set, then
        # derive every card on this poll thread in sorted order. An INCOMPLETE
        # discovery makes ZERO per-session calls (unchanged fail-closed path).
        fetches = {}
        if complete:
            fetch_ids = [sid for sid in sorted(live_ids)]
            fetches = self._fetch_sessions(fetch_ids, context["info_cache"])

        sessions = []
        for sid in sorted(live_ids):
            key = (team.key, sid)
            prev = self._session_records.get(key)
            if not complete:
                # Discovery INCOMPLETE: keep visible as UNKNOWN, no membership
                # advancement, pending preserved, no per-session API calls.
                sessions.append(self._unknown_snapshot(team, sid, prev, reason or "discovery incomplete"))
                continue

            # COMPLETE: id observed as a primary this poll (pinned is always live).
            child_ids = children_by_parent.get(sid, ())
            child_busy = any(child in active for child in child_ids)
            self._note("note_polled")
            try:
                snap = self._derive_session(
                    team, sid, fetches[sid], child_ids, child_busy, active
                )
                snap.membership = PRESENT
                snap.grace_count = 0
            except Exception as exc:  # noqa: BLE001 - one bad session must not poison siblings
                snap = SessionSnapshot(
                    session_id=sid, source_key=self.source_key,
                    team_key=team.key, team_label=team.label,
                    status=UNKNOWN, reason="session error: " + safe_error(str(exc)),
                    updated_at=time.monotonic(), membership=PRESENT,
                    child_ids=child_ids, child_busy=child_busy,
                    last_activity_ts=prev.last_activity_ts if prev else None,
                    title=prev.title if prev else "", summary=prev.summary if prev else "",
                )
            sessions.append(snap)

        if complete:
            # Retention: absent-in-successful-discovery -> GRACE -> DROPPED.
            retained = {}
            for snap in sessions:
                retained[(team.key, snap.session_id)] = snap
            for (tkey, sid), prev in list(self._session_records.items()):
                if tkey != team.key or sid in live_ids:
                    if tkey == team.key:
                        retained.setdefault((tkey, sid), prev)
                    continue
                grace = prev.grace_count + 1
                if grace > GRACE_LIMIT:
                    # Final drop: clear pending, keep last_seen baseline forever.
                    self.ready.clear(sid, self.source_key)
                    continue
                retained[(tkey, sid)] = self._grace_snapshot(team, sid, prev, grace)
            self._session_records = {
                key: value for key, value in self._session_records.items()
                if key[0] != team.key
            }
            self._session_records.update(retained)
            sessions = [
                snap for (tkey, _sid), snap in sorted(
                    retained.items(), key=lambda kv: kv[0][1]
                ) if tkey == team.key
            ]

        # Build the team aggregate view (no UNKNOWN-collapse, no sibling poisoning).
        sessions = tuple(sessions)
        for derived in sessions:
            self._note("note_status", derived.status)
            if derived.status == UNKNOWN:
                try:
                    reason_class = classify_unknown_reason(derived)
                except Exception:  # noqa: BLE001 - instrumentation only
                    reason_class = "retained_unknown"
                self._note("note_unknown_reason", reason_class)
        now = time.monotonic()
        snapshot = TeamSnapshot(
            key=team.key, label=team.label, updated_at=now,
            status=aggregate_status(sessions),
            session_ids=tuple(s.session_id for s in sessions),
            sessions=sessions,
            discovery_complete=complete,
            discovery_reason="" if complete else (reason or "discovery incomplete"),
            overflow=len(candidates) > MAX_PRIMARIES_PER_TEAM,
            source_key=self.source_key,
        )
        timed = [s for s in sessions if s.last_activity_ts is not None]
        if timed:
            latest = max(timed, key=lambda s: s.last_activity_ts)
            snapshot.last_activity_ts = latest.last_activity_ts
            snapshot.title = latest.title
            snapshot.summary = latest.summary
        snapshot.awaiting = any(s.awaiting for s in sessions)
        reasons = [s.reason for s in sessions if s.reason]
        if unknown_ids and complete:
            reasons.append("unresolved candidates: " + cap_text(", ".join(unknown_ids), 80))
        if not complete:
            reasons.insert(0, "discovery incomplete — membership unverified")
        if reasons:
            snapshot.reason = cap_text("; ".join(reasons), MAX_REASON_CHARS)
        with self._lock:
            for snap in sessions:
                self._session_records[(team.key, snap.session_id)] = snap
        return snapshot

    def _unknown_snapshot(self, team, sid, prev, reason):
        snap = SessionSnapshot(
            session_id=sid, source_key=self.source_key,
            team_key=team.key, team_label=team.label,
            status=UNKNOWN, reason=cap_text(reason or "discovery incomplete", MAX_REASON_CHARS),
            updated_at=time.monotonic(),
            membership=prev.membership if prev else PRESENT,
            grace_count=prev.grace_count if prev else 0,
            child_ids=prev.child_ids if prev else (),
            incomplete=True, awaiting=False,
            last_activity_ts=prev.last_activity_ts if prev else None,
            title=prev.title if prev else "", summary=prev.summary if prev else "",
        )
        return snap

    def _grace_snapshot(self, team, sid, prev, grace):
        snap = self._unknown_snapshot(
            team, sid, prev, "not observed (grace %d/%d)" % (grace, GRACE_LIMIT)
        )
        snap.membership = GRACE
        snap.grace_count = grace
        return snap

    def _fetch_message_data(self, session_id):
        """Return the message list with a bounded fallback for oversized output.

        At most two GET-only calls, both order=desc. Truncated JSON is never
        parsed into partial state; only MalformedApiResponse triggers the retry.
        """
        try:
            payload = self.api.session_message_list(
                session_id, limit=MESSAGE_LIMIT, order="desc"
            )
        except MalformedApiResponse:
            self._note("note_message_list_fallback")
            payload = self.api.session_message_list(
                session_id, limit=MESSAGE_FALLBACK_LIMIT, order="desc"
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise MonitorApiError("message list data missing")
        return data

    def _fetch_session(self, sid, info_cache):
        """Worker-thread fetch of ONE primary: read-only, never raises.

        The info cache is READ-ONLY here (no worker write); the poll thread
        merges successful infos only after every future has joined. Any
        ``Exception`` is captured in ``SessionFetch.error`` so the poll thread
        can re-raise it inside the existing per-session try/except, preserving
        the UNKNOWN card, reason text and metrics classification exactly.
        """
        try:
            info = info_cache.get(sid)
            if info is None:
                payload = self.api.session_get(sid)
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise MonitorApiError("session.get data missing")
                if data.get("id") != sid:
                    raise MonitorApiError("session.get id mismatch")
                info = data
            archived = extract_archived(info)
            if archived is True:
                return SessionFetch(info=info, archived=True)

            permissions = self.api.session_permission_list(sid)
            permission_data = permissions.get("data")
            if not isinstance(permission_data, list):
                raise MonitorApiError("permission list data missing")
            message_data = self._fetch_message_data(sid)
            return SessionFetch(
                info=info, permissions=permission_data, messages=message_data,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised on the poll thread
            return SessionFetch(error=exc)

    def _fetch_sessions(self, sids, info_cache):
        """Fetch every primary with a bounded, per-source worker pool.

        ``len <= 1`` runs inline (no executor). Otherwise exactly one
        ThreadPoolExecutor is created, named ``monitor-fetch-<source>``, and
        shut down before this method returns, so no worker thread can outlive
        the poll. Results are collected in ``sids`` order; the info cache is
        written ONLY here, on the poll thread, after the join.
        """
        sids = list(sids)
        if len(sids) <= 1:
            results = {sid: self._fetch_session(sid, info_cache) for sid in sids}
        else:
            results = {}
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(FETCH_CONCURRENCY, len(sids)),
                thread_name_prefix="monitor-fetch-" + self.source_key,
            ) as pool:
                futures = {
                    sid: pool.submit(self._fetch_session, sid, info_cache)
                    for sid in sids
                }
                for sid in sids:
                    results[sid] = futures[sid].result()
        # The ONLY poll-stage cache writes happen on the poll thread, post-join.
        for sid, fetch in results.items():
            if fetch.error is None and isinstance(fetch.info, dict):
                info_cache[sid] = fetch.info
        return results

    def _derive_session(self, team, sid, fetch, child_ids, child_busy, active):
        """Derive one card from a completed fetch — poll thread only.

        This is the verbatim remainder of the pre-027f ``_poll_session``: every
        store/ready mutation and every status-precedence branch happens here,
        in sorted poll-thread order, so concurrency can never reorder state.
        """
        if fetch.error is not None:
            raise fetch.error

        info = fetch.info
        if fetch.archived is True:
            return SessionSnapshot(
                session_id=sid, source_key=self.source_key,
                team_key=team.key, team_label=team.label,
                status=UNKNOWN, reason="session archived", updated_at=time.monotonic(),
                child_ids=child_ids, child_busy=child_busy,
            )

        permission_data = fetch.permissions
        message_data = fetch.messages

        outcome = info.get("outcome")
        busy = sid in active
        # Metrics only: bucket the outcome literal without touching the derived
        # status/reason; the existing branch immediately below is unchanged.
        try:
            outcome_bucket = classify_outcome("outcome" in info, outcome, KNOWN_OUTCOMES)
        except Exception:  # noqa: BLE001 - instrumentation must never affect status
            outcome_bucket = "other_type"
        self._note("note_outcome", outcome_bucket, busy, outcome)

        newest_id = None
        newest_message = None
        for message in message_data:
            if is_assistant(message):
                candidate = message_id(message)
                if candidate:
                    newest_id = candidate
                    newest_message = message
                    break

        # Completed-answer detection only when the session is not busy.
        if not busy:
            baseline = self.store.last_seen(sid, self.source_key)
            if baseline is None:
                self.store.record(sid, newest_id or "", self.source_key)
            elif newest_id is not None and newest_id != baseline:
                # Persist on detection so a restart cannot re-raise this READY.
                self.store.record(sid, newest_id, self.source_key)
                self.ready.set(sid, newest_id, self.source_key)

        # INC027i precedence: a CURRENT action/permission state outranks the
        # PREVIOUS turn's terminal outcome (a retry while ``outcome`` still holds
        # ``failed`` is BUSY/WAITING_PERMISSION, not ERROR). ERROR stays above
        # READY because a failed turn can emit an assistant message with no text,
        # so a partial failed turn must not be silently upgraded to READY.
        if not isinstance(outcome, str) or outcome not in KNOWN_OUTCOMES:
            status, reason = UNKNOWN, "outcome missing or unrecognized: " + safe_error(str(outcome))
        elif len(permission_data) > 0:
            status, reason = WAITING_PERMISSION, "permission request pending"
        elif busy:
            status, reason = BUSY, "session active"
        elif outcome in ("failed", "interrupted"):
            status, reason = ERROR, "outcome=%s" % str(outcome)
        elif self.ready.has(sid, self.source_key):
            status, reason = READY, "new completed answer"
        else:
            status, reason = IDLE, "idle"

        # Advisory child override: only IDLE -> BUSY; never mask READY/WAITING/
        # PERMISSION/ERROR/UNKNOWN and never make child message/permission calls.
        if status == IDLE and child_busy:
            status, reason = BUSY, "child session active"

        summary = ""
        if newest_message is not None:
            rendered = summarize_messages([newest_message])
            if rendered["texts"]:
                summary = cap_text(rendered["texts"][0], DISPLAY_SUMMARY_CHARS)

        last_activity_ts = None
        timing = info.get("time")
        if isinstance(timing, dict):
            updated = timing.get("updated")
            if isinstance(updated, (int, float)) and not isinstance(updated, bool):
                last_activity_ts = float(updated) / 1000.0
        if last_activity_ts is None and newest_message is not None:
            message_ts = message_time_ms(newest_message)
            if message_ts is not None:
                last_activity_ts = message_ts / 1000.0

        title = info.get("title") if isinstance(info.get("title"), str) else ""

        return SessionSnapshot(
            session_id=sid, source_key=self.source_key,
            team_key=team.key, team_label=team.label,
            status=status, reason=reason, last_activity_ts=last_activity_ts,
            title=title, summary=summary, awaiting=status in (WAITING_PERMISSION, READY),
            updated_at=time.monotonic(), membership=PRESENT, grace_count=0,
            child_ids=tuple(child_ids), child_busy=child_busy,
        )

    # ---- read-only views and local acknowledgement ----------------------------------

    def collect_view(self, stale_threshold, now=None):
        """Produce ONE consistent snapshot for both cards and awaiting_count."""
        return build_collection_view(self._registry.values(), stale_threshold, now)

    def snapshot(self, key):
        return self._registry.get(key)

    def snapshots(self):
        return self._registry.values()

    def await_count(self, stale_threshold, now=None):
        return self.collect_view(stale_threshold, now).awaiting_count

    def acknowledge_team(self, key):
        """Back-compat local Gezien: clear READY for every session in a team."""
        with self._lock:
            team = self._registry.get(key)
            if team is None:
                return False
            for session in team.sessions:
                self.ready.clear(session.session_id, session.source_key)
                if session.status == READY:
                    session.status = IDLE
                    session.reason = "acknowledged locally"
                    session.awaiting = False
                    session.updated_at = time.monotonic()
            self._recompute_team(team)
            return True

    def acknowledge_session(self, source_key, session_id):
        """Per-session local Gezien: clear READY for one (source_key, session_id)."""
        with self._lock:
            for team in self._registry.values():
                for session in team.sessions:
                    if session.session_id == session_id and session.source_key == source_key:
                        self.ready.clear(session_id, source_key)
                        if session.status == READY:
                            session.status = IDLE
                            session.reason = "acknowledged locally"
                            session.awaiting = False
                            session.updated_at = time.monotonic()
                        self._recompute_team(team)
                        return True
            return False

    @staticmethod
    def _recompute_team(team):
        sessions = team.sessions
        team.status = aggregate_status(sessions)
        team.awaiting = any(s.awaiting for s in sessions)
        reasons = [s.reason for s in sessions if s.reason]
        team.reason = cap_text("; ".join(reasons), MAX_REASON_CHARS)


class MultiSourceMonitor:
    """One dashboard surface over N independent per-source Monitor pollers.

    INC027e: each source has its own GET-only API and its own poll thread, but
    ALL sources share one snapshot registry, one StateStore and one
    ReadyPending. A Windows fault or staleness marks ONLY that source's cards
    UNKNOWN; WSL2 cards are unaffected. The web layer needs no change: this
    facade exposes the same read/ack surface as a single Monitor and one
    ``collect_view()`` feeds cards, status JSON and awaiting_count.
    """

    def __init__(self, monitors, store, ready, registry=None):
        if not monitors:
            raise ValueError("at least one source monitor is required")
        self.monitors = tuple(monitors)
        self.store = store
        self.ready = ready
        self.registry = registry if registry is not None else monitors[0]._registry
        # Default source for back-compat ack callers that omit source_key;
        # cards carry an explicit source_key, so Gezien is always unambiguous.
        self.source_key = monitors[0].source_key

    def poll_once(self):
        """Poll every source once (sequential helper for offline tests)."""
        for monitor in self.monitors:
            monitor.poll_once()

    def poll_source(self, source_key):
        """Poll one source by key; return True if it exists."""
        for monitor in self.monitors:
            if monitor.source_key == source_key:
                monitor.poll_once()
                return True
        return False

    def source_keys(self):
        return tuple(m.source_key for m in self.monitors)

    def metrics_snapshots(self):
        """Per-source last completed cycle (additive; never raises)."""
        block = {}
        for monitor in self.monitors:
            try:
                snapshot = monitor.metrics_snapshot()
            except Exception:  # noqa: BLE001 - metrics must never break status JSON
                snapshot = None
            if isinstance(snapshot, dict):
                block[monitor.source_key] = snapshot
        return block

    def collect_view(self, stale_threshold, now=None):
        return build_collection_view(self.registry.values(), stale_threshold, now)

    def snapshot(self, key):
        return self.registry.get(key)

    def snapshots(self):
        return self.registry.values()

    def await_count(self, stale_threshold, now=None):
        return self.collect_view(stale_threshold, now).awaiting_count

    def acknowledge_team(self, key):
        # Every monitor scans the shared registry, so one successful match is
        # enough and the operation is idempotent across the others.
        for monitor in self.monitors:
            if monitor.acknowledge_team(key):
                return True
        return False

    def acknowledge_session(self, source_key, session_id):
        for monitor in self.monitors:
            if monitor.acknowledge_session(source_key, session_id):
                return True
        return False
