"""Loopback-only stdlib dashboard and the local "Gezien" POST endpoint.

The web layer reads ONE consistent view (``collect_view()``) per request for
both the cards and the awaiting-operator count, so the two can never disagree.
It can only clear local in-memory READY state (per session or per team); it never
holds or calls the OpenCode API object, so Gezien can never reach a session.
"""
from __future__ import annotations

import html
import ipaddress
import json
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .collector import (
    BUSY, ERROR, READY, UNKNOWN, WAITING_PERMISSION, effective_status,
    status_rank,
)

STATUS_COLOURS = {
    "BUSY": "#e08b16",
    "READY": "#1f9d55",
    "WAITING_PERMISSION": "#b23a9b",
    "ERROR": "#c0392b",
    "IDLE": "#5b7a99",
    "UNKNOWN": "#8a8a8a",
}


class RefusedBind(Exception):
    """Raised when a non-loopback bind is requested without the override flag."""


def is_loopback(host):
    """True only when EVERY resolved address is an IPv4/IPv6 loopback."""
    if not isinstance(host, str) or not host:
        return False
    candidate = host.strip()
    if not candidate:
        return False
    if candidate in ("0.0.0.0", "::", "*", "[::]"):
        return False
    try:
        resolved = socket.getaddrinfo(candidate, None)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return False
    if not resolved:
        return False
    for entry in resolved:
        address = entry[4][0]
        if not isinstance(address, str):
            return False
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_loopback:
            return False
    return True


def _format_timestamp(epoch):
    if epoch is None:
        return "\u2014"
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, ValueError, OverflowError):
        return "\u2014"


def _team_status(team, stale_threshold, now):
    """Compatibility team aggregate from effective per-session statuses.

    AC-d2: no 'any UNKNOWN -> whole team UNKNOWN' collapse — a healthy sibling
    keeps a healthy aggregate; UNKNOWN is simply lowest precedence and is
    ignored unless every session is UNKNOWN. INC027j order is
    WAITING_PERMISSION, READY, BUSY, IDLE, ERROR (from ``STATUS_ORDER``): a
    historical ERROR never hides a current operational sibling (action, BUSY
    or IDLE), and ERROR only wins when no operational state exists and at least
    one session is actually ERROR. No IDLE is fabricated.
    """
    statuses = [
        effective_status(s, stale_threshold, now) for s in team.sessions
    ]
    non_unknown = [s for s in statuses if s != UNKNOWN]
    return min(non_unknown, key=status_rank) if non_unknown else UNKNOWN


def _metrics_block(monitor):
    """Additive per-source metrics for /status.json; never raises.

    A metrics fault (broken/absent recorder) yields ``{}`` so the existing
    status payload is always served unchanged.
    """
    try:
        getter = getattr(monitor, "metrics_snapshots", None)
        if callable(getter):
            block = getter()
            return block if isinstance(block, dict) else {}
        getter = getattr(monitor, "metrics_snapshot", None)
        if callable(getter):
            snapshot = getter()
            if isinstance(snapshot, dict):
                source = getattr(monitor, "source_key", "default") or "default"
                return {source: snapshot}
    except Exception:  # noqa: BLE001 - instrumentation must never break status JSON
        return {}
    return {}


def _session_age(now, updated_at):
    try:
        return max(0.0, float(now) - float(updated_at))
    except (TypeError, ValueError):
        return 0.0


def status_json(monitor, stale_threshold, now=None):
    """Return a JSON-serialisable snapshot from ONE consistent view.

    INC027e METRICS adds only: a top-level ``metrics`` block (per source) and,
    per session, ``age_seconds`` + ``stale`` derived from the SAME ``now`` as
    ``effective_status``. Existing keys and values are unchanged.
    """
    if now is None:
        now = time.monotonic()
    view = monitor.collect_view(stale_threshold, now)
    teams = []
    for team in view.teams:
        teams.append({
            "key": team.key,
            "label": team.label,
            "source_key": getattr(team, "source_key", "default"),
            "status": _team_status(team, stale_threshold, now),
            "reason": team.reason,
            "title": team.title,
            "last_activity": _format_timestamp(team.last_activity_ts),
            "awaiting": team.awaiting,
            "summary": team.summary,
        })
    sessions = []
    for session in view.sessions:
        age = _session_age(now, session.updated_at)
        sessions.append({
            "session_id": session.session_id,
            "source_key": session.source_key,
            "team_key": session.team_key,
            "team_label": session.team_label,
            "status": effective_status(session, stale_threshold, now),
            "reason": session.reason,
            "title": session.title,
            "last_activity": _format_timestamp(session.last_activity_ts),
            "awaiting": session.awaiting,
            "summary": session.summary,
            "membership": session.membership,
            "grace_count": session.grace_count,
            # Additive (INC027e METRICS): same ``now`` as effective_status above.
            "age_seconds": round(age, 3),
            "stale": age > stale_threshold,
        })
    return {
        "teams": teams,
        "awaiting_count": view.awaiting_count,
        "sessions": sessions,
        "banners": [reason for _key, _label, reason in view.incomplete_teams],
        "metrics": _metrics_block(monitor),
    }


def _session_sort_key(session, stale_threshold, now):
    """UI render order inside one team (INC027j D2; NOT the status_json order).

    Group 0 is the action group (WAITING_PERMISSION, READY), group 1 is BUSY,
    and group 2 is every remaining status (IDLE, ERROR, UNKNOWN). Within the
    action and BUSY groups newest ``last_activity_ts`` comes first; group 2 is
    ordered purely by activity descending across statuses (no ERROR-first
    ordering). A session without a timestamp sorts last within its group, with
    a deterministic ``(source_key, session_id)`` tie-break.
    """
    status = effective_status(session, stale_threshold, now)
    if status in (WAITING_PERMISSION, READY):
        group = 0
    elif status == BUSY:
        group = 1
    else:
        group = 2
    activity = session.last_activity_ts
    has_no_activity = activity is None
    return (
        group,
        has_no_activity,
        -(activity if activity is not None else 0.0),
        session.source_key or "default",
        session.session_id,
    )


def _banner_html(view, stale_threshold, now):
    """Split UNKNOWN sessions into neutral outcome-missing vs red causes.

    Fail-closed status values are unchanged: this only classifies the existing
    UNKNOWN cards. ``outcome missing or unrecognized`` is a feed/data condition
    (neutral info, explicitly NOT a connection failure); a raw non-UNKNOWN card
    that is stale, a ``session error``/``collector error`` and any other
    non-archived UNKNOWN cause stay on the red transport banner. Archived cards
    never raise either banner, and incomplete discovery keeps its yellow banner.
    """
    neutral = 0
    transport = 0
    for session in view.sessions:
        if effective_status(session, stale_threshold, now) != UNKNOWN:
            continue
        raw_status = getattr(session, "status", UNKNOWN)
        reason = session.reason or ""
        if raw_status == UNKNOWN and reason.startswith("session archived"):
            continue
        if raw_status == UNKNOWN and reason.startswith("outcome missing or unrecognized"):
            neutral += 1
        else:
            transport += 1
    parts = []
    if transport:
        parts.append(
            '<div class="banner" role="alert">Connection lost or status unavailable '
            "for " + str(transport) + " session(s) (UNKNOWN). No status is fabricated.</div>"
        )
    if neutral:
        parts.append(
            '<div class="banner info" role="status">Status unavailable for '
            + str(neutral) + " session(s): no terminal outcome was available in the "
            "session feed. This is NOT a connection failure; no status is "
            "fabricated.</div>"
        )
    if view.incomplete_teams:
        labels = ", ".join(html.escape(label) for _key, label, _reason in view.incomplete_teams)
        parts.append(
            '<div class="banner incomplete" role="alert">'
            "Discovery incomplete — membership unverified for: " + labels + ".</div>"
        )
    return "\n  ".join(parts)


def _session_card(session, stale_threshold, now):
    status = effective_status(session, stale_threshold, now)
    colour = STATUS_COLOURS.get(status, STATUS_COLOURS[UNKNOWN])
    escaped_reason = html.escape(session.reason or "")
    escaped_title = html.escape(session.title or "")
    escaped_summary = html.escape(session.summary or "")
    activity = html.escape(_format_timestamp(session.last_activity_ts))
    source_badge = html.escape(session.source_key or "default")
    if status == READY:
        action = (
            '        <form method="post" action="/ack">\n'
            '          <input type="hidden" name="team_key" value="{key}">\n'
            '          <input type="hidden" name="source_key" value="{source}">\n'
            '          <input type="hidden" name="session_id" value="{sid}">\n'
            '          <button type="submit">Gezien</button>\n'
            '        </form>'
        ).format(
            key=html.escape(session.team_key),
            source=html.escape(session.source_key),
            sid=html.escape(session.session_id),
        )
    elif status == WAITING_PERMISSION:
        action = (
            '        <div class="ack-note">Permission request pending in the '
            'OpenCode session; Gezien does not apply.</div>'
        )
    else:
        action = ""
    grace_note = ""
    if session.membership == "GRACE" or session.incomplete:
        grace_note = (
            '<div class="activity">Retained card (membership unverified); '
            'status shown as UNKNOWN.</div>'
        )
    # INC027i: stable scroll anchor; the value is fully HTML-escaped because a
    # source key or session id is untrusted feed data.
    session_anchor = html.escape(
        "%s|%s" % (session.source_key or "default", session.session_id)
    )
    return """
      <section class="card" data-session="{anchor}" style="border-top-color:{colour}">
        <div class="session-id">Session: {sid} <span class="source-badge">source: {source}</span></div>
        <div class="status" style="color:{colour}">{status}</div>
        <div class="reason">{reason}</div>
        <div class="activity">Last activity: {activity}{title}</div>
        <div class="summary">Last answer: {summary}</div>
{grace}
{action}
      </section>""".format(
        anchor=session_anchor,
        colour=colour,
        sid=html.escape(session.session_id),
        source=source_badge,
        status=html.escape(status),
        reason=escaped_reason,
        activity=activity,
        title=(" \u2014 " + escaped_title) if escaped_title else "",
        summary=escaped_summary or "\u2014",
        grace=grace_note,
        action=action,
    )


def _team_summary_html(team, sessions, stale_threshold, now):
    """Collapsed ``<summary>``: label, counts, aggregate and action badges.

    UI-only (INC027h): derived from the same ``effective_status`` as the cards
    and ``status_json``; no backend field or status precedence is changed.
    """
    statuses = [effective_status(s, stale_threshold, now) for s in sessions]
    counts = {status: statuses.count(status) for status in (READY, BUSY, ERROR, UNKNOWN)}
    actions = sum(1 for status in statuses if status in (READY, WAITING_PERMISSION))
    aggregate = _team_status(team, stale_threshold, now)
    colour = STATUS_COLOURS.get(aggregate, STATUS_COLOURS[UNKNOWN])
    return (
        "<summary>"
        '<span class="team-label">{label}</span>'
        '<span class="source-badge">source: {source}</span>'
        '<span class="session-count">sessions: {count}</span>'
        '<span class="team-status" style="color:{colour}">{aggregate}</span>'
        '<span class="counts">'
        '<span class="badge ready">READY {ready}</span>'
        '<span class="badge busy">BUSY {busy}</span>'
        '<span class="badge error">ERROR {error}</span>'
        '<span class="badge unknown">UNKNOWN {unknown}</span>'
        '<span class="badge action">\u26a0 actie {actions}</span>'
        "</span>"
        "</summary>"
    ).format(
        label=html.escape(team.label),
        source=html.escape(getattr(team, "source_key", "default")),
        count=len(sessions),
        colour=colour,
        aggregate=html.escape(aggregate),
        ready=counts[READY],
        busy=counts[BUSY],
        error=counts[ERROR],
        unknown=counts[UNKNOWN],
        actions=actions,
    )


def render_dashboard(monitor, stale_threshold, refresh_seconds, now=None):
    """Render the single dashboard page from one consistent snapshot view."""
    view = monitor.collect_view(stale_threshold, now)

    by_team = {}
    for team in view.teams:
        by_team[team.key] = []
    for session in view.sessions:
        by_team.setdefault(session.team_key, []).append(session)

    groups = []
    for team in view.teams:
        # INC027j D2: display order only. status_json keeps the view order and
        # its shape; this list is a per-render copy.
        sessions = sorted(
            by_team.get(team.key, []),
            key=lambda s: _session_sort_key(s, stale_threshold, now),
        )
        cards = "\n".join(
            _session_card(s, stale_threshold, now) for s in sessions
        )
        if not cards:
            cards = '<p class="empty">No verified primary sessions for this team.</p>'
        groups.append(
            """
      <details class="team" data-team="{key}">
        {summary}
        <div class="session-list">
{cards}
        </div>
      </details>""".format(
                key=html.escape(team.key),
                summary=_team_summary_html(team, sessions, stale_threshold, now),
                cards=cards,
            )
        )

    banner = _banner_html(view, stale_threshold, now)
    teams_html = "\n".join(groups) if groups else "<p>No teams configured.</p>"
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{refresh}">
  <title>OpenCode Session Monitor</title>
  <style>
    body {{ font-family: sans-serif; margin: 1.5rem; background: #f5f6f8; color: #222; }}
    h1 {{ margin-bottom: .2rem; }}
    .banner {{ background: #ffe8e6; border: 1px solid #c0392b; color: #7a1c12;
               padding: .7rem 1rem; border-radius: 6px; margin: 1rem 0; }}
    .banner.incomplete {{ background: #fff4d6; border-color: #b8860b; color: #6b4a00; }}
    .banner.info {{ background: #eef2f7; border-color: #8a8a8a; color: #444; }}
    .count {{ font-weight: bold; margin: 1rem 0; }}
    .teams {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
              gap: 1rem; align-items: start; margin-top: 1rem; }}
    .team {{ background: #fff; border: 1px solid #ddd; border-radius: 8px;
             padding: .6rem .8rem; min-width: 0; }}
    .team > summary {{ cursor: pointer; display: flex; flex-wrap: wrap;
                       gap: .35rem .55rem; align-items: center; }}
    .team > summary::-webkit-details-marker {{ display: none; }}
    .team > summary::before {{ content: "\\25B8"; color: #888; font-size: .9rem; }}
    .team[open] > summary::before {{ content: "\\25BE"; }}
    .team-label {{ font-weight: bold; }}
    .team-status {{ font-weight: bold; }}
    .session-count {{ color: #666; font-size: .85rem; }}
    .counts {{ display: flex; flex-wrap: wrap; gap: .25rem; flex-basis: 100%; }}
    .badge {{ font-size: .72rem; font-weight: bold; border: 1px solid #ccc;
              border-radius: 999px; padding: 1px 8px; background: #f2f2f2; color: #444; }}
    .badge.ready {{ background: #e3f6ea; border-color: #1f9d55; color: #156b3b; }}
    .badge.busy {{ background: #fdf1de; border-color: #e08b16; color: #8a5300; }}
    .badge.error {{ background: #fde8e6; border-color: #c0392b; color: #8e2418; }}
    .badge.unknown {{ background: #eee; border-color: #8a8a8a; color: #555; }}
    .badge.action {{ background: #fff4d6; border-color: #b8860b; color: #6b4a00; }}
    .session-list {{ max-height: 22rem; overflow-y: auto; margin-top: .7rem;
                     padding-right: .2rem; }}
    .card {{ background: #fff; border: 1px solid #ddd; border-top: 5px solid #888;
             border-radius: 8px; padding: 1rem; margin: .7rem 0; }}
    .session-id {{ font-size: .8rem; color: #888; }}
    .source-badge {{ display: inline-block; font-size: .75rem; font-weight: normal;
                     color: #2c5a8a; background: #e8f0fa; border: 1px solid #bcd3ec;
                     border-radius: 4px; padding: 1px 6px; margin-left: .4rem; }}
    .status {{ font-size: 1.4rem; font-weight: bold; }}
    .reason, .activity, .summary {{ font-size: .9rem; margin-top: .4rem; color: #444; }}
    button {{ margin-top: .8rem; padding: .4rem .9rem; cursor: pointer; }}
    .ack-note {{ font-size: .85rem; color: #6b4a00; margin-top: .8rem; }}
    .empty {{ font-size: .9rem; color: #888; }}
    @media (max-width: 1100px) {{
      .teams {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 700px) {{
      .teams {{ grid-template-columns: minmax(0, 1fr); }}
    }}
  </style>
</head>
<body>
  <h1>OpenCode Session Monitor</h1>
  <p>Read-only, local, loopback-only. Monitoring never writes to a session.</p>
  {banner}
  <div class="count">Awaiting operator: {awaiting}</div>
  <div class="teams">
{teams}
  </div>
  <script>
  (function () {{
    // INC027h open-state key is unchanged, so the current open/closed state
    // survives this increment. Scroll position uses sessionStorage so it
    // survives the meta refresh without changing the refresh itself.
    var PREFIX = 'inc027h-team-open:';
    var SCROLL_PREFIX = 'inc027i-scroll:';
    var PAGE_KEY = 'inc027i-page-scroll';
    var SAVE_DELAY_MS = 150;
    var teams = document.querySelectorAll('details.team[data-team]');
    var saveTimer = null;

    function read(key) {{
      try {{ return window.localStorage.getItem(PREFIX + key); }}
      catch (e) {{ return null; }}
    }}
    function write(key, open) {{
      try {{ window.localStorage.setItem(PREFIX + key, open ? '1' : '0'); }}
      catch (e) {{ /* storage unavailable: keep working without persistence */ }}
    }}
    function readScroll(key) {{
      try {{
        var raw = window.sessionStorage.getItem(SCROLL_PREFIX + key);
        return raw ? JSON.parse(raw) : null;
      }} catch (e) {{ return null; }}
    }}
    function writeScroll(key, payload) {{
      try {{
        window.sessionStorage.setItem(SCROLL_PREFIX + key, JSON.stringify(payload));
      }} catch (e) {{ /* storage unavailable: rendering continues */ }}
    }}
    function visibleAnchor(list) {{
      var listRect = list.getBoundingClientRect();
      var cards = list.querySelectorAll('.card[data-session]');
      for (var i = 0; i < cards.length; i++) {{
        var rect = cards[i].getBoundingClientRect();
        if (rect.bottom > listRect.top) {{
          return {{ card: cards[i], delta: rect.top - listRect.top }};
        }}
      }}
      return null;
    }}
    function findCard(list, anchor) {{
      if (!anchor) {{ return null; }}
      var cards = list.querySelectorAll('.card[data-session]');
      for (var i = 0; i < cards.length; i++) {{
        if (cards[i].getAttribute('data-session') === anchor) {{ return cards[i]; }}
      }}
      return null;
    }}
    function saveScroll() {{
      try {{
        window.sessionStorage.setItem(PAGE_KEY,
          String(window.pageYOffset || document.documentElement.scrollTop || 0));
      }} catch (e) {{ /* storage unavailable */ }}
      Array.prototype.forEach.call(teams, function (el) {{
        var list = el.querySelector('.session-list');
        if (!list || !el.open) {{ return; }}
        var anchor = visibleAnchor(list);
        writeScroll(el.getAttribute('data-team'), {{
          top: list.scrollTop,
          anchor: anchor ? anchor.card.getAttribute('data-session') : null,
          delta: anchor ? anchor.delta : 0
        }});
      }});
    }}
    function scheduleSave() {{
      if (saveTimer) {{ window.clearTimeout(saveTimer); }}
      saveTimer = window.setTimeout(function () {{
        saveTimer = null;
        saveScroll();
      }}, SAVE_DELAY_MS);
    }}
    function restoreList(el) {{
      var list = el.querySelector('.session-list');
      if (!list || !el.open) {{ return; }}
      var saved = readScroll(el.getAttribute('data-team'));
      if (!saved) {{ return; }}
      var target = Number(saved.top) || 0;
      var card = findCard(list, saved.anchor);
      if (card) {{
        // Keep the same visible card at the same offset from the list top.
        var listRect = list.getBoundingClientRect();
        var cardRect = card.getBoundingClientRect();
        target = list.scrollTop + (cardRect.top - listRect.top) - (Number(saved.delta) || 0);
      }}
      var max = Math.max(0, list.scrollHeight - list.clientHeight);
      list.scrollTop = Math.min(Math.max(0, target), max);
    }}

    // Restore order: page scroll -> open states -> each team list scroll.
    try {{
      var pageTop = window.sessionStorage.getItem(PAGE_KEY);
      if (pageTop !== null) {{ window.scrollTo(0, Number(pageTop) || 0); }}
    }} catch (e) {{ /* storage unavailable */ }}
    Array.prototype.forEach.call(teams, function (el) {{
      var key = el.getAttribute('data-team');
      var stored = read(key);
      if (stored === '1') {{ el.open = true; }}
      else if (stored === '0') {{ el.open = false; }}
      el.addEventListener('toggle', function () {{
        write(key, el.open);
        saveScroll();
      }});
      var list = el.querySelector('.session-list');
      if (list) {{ list.addEventListener('scroll', scheduleSave); }}
    }});
    Array.prototype.forEach.call(teams, function (el) {{ restoreList(el); }});
    window.addEventListener('scroll', scheduleSave);
    window.addEventListener('pagehide', saveScroll);
    window.addEventListener('beforeunload', saveScroll);
  }})();
  </script>
</body>
</html>
""".format(
        refresh=int(refresh_seconds),
        banner=banner,
        awaiting=view.awaiting_count,
        teams=teams_html,
    )


def handle_ack(monitor, team_key=None, session_id=None, source_key=None):
    """Pure local acknowledgement, independently testable without a socket."""
    if session_id:
        source = source_key or getattr(monitor, "source_key", "default")
        return monitor.acknowledge_session(source, session_id)
    if isinstance(team_key, str) and team_key:
        return monitor.acknowledge_team(team_key)
    return False


def make_handler(monitor, stale_threshold, refresh_seconds):
    """Build a request handler bound to this monitor instance."""

    class MonitorHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib API
            path = self.path.split("?", 1)[0]
            if path == "/status.json":
                body = json.dumps(status_json(monitor, stale_threshold)).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif path in ("/", "/index.html"):
                body = render_dashboard(monitor, stale_threshold, refresh_seconds).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - stdlib API
            path = self.path.split("?", 1)[0]
            if path != "/ack":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400)
                return
            if length <= 0 or length > 4096:
                self.send_error(400)
                return
            raw = self.rfile.read(length).decode("utf-8", "replace")
            params = parse_qs(raw, keep_blank_values=False)
            team_key = params.get("team_key", [None])[0]
            session_id = params.get("session_id", [None])[0]
            source_key = params.get("source_key", [None])[0]
            handle_ack(monitor, team_key, session_id, source_key)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
            return

    return MonitorHandler


def serve(monitor, host, port, allow_external=False, stale_threshold=15.0, refresh_seconds=5):
    """Serve the dashboard. Refuse non-loopback binds without an explicit flag."""
    if not is_loopback(host) and not allow_external:
        raise RefusedBind(
            "refusing non-loopback bind %r; pass --allow-external-bind to override" % host
        )

    handler = make_handler(monitor, stale_threshold, refresh_seconds)
    address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = True

    LocalServer.address_family = address_family
    server = LocalServer((host, port), handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
