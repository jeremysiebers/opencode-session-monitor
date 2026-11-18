"""INC027h offline tests — compact collapsible per-team dashboard.

UI-only change (``monitor/web.py``): six responsive team containers rendered as
closed ``<details class="team">`` with a collapsed summary, bounded internal
session scroll, and localStorage persistence across the existing meta refresh.
No backend field, endpoint, ack path or status precedence is changed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor.collector import (  # noqa: E402
    BUSY, ERROR, IDLE, READY, UNKNOWN, WAITING_PERMISSION,
)
from monitor.web import render_dashboard, status_json  # noqa: E402

NOW = 1005.0
STALE = 15


class FakeSession:
    def __init__(self, session_id, team_key, team_label, status,
                 source_key="wsl2", summary="hi", reason="", awaiting=None):
        self.session_id = session_id
        self.source_key = source_key
        self.team_key = team_key
        self.team_label = team_label
        self.status = status
        self.reason = reason
        self.title = ""
        self.last_activity_ts = None
        self.summary = summary
        self.awaiting = status in (READY, WAITING_PERMISSION) if awaiting is None else awaiting
        self.updated_at = NOW - 5.0
        self.membership = "PRESENT"
        self.grace_count = 0
        self.incomplete = False
        self.child_ids = ()
        self.child_busy = False


class FakeTeam:
    def __init__(self, key, label, sessions, source_key="wsl2"):
        self.key = key
        self.label = label
        self.source_key = source_key
        self.sessions = tuple(sessions)
        self.status = UNKNOWN
        self.reason = ""
        self.title = ""
        self.last_activity_ts = None
        self.awaiting = any(s.awaiting for s in sessions)
        self.summary = ""
        self.discovery_complete = True
        self.discovery_reason = ""
        self.overflow = False


class FakeView:
    def __init__(self, teams):
        self.teams = tuple(teams)
        self.sessions = tuple(s for team in teams for s in team.sessions)
        self.awaiting_count = sum(
            1 for s in self.sessions
            if s.status in (READY, WAITING_PERMISSION)
        )
        self.any_unknown = any(s.status == UNKNOWN for s in self.sessions)
        self.incomplete_teams = ()


class FakeMonitor:
    def __init__(self, teams):
        self.view = FakeView(teams)

    def collect_view(self, stale_threshold, now=None):
        return self.view


def session(sid, team_key, team_label, status=IDLE, **kwargs):
    return FakeSession(sid, team_key, team_label, status, **kwargs)


def team(key, label, statuses=(IDLE,), source_key="wsl2"):
    sessions = [
        session("%s_ses_%d" % (key, i), key, label, status, source_key=source_key)
        for i, status in enumerate(statuses)
    ]
    return FakeTeam(key, label, sessions, source_key=source_key)


def six_team_monitor():
    return FakeMonitor([
        team("team-alpha", "Team Alpha", (IDLE,)),
        team("team-beta", "Team Beta", (IDLE,)),
        team("team-gamma", "Team Gamma", (IDLE,)),
        team("team-delta", "Team Delta", (IDLE,)),
        team("team-epsilon", "Team Epsilon", (IDLE,)),
        team("team-windows", "Team Windows", (IDLE,) * 23, source_key="win-windows"),
    ])


def details_slice(page, key):
    start = page.index('<details class="team" data-team="%s">' % key)
    end = page.index("</details>", start) + len("</details>")
    return page[start:end]


def summary_slice(page, key):
    block = details_slice(page, key)
    return block[block.index("<summary>"):block.index("</summary>")]


def aggregate_status(summary):
    match = re.search(
        r'<span class="team-status" style="color:[^"]+">([A-Z_]+)</span>',
        summary,
    )
    if match is None:
        raise AssertionError("aggregate team-status element not found")
    return match.group(1)


class TestCollapsibleTeams(unittest.TestCase):
    def test_six_team_containers_rendered_as_details(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        keys = re.findall(r'<details class="team" data-team="([^"]+)">', page)
        self.assertEqual(
            keys,
            ["team-alpha", "team-beta", "team-gamma", "team-delta", "team-epsilon", "team-windows"],
        )
        for label in ("Team Alpha", "Team Beta", "Team Gamma", "Team Delta",
                      "Team Epsilon", "Team Windows"):
            self.assertIn(label, page)
        self.assertEqual(page.count('<div class="session-list">'), 6)

    def test_summary_shows_label_count_status_and_badges(self):
        mon = FakeMonitor([team(
            "alpha", "Alpha",
            (READY, ERROR, BUSY, UNKNOWN, WAITING_PERMISSION),
        )])
        page = render_dashboard(mon, STALE, 5, now=NOW)
        block = details_slice(page, "alpha")
        summary = block[block.index("<summary>"):block.index("</summary>")]
        self.assertIn("Alpha", summary)
        self.assertIn("sessions: 5", summary)
        self.assertIn('class="team-status" style="color:', summary)
        self.assertIn("ERROR", summary)  # ERROR badge remains visible
        self.assertIn("READY 1", summary)
        self.assertIn("BUSY 1", summary)
        self.assertIn("ERROR 1", summary)
        self.assertIn("UNKNOWN 1", summary)
        self.assertIn("actie 2", summary)  # READY + WAITING_PERMISSION
        # Counts live in the collapsed summary, not only in expanded cards.
        self.assertNotIn("<summary>", summary.replace("<summary>", "", 1))

    def test_asymmetric_counts_and_aggregate_are_exact(self):
        """F1: asymmetric counts + separate aggregate element.

        One READY per status hides a swapped READY<->BUSY / ERROR<->UNKNOWN
        mapping, and a bare "ERROR" substring also matches the ERROR badge.
        This fixture makes every count distinct and reads the aggregate from
        its own element for teams whose aggregate is WAITING_PERMISSION,
        READY and ERROR.
        """
        mon = FakeMonitor([
            team("asym", "Asymmetric", (
                READY, READY, BUSY, BUSY, BUSY, ERROR, WAITING_PERMISSION, IDLE, IDLE,
            )),
            team("perm", "Permission", (WAITING_PERMISSION, IDLE)),
            team("ready", "Ready", (READY, IDLE)),
            team("err", "Error only", (ERROR,)),
        ])
        page = render_dashboard(mon, STALE, 5, now=NOW)

        summary = summary_slice(page, "asym")
        self.assertIn("sessions: 9", summary)
        # Exact badge elements: a swapped count mapping changes the number.
        self.assertRegex(
            summary, r'<span class="badge ready">READY 2</span>'
        )
        self.assertRegex(
            summary, r'<span class="badge busy">BUSY 3</span>'
        )
        self.assertRegex(
            summary, r'<span class="badge error">ERROR 1</span>'
        )
        self.assertRegex(
            summary, r'<span class="badge unknown">UNKNOWN 0</span>'
        )
        self.assertRegex(
            summary, r'<span class="badge action">\u26a0 actie 3</span>'
        )
        # Aggregate read from the team-status element, not a bare substring.
        # INC027j D1: WAITING_PERMISSION/READY/BUSY/IDLE all outrank a
        # historical ERROR; an ERROR-only team (no IDLE fabricated) is ERROR.
        self.assertEqual(aggregate_status(summary), WAITING_PERMISSION)
        self.assertEqual(
            aggregate_status(summary_slice(page, "perm")), WAITING_PERMISSION
        )
        self.assertEqual(aggregate_status(summary_slice(page, "ready")), READY)
        self.assertEqual(aggregate_status(summary_slice(page, "err")), ERROR)

    def test_details_default_closed_without_open_attribute(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        for match in re.finditer(r"<details\b[^>]*>", page):
            self.assertNotIn(" open", " " + match.group(0))

    def test_session_list_is_bounded_scroll_container(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        self.assertIn(
            ".session-list { max-height: 22rem; overflow-y: auto;", page
        )
        for key in ("team-alpha", "team-windows"):
            block = details_slice(page, key)
            self.assertIn('<div class="session-list">', block)

    def test_ready_session_keeps_gezien_form_fields(self):
        mon = FakeMonitor([team("alpha", "Alpha", (READY,))])
        page = render_dashboard(mon, STALE, 5, now=NOW)
        block = details_slice(page, "alpha")
        self.assertIn('<form method="post" action="/ack">', block)
        self.assertIn('name="team_key" value="alpha"', block)
        self.assertIn('name="source_key" value="wsl2"', block)
        self.assertIn('name="session_id" value="alpha_ses_0"', block)
        self.assertIn(">Gezien</button>", block)
        # Per-session READY status text/colour is preserved in the card.
        self.assertIn('<div class="status" style="color:#1f9d55">READY</div>', block)

    def test_responsive_grid_media_queries(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        self.assertIn(
            "grid-template-columns: repeat(3, minmax(0, 1fr));", page
        )
        self.assertIn("@media (max-width: 1100px)", page)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr));", page
        )
        self.assertIn("@media (max-width: 700px)", page)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", page)

    def test_localstorage_persistence_script_keys_on_data_team(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        script = page[page.index("<script>"):page.index("</script>")]
        self.assertIn("localStorage.setItem", script)
        self.assertIn("localStorage.getItem", script)
        self.assertIn("getAttribute('data-team')", script)
        self.assertIn('addEventListener(\'toggle\'', script)
        self.assertIn("el.open", script)
        self.assertIn("try", script)
        self.assertIn("catch", script)
        # Must run after the <details> elements exist (script at end of body).
        self.assertGreater(page.index("<script>"), page.index('<div class="teams">'))
        self.assertLess(page.index("<script>"), page.index("</body>"))

    def test_23_session_team_has_23_cards_inside_bounded_list(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        block = details_slice(page, "team-windows")
        marker = '<div class="session-list">'
        start = block.index(marker) + len(marker)
        sessions_html = block[start:block.rindex("</div>")]
        self.assertEqual(sessions_html.count('class="card"'), 23)
        self.assertIn("sessions: 23", block)
        self.assertIn("READY 0", block)
        self.assertIn("actie 0", block)

    def test_team_label_and_key_are_html_escaped(self):
        evil_label = '<img src=x onerror="alert(1)">'
        evil_key = 'a"b'
        mon = FakeMonitor([team(evil_key, evil_label, (READY,))])
        page = render_dashboard(mon, STALE, 5, now=NOW)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x", page)
        self.assertIn('data-team="a&quot;b"', page)
        self.assertNotIn('data-team="a"b"', page)


class TestStatusJsonUnchanged(unittest.TestCase):
    def test_status_json_payload_is_byte_identical_fixture(self):
        mon = FakeMonitor([team("alpha", "Alpha", (READY,))])
        payload = status_json(mon, STALE, now=NOW)
        expected = (
            '{"teams": [{"key": "alpha", "label": "Alpha", "source_key": "wsl2", '
            '"status": "READY", "reason": "", "title": "", '
            '"last_activity": "\\u2014", "awaiting": true, "summary": ""}], '
            '"awaiting_count": 1, "sessions": [{"session_id": "alpha_ses_0", '
            '"source_key": "wsl2", "team_key": "alpha", "team_label": "Alpha", '
            '"status": "READY", "reason": "", "title": "", '
            '"last_activity": "\\u2014", "awaiting": true, "summary": "hi", '
            '"membership": "PRESENT", "grace_count": 0, "age_seconds": 5.0, '
            '"stale": false}], "banners": [], "metrics": {}}'
        )
        self.assertEqual(json.dumps(payload), expected)
        # Rendering the dashboard must not mutate the JSON view or its output.
        render_dashboard(mon, STALE, 5, now=NOW)
        self.assertEqual(json.dumps(status_json(mon, STALE, now=NOW)), expected)


if __name__ == "__main__":
    unittest.main()
