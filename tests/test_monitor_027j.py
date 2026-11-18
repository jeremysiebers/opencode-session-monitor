"""INC027j offline tests — current team status and activity-based ordering.

Covers the D1 header precedence (ERROR only when no operational state exists),
the D2 render order (action, BUSY, then remaining statuses by activity), the
unchanged fail-closed banners, `status_json` contract, Gezien/source isolation
and scroll/anchor stability.

No network, no browser, no real OpenCode binary: a recording fake CLI feeds the
real ``Monitor``/state path for status changes; a small in-memory HTML harness
is used where precise status/activity fixtures are clearer.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor.api import OpencodeApi  # noqa: E402
from monitor.collector import (  # noqa: E402
    BUSY, ERROR, IDLE, READY, UNKNOWN, WAITING_PERMISSION,
    Monitor, SnapshotRegistry, aggregate_status,
)
from monitor.config import load_config  # noqa: E402
from monitor.state import ReadyPending, StateStore  # noqa: E402
from monitor.web import render_dashboard, status_json  # noqa: E402

from fake_cli import FakeCli  # noqa: E402

STALE = 15
NOW = 1005.0

# Exact per-session ``status_json`` key set (INC027e METRICS + INC027j unchanged).
SESSION_KEYS = {
    "session_id", "source_key", "team_key", "team_label", "status",
    "reason", "title", "last_activity", "awaiting", "summary",
    "membership", "grace_count", "age_seconds", "stale",
}


def assistant(mid, updated_ms, text="answer"):
    return {
        "id": mid,
        "role": "assistant",
        "time": {"updated": updated_ms},
        "content": [{"type": "text", "text": text}],
    }


class JFixture:
    """Real ``Monitor`` over a recording fake CLI (no network/process)."""

    def __init__(self, testcase, source_key="wsl2", store=None, ready=None,
                 registry=None):
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.source_key = source_key
        self.fake = FakeCli()
        self.store = store or StateStore(
            os.path.join(self._tmp.name, "state-%s.json" % source_key)
        )
        self.ready = ready or ReadyPending()
        self.registry = registry
        self._teams = {}
        self._data = {}
        self.monitor = None

    def add_team(self, key, label, directory):
        self._teams[key] = {"label": label, "dir": directory, "order": []}
        return key

    def add_session(self, team_key, sid, outcome="succeeded",
                    updated_ms=1_000_000, active=False, messages=None,
                    perms=None):
        self._teams[team_key]["order"].append(sid)
        if messages is None:
            messages = [assistant(sid + "-m1", updated_ms)]
        self._data[sid] = {
            "team": team_key,
            "outcome": outcome,
            "updated_ms": updated_ms,
            "active": active,
            "perms": list(perms or []),
            "messages": list(messages),
        }

    def update(self, sid, **changes):
        data = self._data[sid]
        if "messages" in changes:
            data["messages"] = list(changes["messages"])
        for key in ("outcome", "updated_ms", "active", "perms"):
            if key in changes:
                data[key] = changes[key]

    def _apply(self):
        self.fake.set_active(
            [sid for sid, data in self._data.items() if data["active"]]
        )
        self.fake.set_session_list([
            sid for team in self._teams.values() for sid in team["order"]
        ])
        for sid, data in self._data.items():
            team = self._teams[data["team"]]
            info = {
                "id": sid,
                "projectID": "project-j",
                "agent": "project-lead",
                "outcome": data["outcome"],
                "time": {"created": 900, "updated": data["updated_ms"]},
                "title": "",
                "location": {"directory": team["dir"]},
            }
            self.fake.set_session(sid, info)
            self.fake.set_messages(sid, data["messages"])
            self.fake.set_permissions(sid, data["perms"])

    def build(self):
        self._apply()
        config = {
            "schema": 1,
            "opencode_bin": "/fake/opencode",
            "server_url": None,
            "poll_interval_seconds": 3,
            "stale_threshold_seconds": STALE,
            "bind": "127.0.0.1",
            "port": 8765,
            "source_key": self.source_key,
            "teams": [
                {"key": key, "label": team["label"],
                 "worktree_dir": team["dir"]}
                for key, team in self._teams.items()
            ],
        }
        path = os.path.join(self._tmp.name, "config-%s.json" % self.source_key)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        cfg = load_config(path)
        api = OpencodeApi(
            cfg.opencode_bin, server_url=cfg.server_url, runner=self.fake
        )
        self.monitor = Monitor(
            cfg, api, self.store, self.ready, registry=self.registry
        )
        return self.monitor

    def poll(self):
        self._apply()
        self.monitor.poll_once()


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------
def details_slice(page, key):
    start = page.index('<details class="team" data-team="%s">' % key)
    end = page.index("</details>", start) + len("</details>")
    return page[start:end]


def summary_slice(page, key):
    block = details_slice(page, key)
    return block[block.index("<summary>"):block.index("</summary>")]


def team_status_of(page, key):
    summary = summary_slice(page, key)
    match = re.search(
        r'<span class="team-status" style="color:[^"]+">([A-Z_]+)</span>',
        summary,
    )
    if match is None:
        raise AssertionError("team-status element not found")
    return match.group(1)


def rendered_statuses(page, key):
    return re.findall(
        r'<div class="status" style="color:[^"]+">([A-Z_]+)</div>',
        details_slice(page, key),
    )


def anchors(page, key):
    return re.findall(r'data-session="([^"]*)"', details_slice(page, key))


# ---------------------------------------------------------------------------
# AC-j1…j4, j7, j10 — real Monitor/state path
# ---------------------------------------------------------------------------
class ScenarioBase(unittest.TestCase):
    def setUp(self):
        fx = JFixture(self)
        fx.add_team("busy-team", "Busy Team", "/w/j/busy")
        fx.add_session("busy-team", "ses_jit_busy_ok", active=True)
        for i in range(7):
            fx.add_session("busy-team", "ses_jit_busy_idle_%d" % i)
        fx.add_session("busy-team", "ses_jit_busy_err", outcome="failed")

        fx.add_team("ready-team", "Ready Team", "/w/j/ready")
        fx.add_session("ready-team", "ses_jit_ready_ok")
        fx.add_session("ready-team", "ses_jit_ready_err", outcome="failed")

        fx.add_team("idle-team", "Idle Team", "/w/j/idle")
        for i in range(7):
            fx.add_session("idle-team", "ses_jit_idle_%d" % i)
        fx.add_session("idle-team", "ses_jit_idle_err", outcome="failed")

        fx.add_team("error-team", "Error Team", "/w/j/error")
        fx.add_session("error-team", "ses_jit_err_a", outcome="failed")
        fx.add_session("error-team", "ses_jit_err_b", outcome="failed")

        fx.build()
        fx.poll()  # baseline
        fx.update("ses_jit_ready_ok", messages=[
            assistant("ses_jit_ready_ok-m2", 1_000_100),
            assistant("ses_jit_ready_ok-m1", 1_000_000),
        ])
        fx.poll()  # READY for the ready team's healthy session
        self.fx = fx
        self.monitor = fx.monitor
        self.now = time.monotonic()

    def page(self):
        return render_dashboard(self.monitor, STALE, 5, now=self.now)

    def payload(self):
        return status_json(self.monitor, STALE, now=self.now)


class TestHeaderPrecedence(ScenarioBase):
    def test_busy_beats_idle_and_historical_error(self):
        # AC-j1: 1 BUSY + 7 IDLE + 1 historical ERROR.
        page = self.page()
        block = details_slice(page, "busy-team")
        summary = summary_slice(page, "busy-team")
        self.assertEqual(team_status_of(page, "busy-team"), BUSY)
        self.assertIn('<span class="badge error">ERROR 1</span>', summary)
        self.assertEqual(rendered_statuses(page, "busy-team").count(BUSY), 1)
        self.assertEqual(rendered_statuses(page, "busy-team").count(ERROR), 1)
        self.assertIn(
            '<div class="status" style="color:#c0392b">ERROR</div>', block
        )
        self.assertIn(
            '<div class="status" style="color:#e08b16">BUSY</div>', block
        )
        # Only the READY team's healthy session is awaiting; the ERROR does not
        # inflate or suppress the count.
        self.assertEqual(self.payload()["awaiting_count"], 1)

    def test_ready_beats_historical_error(self):
        # AC-j2: 1 READY + 1 historical ERROR.
        page = self.page()
        block = details_slice(page, "ready-team")
        summary = summary_slice(page, "ready-team")
        self.assertEqual(team_status_of(page, "ready-team"), READY)
        self.assertIn('<span class="badge error">ERROR 1</span>', summary)
        self.assertIn(
            '<div class="status" style="color:#1f9d55">READY</div>', block
        )
        self.assertIn('<form method="post" action="/ack">', block)
        self.assertIn(">Gezien</button>", block)

    def test_idle_beats_historical_error(self):
        # AC-j3: 7 IDLE + 1 historical ERROR.
        page = self.page()
        block = details_slice(page, "idle-team")
        summary = summary_slice(page, "idle-team")
        self.assertEqual(team_status_of(page, "idle-team"), IDLE)
        self.assertIn('<span class="badge error">ERROR 1</span>', summary)
        statuses = rendered_statuses(page, "idle-team")
        self.assertEqual(statuses.count(IDLE), 7)
        self.assertEqual(statuses.count(ERROR), 1)
        self.assertIn(
            '<div class="status" style="color:#5b7a99">IDLE</div>', block
        )

    def test_error_only_team_has_no_fabricated_idle(self):
        # AC-j4: two ERROR sessions, no IDLE card or counter.
        page = self.page()
        block = details_slice(page, "error-team")
        summary = summary_slice(page, "error-team")
        self.assertEqual(team_status_of(page, "error-team"), ERROR)
        self.assertIn('<span class="badge error">ERROR 2</span>', summary)
        self.assertNotIn("IDLE", summary)
        statuses = rendered_statuses(page, "error-team")
        self.assertEqual(statuses, [ERROR, ERROR])
        self.assertNotIn('color:#5b7a99">IDLE</div>', block)

    def test_aggregate_helper_matches_d1(self):
        # Direct collector contract (INC027j): IDLE > historical ERROR.
        def snap(status, sid):
            from monitor.collector import SessionSnapshot
            return SessionSnapshot(
                session_id=sid, source_key="src", team_key="t",
                team_label="T", status=status,
            )
        self.assertEqual(
            aggregate_status([snap(IDLE, "a"), snap(ERROR, "b")]), IDLE
        )
        self.assertEqual(
            aggregate_status([snap(ERROR, "a"), snap(UNKNOWN, "b")]), ERROR
        )
        self.assertEqual(aggregate_status([]), UNKNOWN)
        self.assertEqual(
            aggregate_status([snap(UNKNOWN, "a"), snap(UNKNOWN, "b")]), UNKNOWN
        )


class TestStatusJsonContract(ScenarioBase):
    def test_team_status_matches_rendered_header_and_shape(self):
        # AC-j10.
        page = self.page()
        payload = self.payload()
        by_key = {team["key"]: team["status"] for team in payload["teams"]}
        for key in ("busy-team", "ready-team", "idle-team", "error-team"):
            self.assertEqual(by_key[key], team_status_of(page, key))
        self.assertEqual(
            set(payload),
            # INC027l: additive top-level "version"; all prior keys kept.
            {"teams", "awaiting_count", "sessions", "banners", "metrics", "version"},
        )
        view = self.monitor.collect_view(STALE, self.now)
        self.assertEqual(
            [session["session_id"] for session in payload["sessions"]],
            [session.session_id for session in view.sessions],
        )
        self.assertEqual(set(payload["sessions"][0]), SESSION_KEYS)

    def test_all_unknown_and_waiting_team_status_contract(self):
        """AC-j10 extra fixtures: all-UNKNOWN and WAITING_PERMISSION + IDLE."""
        fx = JFixture(self)
        fx.add_team("unknown-team", "Unknown Team", "/w/j/unknown")
        fx.add_session("unknown-team", "ses_unk_a", outcome="still-working")
        fx.add_session("unknown-team", "ses_unk_b", outcome="still-working")
        fx.add_team("perm-team", "Permission Team", "/w/j/perm")
        fx.add_session("perm-team", "ses_perm_wait",
                       perms=[{"id": "perm-1"}])
        fx.add_session("perm-team", "ses_perm_idle")
        fx.build()
        fx.poll()
        now = time.monotonic()
        page = render_dashboard(fx.monitor, STALE, 5, now=now)
        payload = status_json(fx.monitor, STALE, now=now)
        by_key = {team["key"]: team["status"] for team in payload["teams"]}

        # All-UNKNOWN team: header and counter.
        self.assertEqual(by_key["unknown-team"], UNKNOWN)
        self.assertEqual(team_status_of(page, "unknown-team"), UNKNOWN)
        unknown_summary = summary_slice(page, "unknown-team")
        self.assertIn(
            '<span class="badge unknown">UNKNOWN 2</span>', unknown_summary
        )
        self.assertIn(
            '<span class="badge action">\u26a0 actie 0</span>',
            unknown_summary,
        )

        # WAITING_PERMISSION + IDLE: WAITING outranks IDLE at team level.
        self.assertEqual(by_key["perm-team"], WAITING_PERMISSION)
        self.assertEqual(
            team_status_of(page, "perm-team"), WAITING_PERMISSION
        )
        perm_summary = summary_slice(page, "perm-team")
        self.assertIn(
            '<span class="badge unknown">UNKNOWN 0</span>', perm_summary
        )
        self.assertIn(
            '<span class="badge action">\u26a0 actie 1</span>', perm_summary
        )
        self.assertEqual(payload["awaiting_count"], 1)
        self.assertEqual(set(payload["sessions"][0]), SESSION_KEYS)


# ---------------------------------------------------------------------------
# AC-j5 — fail-closed banners unchanged (in-memory harness)
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
        self.awaiting = (
            status in (READY, WAITING_PERMISSION)
            if awaiting is None else awaiting
        )
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
            1 for s in self.sessions
            if s.status in (READY, WAITING_PERMISSION)
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


class TestFailClosedBanners(unittest.TestCase):
    def banner_page(self, sessions, incomplete_teams=()):
        team = FakeTeam("b", "Banner", sessions)
        return render_dashboard(
            FakeMonitor([team], incomplete_teams), STALE, 5, now=NOW
        )

    def test_session_error_keeps_red_connection_lost_banner(self):
        page = self.banner_page([
            session("s1", "b", "Banner", UNKNOWN,
                    reason="session error: OSError('down')"),
        ])
        self.assertIn("Connection lost", page)
        self.assertIn("for 1 session(s)", page)
        self.assertNotIn('class="banner info"', page)

    def test_incomplete_discovery_keeps_yellow_banner(self):
        page = self.banner_page(
            [session("s1", "b", "Banner", IDLE)],
            incomplete_teams=(("b", "Banner", "win: discovery incomplete"),),
        )
        self.assertIn('class="banner incomplete" role="alert"', page)
        self.assertIn("Discovery incomplete", page)
        self.assertNotIn("Connection lost", page)

    def test_outcome_missing_stays_neutral(self):
        page = self.banner_page([
            session("s1", "b", "Banner", UNKNOWN,
                    reason="outcome missing or unrecognized: None"),
        ])
        self.assertIn('class="banner info" role="status"', page)
        self.assertNotIn("Connection lost", page)


# ---------------------------------------------------------------------------
# AC-j6/j7 — D2 activity ordering, UNKNOWN participation, tie-break
# ---------------------------------------------------------------------------
class TestActivityOrder(unittest.TestCase):
    def test_action_then_busy_then_activity_across_remaining_statuses(self):
        sessions = [
            # Group 0 (action) despite older activity than the BUSY card.
            session("ready_old", "t", "T", READY, last_activity_ts=NOW - 50),
            # Group 1 (BUSY) despite newest activity.
            session("busy_new", "t", "T", BUSY, last_activity_ts=NOW),
            # Group 2: pure activity order, status ignored.
            session("recent_idle", "t", "T", IDLE, last_activity_ts=NOW - 1),
            session("recent_error", "t", "T", ERROR, last_activity_ts=NOW - 2),
            session("unknown_mid", "t", "T", UNKNOWN, last_activity_ts=NOW - 3),
            # Same activity: deterministic (source_key, session_id).
            session("tie_b", "t", "T", IDLE, source_key="wsl2",
                    last_activity_ts=NOW - 55),
            session("tie_a", "t", "T", ERROR, source_key="wsl2",
                    last_activity_ts=NOW - 55),
            session("tie_c", "t", "T", UNKNOWN, source_key="alpha",
                    last_activity_ts=NOW - 55),
            # Older operational IDLE still beats the 9-day-old ERROR.
            session("old_idle", "t", "T", IDLE, last_activity_ts=NOW - 100),
            session("old_error", "t", "T", ERROR,
                    last_activity_ts=NOW - 9 * 86400),
            # No timestamp: last within group 2.
            session("no_ts", "t", "T", IDLE, last_activity_ts=None),
        ]
        mon = FakeMonitor([FakeTeam("t", "T", sessions)])
        page = render_dashboard(mon, STALE, 5, now=NOW)
        self.assertEqual(
            anchors(page, "t"),
            [
                "wsl2|ready_old",
                "wsl2|busy_new",
                "wsl2|recent_idle",
                "wsl2|recent_error",
                "wsl2|unknown_mid",
                "alpha|tie_c", "wsl2|tie_a", "wsl2|tie_b",
                "wsl2|old_idle",
                "wsl2|old_error",
                "wsl2|no_ts",
            ],
        )
        order = anchors(page, "t")
        # Explicit AC-j6 checks.
        self.assertLess(order.index("wsl2|recent_idle"),
                        order.index("wsl2|old_error"))
        self.assertLess(order.index("wsl2|recent_error"),
                        order.index("wsl2|old_idle"))
        self.assertLess(order.index("wsl2|unknown_mid"),
                        order.index("wsl2|old_idle"))
        self.assertEqual(order[-1], "wsl2|no_ts")


# ---------------------------------------------------------------------------
# AC-j8 — Gezien and source isolation
# ---------------------------------------------------------------------------
class TestGezienAndSources(unittest.TestCase):
    def test_ready_acknowledged_to_idle_with_error_sibling_unchanged(self):
        fx = JFixture(self)
        fx.add_team("gez", "Gezien", "/w/j/gez")
        fx.add_session("gez", "ses_ok")
        fx.add_session("gez", "ses_err", outcome="failed")
        fx.build()
        fx.poll()
        fx.update("ses_ok", messages=[
            assistant("ses_ok-m2", 1_000_100),
            assistant("ses_ok-m1", 1_000_000),
        ])
        fx.poll()
        now = time.monotonic()
        self.assertEqual(
            team_status_of(render_dashboard(fx.monitor, STALE, 5, now=now), "gez"),
            READY,
        )
        self.assertTrue(
            fx.monitor.acknowledge_session("wsl2", "ses_ok")
        )
        now = time.monotonic()
        page = render_dashboard(fx.monitor, STALE, 5, now=now)
        payload = status_json(fx.monitor, STALE, now=now)
        statuses = {
            s["session_id"]: s["status"] for s in payload["sessions"]
        }
        self.assertEqual(statuses["ses_ok"], IDLE)
        self.assertEqual(statuses["ses_err"], ERROR)
        self.assertEqual(team_status_of(page, "gez"), IDLE)
        block = details_slice(page, "gez")
        self.assertNotIn('<form method="post" action="/ack">', block)
        self.assertIn(
            '<div class="status" style="color:#c0392b">ERROR</div>', block
        )

    def test_same_session_id_on_two_sources_remains_distinct(self):
        shared_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(shared_tmp.cleanup)
        store = StateStore(
            os.path.join(shared_tmp.name, "shared-state.json")
        )
        ready = ReadyPending()
        registry = SnapshotRegistry()
        wsl = JFixture(self, source_key="wsl2", store=store, ready=ready,
                       registry=registry)
        wsl.add_team("iso-wsl", "Iso WSL", "/w/iso/wsl")
        wsl.add_session("iso-wsl", "ses_shared")
        win = JFixture(self, source_key="win", store=store, ready=ready,
                       registry=registry)
        win.add_team("iso-win", "Iso Win", "/w/iso/win")
        win.add_session("iso-win", "ses_shared")
        wsl.build()
        win.build()
        wsl.poll()
        win.poll()

        wsl.update("ses_shared", messages=[
            assistant("ses_shared-m2", 1_000_100),
            assistant("ses_shared-m1", 1_000_000),
        ])
        wsl.poll()
        now = time.monotonic()
        view = wsl.monitor.collect_view(STALE, now)
        both = [s for s in view.sessions if s.session_id == "ses_shared"]
        self.assertEqual(len(both), 2)
        self.assertEqual({s.source_key for s in both}, {"wsl2", "win"})
        self.assertEqual(
            {s.source_key: s.status for s in both},
            {"wsl2": READY, "win": IDLE},
        )
        self.assertTrue(wsl.monitor.acknowledge_session("wsl2", "ses_shared"))
        now = time.monotonic()
        view = wsl.monitor.collect_view(STALE, now)
        after = {
            s.source_key: s for s in view.sessions
            if s.session_id == "ses_shared"
        }
        self.assertEqual(after["wsl2"].status, IDLE)
        self.assertEqual(after["wsl2"].reason, "acknowledged locally")
        self.assertEqual(after["win"].status, IDLE)
        self.assertEqual(after["win"].reason, "idle")


# ---------------------------------------------------------------------------
# AC-j9 — five renders/polls keep anchors, scroll/open markup stable
# ---------------------------------------------------------------------------
class TestStability(unittest.TestCase):
    def test_five_changing_polls_keep_anchors_and_no_auto_scroll(self):
        fx = JFixture(self)
        fx.add_team("stable", "Stable", "/w/j/stable")
        fx.add_session("stable", "ses_a")
        fx.add_session("stable", "ses_b")
        fx.add_session("stable", "ses_c")
        fx.add_session("stable", "ses_err", outcome="failed")
        fx.build()
        fx.poll()

        expected_anchors = {
            "wsl2|ses_a", "wsl2|ses_b", "wsl2|ses_c", "wsl2|ses_err",
        }
        for i in range(5):
            for sid in ("ses_a", "ses_b", "ses_c"):
                fx.update(sid, updated_ms=1_000_000 + (i + 1) * 1_000)
            if i == 2:
                # Add a READY state mid-sequence.
                fx.update("ses_c", messages=[
                    assistant("ses_c-m2", 1_001_000),
                    assistant("ses_c-m1", 1_000_000),
                ])
            fx.poll()
            now = time.monotonic()
            page = render_dashboard(fx.monitor, STALE, 5, now=now)
            self.assertEqual(set(anchors(page, "stable")), expected_anchors)
            self.assertEqual(len(anchors(page, "stable")), 4)
            script = page[page.index("<script>"):page.index("</script>")]
            for token in (
                "'inc027i-scroll:'", "'inc027i-page-scroll'",
                "'inc027h-team-open:'", "window.sessionStorage.setItem",
                "window.sessionStorage.getItem", "localStorage.setItem",
                "getAttribute('data-team')", "addEventListener('toggle'",
                "el.open", "'.card[data-session]'", "var target = Number(saved.top) || 0;",
            ):
                self.assertIn(token, script)
            self.assertNotIn("scrollIntoView", script)
            self.assertNotIn("READY", script)
            # The header may legitimately change but anchors/scripts must not.
            self.assertIn('<div class="session-list">', page)


if __name__ == "__main__":
    unittest.main()
