"""INC027l offline tests — v1.0.0 version source, CLI, JSON and dashboard.

No network, no browser, no real OpenCode binary and no live monitor contact:
a small in-memory view fixture drives the real ``status_json`` and
``render_dashboard``, and the CLI is exercised as a throwaway subprocess that
must exit before any config, state, server or API work happens.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor import __main__ as monitor_main  # noqa: E402
from monitor.collector import (  # noqa: E402
    BUSY, IDLE, READY, ERROR, UNKNOWN, WAITING_PERMISSION,
)
from monitor.version import __version__  # noqa: E402
from monitor.web import render_dashboard, status_json  # noqa: E402

NOW = 1005.0
STALE = 15
EXPECTED_OUTPUT = "OpenCode Session Monitor 1.0.0"

# The exact top-level keys ``status_json`` returned before this increment.
PRIOR_TOP_LEVEL_KEYS = ["teams", "awaiting_count", "sessions", "banners", "metrics"]
# The exact per-session key set, unchanged by this increment.
SESSION_KEYS = {
    "session_id", "source_key", "team_key", "team_label", "status",
    "reason", "title", "last_activity", "awaiting", "summary",
    "membership", "grace_count", "age_seconds", "stale",
}


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
            1 for s in self.sessions if s.status in (READY, WAITING_PERMISSION)
        )
        self.any_unknown = any(s.status == UNKNOWN for s in self.sessions)
        self.incomplete_teams = ()


class FakeMonitor:
    def __init__(self, teams):
        self.view = FakeView(teams)

    def collect_view(self, stale_threshold, now=None):
        return self.view


def team(key, label, statuses=(IDLE,), source_key="wsl2"):
    sessions = [
        FakeSession("%s_ses_%d" % (key, i), key, label, status, source_key=source_key)
        for i, status in enumerate(statuses)
    ]
    return FakeTeam(key, label, sessions, source_key=source_key)


def six_team_monitor():
    return FakeMonitor([
        team("team-alpha", "Team Alpha", (IDLE, READY)),
        team("team-beta", "Team Beta", (BUSY,)),
        team("team-gamma", "Team Gamma", (IDLE,)),
        team("team-delta", "Team Delta", (IDLE,)),
        team("team-epsilon", "Team Epsilon", (ERROR,)),
        team("team-windows", "Team Windows", (IDLE,) * 23, source_key="win-windows"),
    ])


def header_region(page):
    """The server-rendered header: from <body> up to the awaiting count."""
    start = page.index("<body>")
    end = page.index('<div class="count">', start)
    return page[start:end]


class TestVersionSource(unittest.TestCase):
    def test_version_is_semver_1_0_0(self):
        self.assertEqual(__version__, "1.0.0")
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_only_version_module_defines_a_version(self):
        monitor_dir = os.path.join(BASE, "monitor")
        definition = re.compile(r"^\s*__version__\s*=", re.MULTILINE)
        offenders = []
        for name in sorted(os.listdir(monitor_dir)):
            if not name.endswith(".py") or name == "version.py":
                continue
            with open(os.path.join(monitor_dir, name), "r", encoding="utf-8") as handle:
                if definition.search(handle.read()):
                    offenders.append(name)
        self.assertEqual(offenders, [])


class TestCliVersion(unittest.TestCase):
    def test_module_version_flag_prints_version_and_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "monitor", "--version"],
            cwd=BASE, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode("utf-8").strip(), EXPECTED_OUTPUT)
        self.assertNotIn("CONFIG ERROR", result.stderr.decode("utf-8"))

    def test_version_exits_before_config_or_server_work(self):
        # Any config load or serve attempt fails the test: --version must exit
        # from argparse before touching config/state/server/API.
        captured = io.StringIO()
        with patch("monitor.__main__.load_config",
                   side_effect=AssertionError("config accessed by --version")), \
                patch("monitor.__main__.serve",
                      side_effect=AssertionError("server started by --version")), \
                contextlib.redirect_stdout(captured):
            with self.assertRaises(SystemExit) as caught:
                monitor_main.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(captured.getvalue().strip(), EXPECTED_OUTPUT)

    def test_version_works_without_any_config_or_state_file(self):
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run(
                [sys.executable, "-m", "monitor", "--version"],
                cwd=empty,
                env=dict(os.environ, PYTHONPATH=BASE),
                capture_output=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.decode("utf-8").strip(), EXPECTED_OUTPUT)
            self.assertEqual(os.listdir(empty), [])


class TestStatusJsonVersion(unittest.TestCase):
    def payload(self):
        return status_json(six_team_monitor(), STALE, now=NOW)

    def test_version_is_present(self):
        self.assertEqual(self.payload()["version"], "1.0.0")

    def test_existing_top_level_keys_and_order_are_preserved(self):
        payload = self.payload()
        self.assertEqual(list(payload), PRIOR_TOP_LEVEL_KEYS + ["version"])
        for key in PRIOR_TOP_LEVEL_KEYS:
            self.assertIn(key, payload)

    def test_version_is_the_only_added_top_level_key(self):
        payload = self.payload()
        self.assertEqual(set(payload) - set(PRIOR_TOP_LEVEL_KEYS), {"version"})
        self.assertEqual(set(PRIOR_TOP_LEVEL_KEYS) - set(payload), set())

    def test_per_session_key_set_is_unchanged(self):
        payload = self.payload()
        self.assertTrue(payload["sessions"])
        for session in payload["sessions"]:
            self.assertEqual(set(session), SESSION_KEYS)


class TestDashboardVersion(unittest.TestCase):
    def page(self):
        return render_dashboard(six_team_monitor(), STALE, 5, now=NOW)

    def test_title_and_version_span(self):
        page = self.page()
        self.assertIn("<h1>OpenCode Session Monitor</h1>", page)
        self.assertIn('<span class="version">\u2014 v1.0.0</span>', page)

    def test_version_appears_before_the_team_grid_and_exactly_once(self):
        page = self.page()
        version = '<span class="version">\u2014 v1.0.0</span>'
        self.assertEqual(page.count("v1.0.0"), 1)
        self.assertLess(page.index(version), page.index('<div class="teams">'))
        self.assertEqual(header_region(page).count("v1.0.0"), 1)

    def test_six_team_containers_and_meta_refresh_are_unchanged(self):
        page = self.page()
        self.assertEqual(len(re.findall(r'<details class="team" data-team="', page)), 6)
        self.assertIn('<meta http-equiv="refresh" content="5">', page)

    def test_topbar_css_wraps_and_keeps_version_visible(self):
        page = self.page()
        self.assertIn(".topbar { display: flex; align-items: baseline;", page)
        self.assertIn("flex-wrap: wrap;", page)
        self.assertIn(".version { font-size: .95rem; color: #555;", page)
        self.assertIn("white-space: nowrap;", page)


class TestLauncherVersion(unittest.TestCase):
    def test_ocmon_forwards_version(self):
        launcher = os.path.join(BASE, "ocmon")
        if not os.path.exists(launcher):
            self.skipTest("ocmon launcher not present in this tree")
        result = subprocess.run(
            ["sh", "./ocmon", "--version"],
            cwd=BASE, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode("utf-8").strip(), EXPECTED_OUTPUT)


if __name__ == "__main__":
    unittest.main()
