"""INC027i offline tests — status correctness and stable session UI.

Covers the collector precedence change (a current permission/active state
outranks the previous turn's terminal outcome), the aggregate/render order,
the banner split (neutral outcome-missing vs red transport), the deterministic
per-team sort and the sessionStorage scroll/anchor persistence.

No network, no browser, no real OpenCode binary: the same recording fake CLI
and in-memory HTML harness as the rest of the suite.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor.api import OpencodeApi  # noqa: E402
from monitor.collector import (  # noqa: E402
    BUSY, ERROR, IDLE, READY, UNKNOWN, WAITING_PERMISSION,
    Monitor, SessionSnapshot, aggregate_status,
)
from monitor.config import load_config  # noqa: E402
from monitor.state import ReadyPending, StateStore  # noqa: E402
from monitor.web import render_dashboard, status_json  # noqa: E402

from fake_cli import FakeCli  # noqa: E402

SID = "ses_i1"
NOW = 1005.0
STALE = 15
OMIT = object()  # sentinel: session.get contains no "outcome" key at all


def session_info(sid, directory="/w/i1", outcome="succeeded", updated=1000, title=""):
    return {
        "id": sid,
        "projectID": "project-x",
        "agent": "project-lead",
        "outcome": outcome,
        "time": {"created": 900, "updated": updated},
        "title": title,
        "location": {"directory": directory},
    }


def assistant_msg(mid, text="answer", updated=1000):
    return {
        "id": mid,
        "role": "assistant",
        "time": {"updated": updated},
        "content": [{"type": "text", "text": text}],
    }


def team_entry(key="t1", label="Team One", directory="/w/i1", sid=None):
    entry = {"key": key, "label": label, "worktree_dir": directory}
    if sid is not None:
        entry["session_id"] = sid
    return entry


class CollectorBase(unittest.TestCase):
    """One pinned primary; every status branch reachable through one poll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeCli()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        config = {
            "schema": 1,
            "opencode_bin": "/fake/opencode",
            "server_url": None,
            "poll_interval_seconds": 3,
            "stale_threshold_seconds": STALE,
            "bind": "127.0.0.1",
            "port": 8765,
            "teams": [team_entry(sid=SID)],
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        cfg = load_config(self.config_path)
        api = OpencodeApi(cfg.opencode_bin, server_url=cfg.server_url, runner=self.fake)
        self.store = StateStore(self.state_path)
        self.ready = ReadyPending()
        self.monitor = Monitor(cfg, api, self.store, self.ready)

    def poll(self, *, outcome="succeeded", perms=None, active=(), messages=None):
        self.fake.set_active(list(active))
        info = session_info(SID, outcome="succeeded" if outcome is OMIT else outcome)
        if outcome is OMIT:
            del info["outcome"]
        self.fake.set_session(SID, info)
        self.fake.set_permissions(SID, perms or [])
        self.fake.set_messages(SID, messages or [])
        self.monitor.poll_once()
        return self.monitor.snapshot("t1").sessions[0]


# ---------------------------------------------------------------------------
# D1/D2 — per-session precedence
# ---------------------------------------------------------------------------
class TestSessionPrecedence(CollectorBase):
    def test_failed_outcome_not_active_is_error(self):
        card = self.poll(outcome="failed", messages=[assistant_msg("m1")])
        self.assertEqual(card.status, ERROR)
        self.assertEqual(card.reason, "outcome=failed")

    def test_failed_outcome_with_active_session_is_busy(self):
        card = self.poll(
            outcome="failed", active=[SID], messages=[assistant_msg("m1")]
        )
        self.assertEqual(card.status, BUSY)
        self.assertEqual(card.reason, "session active")

    def test_failed_outcome_with_pending_permission_is_waiting(self):
        card = self.poll(
            outcome="failed", perms=[{"id": "perm-1"}],
            messages=[assistant_msg("m1")],
        )
        self.assertEqual(card.status, WAITING_PERMISSION)
        self.assertEqual(card.reason, "permission request pending")
        self.assertTrue(card.awaiting)

    def test_failed_outcome_with_new_answer_is_still_error_not_ready(self):
        # Baseline already recorded; the failed turn emits an assistant message
        # without text. READY is pending in the store but must NOT be shown.
        self.poll(messages=[assistant_msg("m1")])
        card = self.poll(
            outcome="failed", messages=[assistant_msg("m2"), assistant_msg("m1")]
        )
        self.assertEqual(card.status, ERROR)
        self.assertEqual(card.reason, "outcome=failed")
        self.assertTrue(self.ready.has(SID, self.monitor.source_key))
        self.assertEqual(self.store.last_seen(SID, self.monitor.source_key), "m2")

    def test_succeeded_new_answer_is_ready_then_gezien_idle(self):
        self.poll(messages=[assistant_msg("m1")])
        card = self.poll(messages=[assistant_msg("m2"), assistant_msg("m1")])
        self.assertEqual(card.status, READY)
        self.assertTrue(card.awaiting)
        self.assertTrue(
            self.monitor.acknowledge_session(self.monitor.source_key, SID)
        )
        card = self.poll(messages=[assistant_msg("m2"), assistant_msg("m1")])
        self.assertEqual(card.status, IDLE)
        self.assertFalse(card.awaiting)

    def test_missing_or_unrecognized_outcome_stays_unknown(self):
        card = self.poll(outcome=OMIT, messages=[assistant_msg("m1")])
        self.assertEqual(card.status, UNKNOWN)
        self.assertIn("outcome missing or unrecognized", card.reason)

        card = self.poll(outcome="still-working", messages=[assistant_msg("m1")])
        self.assertEqual(card.status, UNKNOWN)
        self.assertIn("outcome missing or unrecognized", card.reason)

    def test_interrupted_remains_error(self):
        # D7: no evidence to reclassify; kept explicitly as ERROR.
        card = self.poll(outcome="interrupted", messages=[assistant_msg("m1")])
        self.assertEqual(card.status, ERROR)
        self.assertEqual(card.reason, "outcome=interrupted")


# ---------------------------------------------------------------------------
# D3 — aggregate precedence
# ---------------------------------------------------------------------------
def snap(status, sid=None):
    return SessionSnapshot(
        session_id=sid or ("ses_%s" % status.lower()),
        source_key="src", team_key="t", team_label="T", status=status,
    )


class TestTeamAggregate(unittest.TestCase):
    def test_ready_and_busy_each_beat_historical_error(self):
        self.assertEqual(aggregate_status([snap(READY), snap(ERROR)]), READY)
        self.assertEqual(aggregate_status([snap(BUSY), snap(ERROR)]), BUSY)
        self.assertEqual(
            aggregate_status([snap(WAITING_PERMISSION), snap(READY)]),
            WAITING_PERMISSION,
        )
        # Order between READY and BUSY is part of the contract.
        self.assertEqual(aggregate_status([snap(BUSY), snap(READY)]), READY)

    def test_idle_beats_historical_error(self):
        # INC027j D1: every operational state outranks a historical ERROR;
        # ERROR is the aggregate only when no operational state exists.
        self.assertEqual(aggregate_status([snap(IDLE), snap(ERROR)]), IDLE)
        self.assertEqual(aggregate_status([snap(ERROR)]), ERROR)
        self.assertEqual(aggregate_status([snap(ERROR), snap(UNKNOWN)]), ERROR)
        self.assertEqual(aggregate_status([]), UNKNOWN)
        self.assertEqual(aggregate_status([snap(UNKNOWN), snap(UNKNOWN)]), UNKNOWN)

    def test_unknown_siblings_are_ignored_unless_all_unknown(self):
        self.assertEqual(aggregate_status([snap(UNKNOWN), snap(IDLE)]), IDLE)
        self.assertEqual(aggregate_status([snap(UNKNOWN), snap(UNKNOWN)]), UNKNOWN)
        self.assertEqual(aggregate_status([]), UNKNOWN)


# ---------------------------------------------------------------------------
# Web harness (same shape as the INC027h tests, no server)
# ---------------------------------------------------------------------------
class FakeSession:
    def __init__(self, session_id, team_key, team_label, status,
                 source_key="wsl2", summary="hi", reason="", awaiting=None,
                 last_activity_ts=None, updated_at=None):
        self.session_id = session_id
        self.source_key = source_key
        self.team_key = team_key
        self.team_label = team_label
        self.status = status
        self.reason = reason
        self.title = ""
        self.last_activity_ts = last_activity_ts
        self.summary = summary
        self.awaiting = status in (READY, WAITING_PERMISSION) if awaiting is None else awaiting
        self.updated_at = NOW - 5.0 if updated_at is None else updated_at
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
    def __init__(self, teams, incomplete_teams=()):
        self.teams = tuple(teams)
        self.sessions = tuple(s for team in teams for s in team.sessions)
        self.awaiting_count = sum(
            1 for s in self.sessions if s.status in (READY, WAITING_PERMISSION)
        )
        self.any_unknown = any(s.status == UNKNOWN for s in self.sessions)
        self.incomplete_teams = tuple(incomplete_teams)


class FakeMonitor:
    def __init__(self, teams, incomplete_teams=()):
        self.view = FakeView(teams, incomplete_teams)

    def collect_view(self, stale_threshold, now=None):
        return self.view


def session(sid, team_key, team_label, status=IDLE, **kwargs):
    return FakeSession(sid, team_key, team_label, status, **kwargs)


def simple_team(key, label, statuses=(IDLE,), source_key="wsl2"):
    sessions = [
        session("%s_ses_%d" % (key, i), key, label, status,
                source_key=source_key)
        for i, status in enumerate(statuses)
    ]
    return FakeTeam(key, label, sessions, source_key=source_key)


def details_slice(page, key):
    start = page.index('<details class="team" data-team="%s">' % key)
    end = page.index("</details>", start) + len("</details>")
    return page[start:end]


def summary_slice(page, key):
    block = details_slice(page, key)
    return block[block.index("<summary>"):block.index("</summary>")]


def aggregate_status_of(summary):
    match = re.search(
        r'<span class="team-status" style="color:[^"]+">([A-Z_]+)</span>',
        summary,
    )
    if match is None:
        raise AssertionError("aggregate team-status element not found")
    return match.group(1)


def anchors(page, key):
    return re.findall(r'data-session="([^"]*)"', details_slice(page, key))


def six_team_monitor():
    """Six teams; one 23-session worktree: 7 UNKNOWN, 1 READY, 15 IDLE."""
    win = "win-windows"
    sessions = [session("win_ses_ready", "team-windows", "Team Windows", READY,
                        source_key=win, last_activity_ts=100.0)]
    # Newest first inside IDLE: idle_14 (514) ... idle_00 (500).
    for i in range(15):
        sessions.append(session(
            "win_ses_idle_%02d" % i, "team-windows", "Team Windows", IDLE,
            source_key=win, last_activity_ts=500.0 + i,
        ))
    # Newest first inside UNKNOWN: unknown_6 ... unknown_0.
    for i in range(7):
        sessions.append(session(
            "win_ses_unknown_%d" % i, "team-windows", "Team Windows", UNKNOWN,
            source_key=win, last_activity_ts=900.0 + i,
            reason="outcome missing or unrecognized: None",
        ))
    # Deliberately shuffled input order: render order must not depend on it.
    sessions = sessions[8:] + sessions[:8]
    return FakeMonitor([
        simple_team("team-alpha", "Team Alpha"),
        simple_team("team-beta", "Team Beta"),
        simple_team("team-gamma", "Team Gamma"),
        simple_team("team-delta", "Team Delta"),
        simple_team("team-epsilon", "Team Epsilon"),
        FakeTeam("team-windows", "Team Windows", sessions, source_key=win),
    ])


# ---------------------------------------------------------------------------
# D4 — deterministic display sort
# ---------------------------------------------------------------------------
class TestSessionSort(unittest.TestCase):
    def expected_anchors(self):
        win = "win-windows"
        return (
            [win + "|win_ses_ready"]
            + [win + "|win_ses_unknown_%d" % i for i in range(6, -1, -1)]
            + [win + "|win_ses_idle_%02d" % i for i in range(14, -1, -1)]
        )

    def test_action_first_then_activity_across_remaining_statuses(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        self.assertEqual(anchors(page, "team-windows"), self.expected_anchors())
        block = details_slice(page, "team-windows")
        self.assertEqual(block.count('class="card"'), 23)
        self.assertIn("sessions: 23", block)
        # Bounded list container is unchanged.
        self.assertIn(
            ".session-list { max-height: 22rem; overflow-y: auto;", page
        )

    def test_deterministic_tie_break_on_source_and_session_id(self):
        sessions = [
            session("tie_b", "tie", "Tie", IDLE, source_key="wsl2",
                    last_activity_ts=700.0),
            session("tie_a", "tie", "Tie", IDLE, source_key="wsl2",
                    last_activity_ts=700.0),
            session("tie_c", "tie", "Tie", IDLE, source_key="alpha",
                    last_activity_ts=700.0),
        ]
        mon = FakeMonitor([FakeTeam("tie", "Tie", sessions)])
        page = render_dashboard(mon, STALE, 5, now=NOW)
        self.assertEqual(
            anchors(page, "tie"),
            ["alpha|tie_c", "wsl2|tie_a", "wsl2|tie_b"],
        )

    def test_missing_last_activity_sorts_last_within_status(self):
        sessions = [
            session("no_ts", "t", "T", IDLE, last_activity_ts=None),
            session("has_ts", "t", "T", IDLE, last_activity_ts=10.0),
        ]
        page = render_dashboard(
            FakeMonitor([FakeTeam("t", "T", sessions)]), STALE, 5, now=NOW
        )
        self.assertEqual(anchors(page, "t"), ["wsl2|has_ts", "wsl2|no_ts"])

    def test_status_json_keeps_view_order_and_shape(self):
        mon = six_team_monitor()
        payload = status_json(mon, STALE, now=NOW)
        self.assertEqual(
            [s["session_id"] for s in payload["sessions"]],
            [s.session_id for s in mon.view.sessions],
        )
        self.assertEqual(
            set(payload),
            {"teams", "awaiting_count", "sessions", "banners", "metrics"},
        )
        self.assertEqual(
            set(payload["sessions"][0]),
            {
                "session_id", "source_key", "team_key", "team_label", "status",
                "reason", "title", "last_activity", "awaiting", "summary",
                "membership", "grace_count", "age_seconds", "stale",
            },
        )


# ---------------------------------------------------------------------------
# D6 — banner split
# ---------------------------------------------------------------------------
def banner_page(sessions, incomplete_teams=()):
    team = FakeTeam("b", "Banner", sessions)
    return render_dashboard(
        FakeMonitor([team], incomplete_teams), STALE, 5, now=NOW
    )


class TestBannerSplit(unittest.TestCase):
    def test_outcome_missing_only_is_neutral_and_not_connection_lost(self):
        page = banner_page([
            session("s1", "b", "Banner", UNKNOWN,
                    reason="outcome missing or unrecognized: None"),
        ])
        self.assertIn('class="banner info" role="status"', page)
        self.assertIn("no terminal outcome was available", page)
        self.assertIn("NOT a connection failure", page)
        self.assertNotIn("Connection lost", page)
        # Fail-closed values are unchanged.
        self.assertIn('<div class="status" style="color:#8a8a8a">UNKNOWN</div>', page)

    def test_session_error_is_red_with_connection_lost_and_count(self):
        page = banner_page([
            session("s1", "b", "Banner", UNKNOWN,
                    reason="session error: OSError('down')"),
        ])
        self.assertIn("Connection lost", page)
        self.assertIn("for 1 session(s)", page)
        self.assertNotIn('class="banner info"', page)

    def test_stale_snapshot_is_red(self):
        page = banner_page([
            session("s1", "b", "Banner", IDLE, updated_at=NOW - 100.0),
        ])
        self.assertIn("Connection lost", page)
        self.assertIn("for 1 session(s)", page)
        self.assertNotIn('class="banner info"', page)

    def test_archived_unknown_raises_no_banner(self):
        page = banner_page([
            session("s1", "b", "Banner", UNKNOWN, reason="session archived"),
        ])
        self.assertNotIn("Connection lost", page)
        self.assertNotIn('class="banner info"', page)
        self.assertNotIn('class="banner incomplete"', page)

    def test_incomplete_discovery_yellow_banner_unchanged(self):
        page = banner_page(
            [session("s1", "b", "Banner", IDLE)],
            incomplete_teams=(("b", "Banner", "win: discovery incomplete"),),
        )
        self.assertIn('class="banner incomplete" role="alert"', page)
        self.assertIn("Discovery incomplete", page)
        self.assertNotIn("Connection lost", page)

    def test_mixed_causes_split_into_two_banners(self):
        page = banner_page([
            session("s1", "b", "Banner", UNKNOWN,
                    reason="outcome missing or unrecognized: None"),
            session("s2", "b", "Banner", UNKNOWN,
                    reason="session error: boom"),
        ])
        self.assertIn("Connection lost", page)
        self.assertIn("for 1 session(s)", page)
        self.assertIn('class="banner info" role="status"', page)
        self.assertIn("Status unavailable for 1 session(s)", page)


# ---------------------------------------------------------------------------
# D5 — scroll/anchor persistence markup (logic-level, no browser)
# ---------------------------------------------------------------------------
class TestScrollPersistence(unittest.TestCase):
    def script(self):
        page = render_dashboard(six_team_monitor(), STALE, 5, now=NOW)
        return page, page[page.index("<script>"):page.index("</script>")]

    def test_markup_carries_session_anchors(self):
        page, _script = self.script()
        anchor = "win-windows|win_ses_ready"
        self.assertIn('data-session="%s"' % anchor, page)
        self.assertEqual(len(anchors(page, "team-windows")), 23)

    def test_sessionstorage_save_and_restore_identifiers(self):
        _page, script = self.script()
        for token in (
            "window.sessionStorage.setItem", "window.sessionStorage.getItem",
            "'inc027i-scroll:'", "'inc027i-page-scroll'",
            "JSON.stringify", "JSON.parse", "window.setTimeout",
            "window.clearTimeout", "getBoundingClientRect", "scrollHeight",
            "clientHeight", "addEventListener('pagehide'", "beforeunload",
            "addEventListener('scroll'", "delta", "saved.anchor",
        ):
            self.assertIn(token, script)
        # The INC027h open-state key is reused, so current state survives.
        self.assertIn("'inc027h-team-open:'", script)

    def test_restore_order_page_then_open_then_list(self):
        _page, script = self.script()
        self.assertLess(script.index("var pageTop"), script.index("var stored = read(key)"))
        self.assertLess(
            script.index("var stored = read(key)"),
            script.rindex("restoreList(el);"),
        )
        # Anchor lookup falls back to the stored top when the card is gone.
        self.assertIn("var target = Number(saved.top) || 0;", script)
        self.assertIn("Math.max(0, list.scrollHeight - list.clientHeight)", script)

    def test_no_automatic_scroll_to_ready_or_new_session(self):
        _page, script = self.script()
        self.assertNotIn("scrollIntoView", script)
        self.assertNotIn("READY", script)
        # Anchoring is by data-session attribute, never by a status selector.
        self.assertIn("'.card[data-session]'", script)

    def test_data_session_value_is_html_escaped(self):
        evil = session(
            'ses<1>', "x", "X", READY, source_key='s"x',
        )
        page = render_dashboard(
            FakeMonitor([FakeTeam("x", "X", [evil])]), STALE, 5, now=NOW
        )
        self.assertIn('data-session="s&quot;x|ses&lt;1&gt;"', page)
        self.assertNotIn('data-session="s"x|ses<1>"', page)


if __name__ == "__main__":
    unittest.main()
