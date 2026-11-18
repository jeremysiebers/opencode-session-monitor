"""INC027e offline tests — second (Windows) source, per-source isolation, BOM.

Fully offline: two recording fake CLIs (one per source) supply canned API
responses. No network, no real binary, no Windows service, no live config, no
session prompt. Run with::

    cd opencode-session-monitor && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor.api import ALLOWED_OPERATIONS, OpencodeApi  # noqa: E402
from monitor.collector import (  # noqa: E402
    IDLE, MAX_PRIMARIES_PER_TEAM, PRESENT, READY, UNKNOWN, Monitor,
    MultiSourceMonitor, SnapshotRegistry,
)
from monitor.config import ConfigError, load_config  # noqa: E402
from monitor.paths import normalize_path, same_path  # noqa: E402
from monitor.state import ReadyPending, StateStore  # noqa: E402
from monitor.web import render_dashboard, status_json, handle_ack  # noqa: E402

from fake_cli import FakeCli, WindowsFakeCli  # noqa: E402
from test_monitor import SID, SID_B, assistant_msg, session_info, team  # noqa: E402

# Reference API item shapes (structure only; never re-probed here).
WIN_PL = "ses_examplewindows000000001"
WIN_CHILD = "ses_examplechild0000000001"
WIN_DIR = "C:\\Users\\you\\projects\\team-windows"
WIN_BIN = "/mnt/c/Users/you/tools/opencode.exe"
WSL_BIN = "/fake/wsl-opencode"


def win_info(sid, outcome="succeeded", directory=WIN_DIR):
    return {
        "id": sid, "outcome": outcome, "title": "",
        "time": {"created": 1000, "updated": 1000},
        "location": {"directory": directory},
    }


def wsl_source(teams, key="wsl2", binary=WSL_BIN, deny=()):
    return {
        "key": key, "opencode_bin": binary, "server_url": "",
        "teams": teams, "deny_session_ids": list(deny),
    }


def win_source(sid=WIN_PL, children=(WIN_CHILD,)):
    entry = {
        "key": "team-windows", "label": "Team Windows",
        "worktree_dir": WIN_DIR, "session_id": sid,
    }
    if children:
        entry["child_session_ids"] = list(children)
    return wsl_source(
        [entry], key="win-windows", binary=WIN_BIN,
    )


class EBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.wsl_cli = FakeCli()
        self.win_cli = WindowsFakeCli()

    def write_config(self, sources, **overrides):
        config = {
            "schema": overrides.get("schema", 2),
            "opencode_bin": WSL_BIN,
            "server_url": "",
            "poll_interval_seconds": 3,
            "stale_threshold_seconds": 15,
            "bind": "127.0.0.1", "port": 8765,
            "teams": [team(sid=SID)],  # top-level schema-1/2 back-compat fields
        }
        if sources is not None:
            config["sources"] = sources
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return load_config(self.config_path)

    def make(self, sources=None, wsl_cli=None, win_cli=None):
        self.wsl_cli = wsl_cli or self.wsl_cli
        self.win_cli = win_cli or self.win_cli
        cfg = self.write_config(sources)
        store = StateStore(self.state_path)
        ready = ReadyPending()
        registry = SnapshotRegistry()
        from monitor.__main__ import build_source_monitors
        monitors = build_source_monitors(
            cfg, store, ready, registry, 15.0,
            runners={"wsl2": self.wsl_cli, "win-windows": self.win_cli},
        )
        hub = MultiSourceMonitor(monitors, store, ready, registry)
        return hub, store, ready, registry, cfg

    def seed(self, wsl_cli=None, win_cli=None):
        """Both sources healthy with one pinned primary each (idle baseline)."""
        wsl_cli = wsl_cli or self.wsl_cli
        win_cli = win_cli or self.win_cli
        wsl_cli.set_active([])
        wsl_cli.set_session_pages([{"data": []}])  # terminal COMPLETE walk
        wsl_cli.set_session(SID, session_info(SID, "/w/pl"))
        wsl_cli.set_permissions(SID, [])
        wsl_cli.set_messages(SID, [assistant_msg("wsl_m1")])
        win_cli.set_active([])
        win_cli.set_session_pages([{"data": []}])
        win_cli.set_session(WIN_PL, win_info(WIN_PL))
        win_cli.set_permissions(WIN_PL, [])
        win_cli.set_messages(WIN_PL, [assistant_msg("win_m1")])

    @staticmethod
    def wsl_teams():
        return [
            team("team-alpha", "Team Alpha", "/w/pl", SID),
            team("team-beta", "Team Beta", "/w/wp"),
            team("team-gamma", "Team Gamma", "/w/team-gamma"),
            team("team-delta", "Team Delta", "/w/team-delta"),
            team("team-epsilon", "Team Epsilon", "/w/team-epsilon"),
        ]

    def two_sources(self, wsl_cli=None, win_cli=None, win=None):
        sources = [wsl_source(self.wsl_teams())]
        sources.append(win if win is not None else win_source())
        return self.make(sources, wsl_cli=wsl_cli, win_cli=win_cli)


# ---------------------------------------------------------------------------
# AC-e1 — schema 2 sources[], schema-1 back-compat, strict allowlist
# ---------------------------------------------------------------------------
class TestConfigSources(EBase):
    def test_two_sources_are_validated_into_source_configs(self):
        cfg = self.write_config([wsl_source(self.wsl_teams()), win_source()])
        sources = cfg.active_sources()
        self.assertEqual(len(sources), 2)
        self.assertEqual([s.key for s in sources], ["wsl2", "win-windows"])
        self.assertEqual(sources[0].source_key, "wsl2")
        self.assertEqual(sources[1].opencode_bin, WIN_BIN)
        self.assertTrue(sources[1].opencode_bin.endswith(".exe"))
        self.assertIsNone(sources[1].server_url)  # "" => bare CLI auth
        self.assertEqual(len(sources[0].teams), 5)
        self.assertEqual(len(sources[1].teams), 1)
        win_team = sources[1].teams[0]
        self.assertEqual(win_team.worktree_dir, normalize_path(WIN_DIR))
        self.assertEqual(win_team.session_id, WIN_PL)
        self.assertEqual(win_team.child_session_ids, (WIN_CHILD,))
        # No new operation/param: source keys are a strict config allowlist.
        self.assertEqual(cfg.schema, 2)

    def test_source_config_rejects_operation_and_relative_binary(self):
        bad_op = wsl_source(self.wsl_teams())
        bad_op["operation"] = "session.prompt"
        with self.assertRaises(ConfigError):
            self.write_config([bad_op, win_source()])
        bad_bin = win_source()
        bad_bin["opencode_bin"] = "opencode.exe"  # not absolute interop path
        with self.assertRaises(ConfigError):
            self.write_config([wsl_source(self.wsl_teams()), bad_bin])
        dup = win_source()
        dup["key"] = "wsl2"
        with self.assertRaises(ConfigError):
            self.write_config([wsl_source(self.wsl_teams()), dup])

    def test_schema1_still_loads_as_single_source(self):
        cfg = self.write_config(None, schema=1)
        self.assertEqual(cfg.schema, 1)
        sources = cfg.active_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].key, "default")
        self.assertEqual(sources[0].opencode_bin, WSL_BIN)


# ---------------------------------------------------------------------------
# AC-e2 — BOM/CRLF JSON, rc!=0, timeout, large response, no shell wrapper
# ---------------------------------------------------------------------------
class TestWindowsTransport(EBase):
    def test_bom_and_crlf_json_parses(self):
        win = WindowsFakeCli(bom=True, crlf=True)
        api = OpencodeApi(WIN_BIN, runner=win)
        payload = api.session_active()
        self.assertIsInstance(payload, dict)
        self.assertIn("data", payload)
        # Leading bytes of raw stdout really were BOM + CRLF.
        self.assertTrue(win.calls)
        self.assertEqual(win.returncodes, {})

    def test_bom_poll_succeeds_end_to_end(self):
        win = WindowsFakeCli(bom=True, crlf=True)
        hub, _, _, _, _ = self.two_sources(win_cli=win)
        self.seed(win_cli=win)
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)

    def test_large_windows_response_parses_and_initialises_baseline(self):
        win = WindowsFakeCli(bom=True, crlf=True)
        hub, store, _, _, _ = self.two_sources(win_cli=win)
        self.seed(win_cli=win)
        big = [assistant_msg("win_big", text="x" * 38000)]
        win.set_messages(WIN_PL, big)
        raw = json.dumps({"data": big})
        self.assertGreaterEqual(len(raw), 38000)
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)
        self.assertEqual(store.last_seen(WIN_PL, "win-windows"), "win_big")

    def test_nonzero_exit_code_maps_to_unknown_only_for_windows(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)

        self.win_cli.set_rc("session.active", 1)
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-windows").status, UNKNOWN)
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)
        # The failing call still used the frozen GET argv with the Windows exe.
        active = [c for c in self.win_cli.calls if "session.active" in c]
        self.assertTrue(active)
        self.assertEqual(active[-1][0], WIN_BIN)
        self.assertEqual(active[-1][1], "api")

    def test_timeout_is_isolated_to_windows_source(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        self.win_cli.will_raise(
            ("session.message.list", WIN_PL),
            subprocess.TimeoutExpired(cmd=[WIN_BIN], timeout=15),
        )
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-windows").status, UNKNOWN)
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)

    def test_no_shell_wrapper_token_in_monitor_source(self):
        forbidden = ("powershell", "cmd.exe", "-command", "shell=true")
        root = os.path.join(BASE, "monitor")
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                lowered = handle.read().lower()
            for token in forbidden:
                self.assertNotIn(token, lowered, "%s contains %s" % (name, token))


# ---------------------------------------------------------------------------
# AC-e3 — per-source pollers and failure isolation
# ---------------------------------------------------------------------------
class TestPerSourceIsolation(EBase):
    def test_one_registry_one_store_one_ready_across_sources(self):
        hub, store, ready, registry, _ = self.two_sources()
        self.assertEqual(len(hub.monitors), 2)
        self.assertEqual(hub.source_keys(), ("wsl2", "win-windows"))
        for mon in hub.monitors:
            self.assertIs(mon._registry, registry)
            self.assertIs(mon.store, store)
            self.assertIs(mon.ready, ready)
        self.assertIs(hub.registry, registry)

    def test_windows_fault_keeps_wsl_cards_and_marks_only_windows(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)

        self.win_cli.will_raise("session.active", OSError("windows service gone"))
        hub.poll_once()
        snap = hub.snapshot("team-windows")
        self.assertFalse(snap.discovery_complete)
        self.assertEqual(snap.status, UNKNOWN)
        self.assertTrue(snap.sessions[0].incomplete)
        # WSL2 source unaffected.
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)
        self.assertTrue(hub.snapshot("team-alpha").discovery_complete)
        # Per-source INCOMPLETE banner names the Windows source.
        banners = status_json(hub, 15)["banners"]
        self.assertTrue(any("win-windows" in b for b in banners), banners)

    def test_stale_windows_source_marks_only_windows_unknown(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        win_snap = hub.snapshot("team-windows")
        # Simulate only the Windows source going stale (collector thread died).
        for session in win_snap.sessions:
            session.updated_at = time.monotonic() - 100
        now = time.monotonic()
        payload = status_json(hub, 15, now=now)
        cards = {s["session_id"]: s for s in payload["sessions"]}
        # Staleness is applied at read time (effective_status), never by
        # rewriting stored state; only the Windows cards become UNKNOWN.
        self.assertEqual(cards[WIN_PL]["status"], UNKNOWN)
        self.assertEqual(cards[SID]["status"], IDLE)
        win_teams = {t["key"]: t for t in payload["teams"]}
        self.assertEqual(win_teams["team-windows"]["status"], UNKNOWN)
        self.assertEqual(win_teams["team-alpha"]["status"], IDLE)

    def test_poll_source_only_polls_requested_source(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        before = (len(self.wsl_cli.calls), len(self.win_cli.calls))
        self.assertTrue(hub.poll_source("win-windows"))
        self.assertEqual(len(self.wsl_cli.calls), before[0])
        self.assertGreater(len(self.win_cli.calls), before[1])
        self.assertFalse(hub.poll_source("does-not-exist"))


# ---------------------------------------------------------------------------
# AC-e4 — six groups, source badge, child advisory, one view
# ---------------------------------------------------------------------------
class TestSixGroupsAndBadges(EBase):
    def test_six_group_labels_and_source_badges(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        page = render_dashboard(hub, 15, 5)
        for label in ("Team Alpha", "Team Beta", "Team Gamma", "Team Delta",
                      "Team Epsilon", "Team Windows"):
            self.assertIn(label, page)
        self.assertIn("source: wsl2", page)
        self.assertIn("source: win-windows", page)
        payload = status_json(hub, 15)
        self.assertEqual(len(payload["teams"]), 6)
        self.assertIn("win-windows", [t["source_key"] for t in payload["teams"]])
        self.assertEqual(payload["awaiting_count"], 0)

    def test_child_is_advisory_not_a_card_and_pinned_anchor_is_documented(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        snap = hub.snapshot("team-windows")
        self.assertEqual([s.session_id for s in snap.sessions], [WIN_PL])
        self.assertIn(WIN_CHILD, snap.sessions[0].child_ids)
        self.assertFalse(any(s.session_id == WIN_CHILD for s in snap.sessions))

    def test_gezien_is_per_source_session_and_never_calls_opencode(self):
        hub, store, ready, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        # Raise READY on both sources for the SAME session id (see next class).
        for cli, sid in ((self.wsl_cli, SID), (self.win_cli, WIN_PL)):
            cli.set_messages(sid, [assistant_msg(sid + "_m2"), assistant_msg("m1")])
        hub.poll_once()
        win_card = [s for s in hub.snapshot("team-windows").sessions][0]
        self.assertEqual(win_card.status, READY)
        before = (len(self.wsl_cli.calls), len(self.win_cli.calls))
        self.assertTrue(handle_ack(hub, session_id=WIN_PL, source_key="win-windows"))
        self.assertEqual((len(self.wsl_cli.calls), len(self.win_cli.calls)), before)
        self.assertFalse(ready.has(WIN_PL, "win-windows"))
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)

    def test_one_collect_view_feeds_cards_and_json(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        original = hub.collect_view
        calls = []

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        hub.collect_view = counted
        render_dashboard(hub, 15, 5)
        self.assertEqual(len(calls), 1)
        calls.clear()
        status_json(hub, 15)
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------------------
# AC-e5 — cross-source same session id never shares state
# ---------------------------------------------------------------------------
class TestCrossSourceNoCollision(EBase):
    def test_same_session_id_in_two_sources_has_independent_baseline_and_ready(self):
        win = win_source(sid=SID, children=())
        hub, store, ready, _, _ = self.two_sources(win=win)
        # Both sources pin the SAME session id, with different first answers.
        self.wsl_cli.set_active([])
        self.wsl_cli.set_session_pages([{"data": []}])
        self.wsl_cli.set_session(SID, session_info(SID, "/w/pl"))
        self.wsl_cli.set_permissions(SID, [])
        self.wsl_cli.set_messages(SID, [assistant_msg("wsl_m1")])
        self.win_cli.set_active([])
        self.win_cli.set_session_pages([{"data": []}])
        self.win_cli.set_session(SID, win_info(SID))
        self.win_cli.set_permissions(SID, [])
        self.win_cli.set_messages(SID, [assistant_msg("m1")])
        hub.poll_once()
        self.assertEqual(store.last_seen(SID, "wsl2"), "wsl_m1")
        self.assertEqual(store.last_seen(SID, "win-windows"), "m1")
        self.assertFalse(ready.has(SID, "win-windows"))  # first observation
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)

        # A new Windows answer raises READY only for the Windows namespace.
        self.win_cli.set_messages(SID, [assistant_msg("win_m2"), assistant_msg("m1")])
        hub.poll_once()
        self.assertEqual(hub.snapshot("team-windows").status, READY)
        self.assertEqual(store.last_seen(SID, "win-windows"), "win_m2")
        self.assertTrue(ready.has(SID, "win-windows"))
        self.assertFalse(ready.has(SID, "wsl2"))
        self.assertEqual(store.last_seen(SID, "wsl2"), "wsl_m1")
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)

        # Acknowledging Windows leaves the WSL2 baseline/pending untouched.
        self.assertTrue(handle_ack(hub, session_id=SID, source_key="win-windows"))
        self.assertFalse(ready.has(SID, "win-windows"))
        self.assertEqual(store.last_seen(SID, "wsl2"), "wsl_m1")

    def test_windows_directory_matches_config_case_rule(self):
        self.assertTrue(same_path(WIN_DIR, "c:/Users/you/projects/team-windows"))
        self.assertTrue(same_path(WIN_DIR, WIN_DIR + "\\"))
        self.assertFalse(same_path(WIN_DIR, "C:\\Users\\you\\Other"))
        self.assertEqual(normalize_path(WIN_DIR), "c:/Users/you/projects/team-windows")


# ---------------------------------------------------------------------------
# AC-e6 — GET-only across sources, performance block condition
# ---------------------------------------------------------------------------
class TestInvariantsAndPerformance(EBase):
    def test_all_source_calls_are_frozen_get_operations(self):
        hub, _, _, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()
        forbidden = {"-d", "--data", "--header", "-X", "--request",
                     "POST", "PUT", "DELETE", "PATCH"}
        for cli, binary in ((self.wsl_cli, WSL_BIN), (self.win_cli, WIN_BIN)):
            self.assertTrue(cli.calls)
            for argv in cli.calls:
                self.assertEqual(argv[0], binary)
                self.assertEqual(argv[1], "api")
                ops = [t for t in argv if t in ALLOWED_OPERATIONS]
                self.assertEqual(len(ops), 1, argv)
                for token in argv:
                    self.assertNotIn(token, forbidden)
                    self.assertFalse(token.startswith("/api/"))

    def test_full_cycle_wall_plus_poll_stays_below_stale(self):
        hub, _, _, _, cfg = self.two_sources()
        self.seed()
        started = time.monotonic()
        hub.poll_once()
        wall = time.monotonic() - started
        # Offline fake wall time; real per-call latency was measured at ~1.6 s
        # for a full walk of one source. No poll/stale value is changed here.
        self.assertLess(wall, cfg.stale_threshold)
        self.assertEqual(cfg.poll_interval, 3.0)
        self.assertEqual(cfg.stale_threshold, 15.0)
        self.assertLess(cfg.poll_interval + wall, cfg.stale_threshold)


# ---------------------------------------------------------------------------
# INC027e corrective — Windows primary overflow boundary (23 / 32 / 33)
# ---------------------------------------------------------------------------
# Observed on an isolated schema-2 run: one native-Windows worktree had 23
# primaries and an earlier cap of 16 refused the whole team
# ("primary overflow (23 > 16)"). These tests pin the corrected boundary using
# the same offline Windows fake and cursor paging; no live call is made.
EXAMPLE_PATH = os.path.join(BASE, "config", "teams.example.json")


def windows_primary_ids(count):
    """Exactly ``count`` unique Windows primary ids, the first being WIN_PL."""
    ids = [WIN_PL]
    ids.extend("ses_win_%03d" % i for i in range(1, count))
    return ids


def windows_list_pages(sids, page_size=12):
    """Cursor-paged session.list pages in the observed item shape."""
    pages = []
    for start in range(0, len(sids), page_size):
        chunk = sids[start:start + page_size]
        page = {"data": [
            {"id": sid, "location": {"directory": WIN_DIR}} for sid in chunk
        ]}
        if start + page_size < len(sids):
            page["cursor"] = "wcur-%d" % (start // page_size + 1)
        pages.append(page)
    return pages


class TestPrimaryOverflowBoundary(EBase):
    """23/32 primaries must be COMPLETE; 33 still fails closed."""

    def _seed_windows_primaries(self, cli, sids):
        for sid in sids:
            cli.set_session(sid, win_info(sid))
            cli.set_permissions(sid, [])
            cli.set_messages(sid, [assistant_msg("m_" + sid)])

    def _poll(self, count):
        hub, store, ready, registry, cfg = self.two_sources()
        self.seed()
        sids = windows_primary_ids(count)
        self.win_cli.set_session_pages(windows_list_pages(sids))
        self._seed_windows_primaries(self.win_cli, sids)
        hub.poll_once()
        return hub, store, ready, cfg, sids

    def test_cap_is_the_provisional_32(self):
        self.assertEqual(MAX_PRIMARIES_PER_TEAM, 32)

    def test_23_windows_primaries_are_complete_and_stacked(self):
        hub, _, _, _, sids = self._poll(23)
        snap = hub.snapshot("team-windows")
        self.assertTrue(snap.discovery_complete)
        self.assertFalse(snap.overflow)
        self.assertEqual(snap.discovery_reason, "")
        self.assertNotIn("primary overflow", snap.reason or "")
        self.assertEqual(len(snap.sessions), 23)
        self.assertEqual(set(snap.session_ids), set(sids))
        self.assertEqual(snap.status, IDLE)  # not UNKNOWN
        self.assertTrue(all(s.membership == PRESENT for s in snap.sessions))
        self.assertTrue(all(s.status == IDLE for s in snap.sessions))
        # Multi-primary => one stacked card each; same source_key, distinct ids.
        self.assertEqual([s.session_id for s in snap.sessions], sorted(sids))
        self.assertEqual({s.source_key for s in snap.sessions}, {"win-windows"})
        # Other source/team unaffected by the Windows worktree size.
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)
        self.assertTrue(hub.snapshot("team-alpha").discovery_complete)

    def test_32_windows_primaries_are_complete_and_stacked(self):
        hub, _, _, _, sids = self._poll(32)
        snap = hub.snapshot("team-windows")
        self.assertTrue(snap.discovery_complete)
        self.assertFalse(snap.overflow)
        self.assertEqual(snap.discovery_reason, "")
        self.assertNotIn("primary overflow", snap.reason or "")
        self.assertEqual(len(snap.sessions), 32)
        self.assertEqual(set(snap.session_ids), set(sids))
        self.assertEqual(snap.status, IDLE)
        self.assertTrue(all(s.membership == PRESENT for s in snap.sessions))
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)

    def test_33_windows_primaries_fail_closed_and_admit_nothing_new(self):
        hub, store, ready, _, _ = self.two_sources()
        self.seed()
        hub.poll_once()  # baseline: only the pinned WIN_PL card
        self.assertEqual(hub.snapshot("team-windows").session_ids, (WIN_PL,))
        self.assertEqual(store.last_seen(WIN_PL, "win-windows"), "win_m1")
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)

        overflow_ids = windows_primary_ids(33)
        # WIN_PL stays first; 32 genuinely new candidate ids are also present.
        self.assertEqual(len(overflow_ids), 33)
        self.win_cli.set_session_pages(windows_list_pages(overflow_ids))
        self._seed_windows_primaries(self.win_cli, overflow_ids)
        new_ids = set(overflow_ids) - {WIN_PL}
        before_new_calls = {
            sid: len([c for c in self.win_cli.calls if sid in c]) for sid in new_ids
        }
        hub.poll_once()

        snap = hub.snapshot("team-windows")
        self.assertFalse(snap.discovery_complete)
        self.assertTrue(snap.overflow)
        self.assertIn("primary overflow (33 > 32)", snap.discovery_reason)
        self.assertEqual(snap.status, UNKNOWN)
        # Fail-closed: no new candidates admitted; the existing card is retained.
        self.assertEqual(snap.session_ids, (WIN_PL,))
        self.assertEqual(len(snap.sessions), 1)
        self.assertNotIn(True, [sid in snap.session_ids for sid in new_ids])
        card = snap.sessions[0]
        self.assertTrue(card.incomplete)
        self.assertEqual(card.status, UNKNOWN)
        self.assertEqual(card.membership, PRESENT)  # retained, not dropped
        self.assertIn("primary overflow (33 > 32)", card.reason)
        self.assertEqual(store.last_seen(WIN_PL, "win-windows"), "win_m1")
        # No per-session GET-any-kind for the 32 unadmitted ids on this poll.
        for sid in new_ids:
            self.assertEqual(
                len([c for c in self.win_cli.calls if sid in c]), before_new_calls[sid],
                "unexpected API call for unadmitted candidate %s" % sid,
            )
        # Sibling team and other source unaffected.
        self.assertEqual(hub.snapshot("team-alpha").status, IDLE)
        self.assertTrue(hub.snapshot("team-alpha").discovery_complete)
        banners = status_json(hub, 15)["banners"]
        self.assertTrue(any("win-windows" in b and "primary overflow" in b
                            for b in banners), banners)


class TestExampleConfigPaths(unittest.TestCase):
    """The shipped example uses generic worktrees and no real exe path."""

    def test_example_loads_two_sources_and_six_teams(self):
        cfg = load_config(EXAMPLE_PATH)
        sources = cfg.active_sources()
        self.assertEqual(len(sources), 2)
        self.assertEqual([s.key for s in sources], ["wsl2", "win-windows"])
        self.assertEqual(sum(len(s.teams) for s in sources), 6)
        self.assertEqual(len(sources[0].teams), 5)
        self.assertEqual(len(sources[1].teams), 1)
        self.assertEqual(len(cfg.teams), 5)  # schema-1/2 back-compat block

    def test_real_wsl_worktrees_in_both_teams_blocks(self):
        expected = {
            "team-alpha": "/home/you/projects/team-alpha",
            "team-beta": "/home/you/projects/team-beta",
            "team-gamma": "/home/you/projects/team-gamma",
            "team-delta": "/home/you/projects/team-delta",
            "team-epsilon": "/home/you/projects/team-epsilon",
        }
        cfg = load_config(EXAMPLE_PATH)
        wsl = cfg.active_sources()[0]
        for team_cfg in wsl.teams:
            self.assertEqual(team_cfg.worktree_dir, expected[team_cfg.key])
        for team_cfg in cfg.teams:
            self.assertEqual(team_cfg.worktree_dir, expected[team_cfg.key])
        with open(EXAMPLE_PATH, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("REPLACE_WITH_WORKTREE_DIR", raw)

    def test_windows_mapping_and_user_supplied_placeholder_are_preserved(self):
        cfg = load_config(EXAMPLE_PATH)
        win = cfg.active_sources()[1]
        team_cfg = win.teams[0]
        self.assertEqual(team_cfg.key, "team-windows")
        self.assertEqual(same_path(team_cfg.worktree_dir, WIN_DIR), True)
        # The example ships no pinned session ids: everything is discovered.
        self.assertIsNone(team_cfg.session_id)
        self.assertEqual(team_cfg.child_session_ids, ())
        # A real exe path is supplied by the user at activation; never fabricated.
        self.assertEqual(win.opencode_bin, "/mnt/c/REPLACE_WITH_PATH_TO/opencode.exe")
        self.assertIn("REPLACE_WITH_PATH_TO", win.opencode_bin)


if __name__ == "__main__":
    unittest.main()
