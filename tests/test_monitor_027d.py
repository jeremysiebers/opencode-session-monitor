"""INC027d offline tests — dynamic discovery, multi-primary cards, schema 2,
retention, fail-closed and per-session Gezien.

Fully offline: the recording FakeCli supplies synthetic session.list pages and
session.get/message/permission envelopes. No network, no real binary, no live
config. Run with::

    cd opencode-session-monitor && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from monitor.api import MalformedApiResponse, MonitorApiError, next_list_cursor
from monitor.collector import (
    GRACE, GRACE_LIMIT, IDLE, MAX_PAGES, MAX_PRIMARIES_PER_TEAM, PRESENT, READY,
    UNKNOWN, WAITING_PERMISSION, aggregate_status, classify_candidate,
    classify_parent, extract_archived, extract_directory, is_denied,
)
from monitor.config import ConfigError, load_config
from monitor.paths import normalize_path, same_path
from monitor.state import LEGACY_SOURCE, STATE_SCHEMA, ReadyPending, StateStore
from monitor.web import render_dashboard, status_json

from fake_cli import FakeCli
from test_monitor import SID, SID_B, assistant_msg, session_info, team

ABSENT = object()


def list_item(sid, directory=ABSENT, parent=ABSENT, archived=ABSENT, title=ABSENT):
    """Synthetic session.list item; absent keys are truly omitted."""
    item = {"id": sid}
    if directory is not ABSENT:
        item["location"] = {"directory": directory}
    if parent is not ABSENT:
        item["parentID"] = parent
    if archived is not ABSENT:
        item["archived"] = archived
    if title is not ABSENT:
        item["title"] = title
    return item


class DBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeCli()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")

    def write_config(self, teams, **overrides):
        config = {
            "schema": overrides.get("schema", 2),
            "opencode_bin": "/fake/opencode",
            "server_url": None,
            "poll_interval_seconds": 3,
            "stale_threshold_seconds": 15,
            "bind": "127.0.0.1",
            "port": 8765,
            "teams": teams,
        }
        for key in ("deny_session_ids", "source_key", "sources"):
            if key in overrides:
                config[key] = overrides[key]
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return load_config(self.config_path)

    def make(self, teams, **overrides):
        from monitor.api import OpencodeApi
        from monitor.collector import Monitor
        cfg = self.write_config(teams, **overrides)
        api = OpencodeApi(cfg.opencode_bin, runner=self.fake)
        store = StateStore(self.state_path, source_key=cfg.source_key)
        ready = ReadyPending()
        return Monitor(cfg, api, store, ready), store, ready, cfg

    def basic_primary(self, sid, directory="/w/team", msgs=None):
        self.fake.set_session(sid, session_info(sid, directory))
        self.fake.set_permissions(sid, [])
        self.fake.set_messages(sid, msgs if msgs is not None else [assistant_msg("m_" + sid)])


# ---------------------------------------------------------------------------
# AC-d1 — pure normalizer + classification/visibility/deny
# ---------------------------------------------------------------------------
class TestPathsPure(DBase):
    def test_posix_and_windows_style_paths_match(self):
        self.assertEqual(normalize_path("/home/you/projects/team-alpha"),
                         normalize_path("/home//you/projects/team-alpha/./"))
        # Windows-style path (backslashes, drive case) matches POSIX-style config
        self.assertTrue(same_path(r"C:\Users\you\dev", "c:/Users/you/dev"))
        self.assertTrue(same_path(r"C:\Users\you\dev", "C:/Users/you/dev/"))
        self.assertFalse(same_path(r"C:\Users\you\dev", r"C:\Users\you\other"))

    def test_dot_dot_and_non_string(self):
        self.assertEqual(normalize_path("/w/a/b/../c"), "/w/a/c")
        self.assertIsNone(normalize_path(""))
        self.assertIsNone(normalize_path(None))
        self.assertIsNone(normalize_path(123))

    def test_config_accepts_windows_style_worktree(self):
        cfg = self.write_config([team(directory=r"C:\Users\you\dev")], schema=2)
        self.assertEqual(cfg.teams[0].worktree_dir, "c:/Users/you/dev")
        with self.assertRaises(ConfigError):
            self.write_config([team(directory="relative/path")], schema=2)

    def test_normalizer_has_no_filesystem_access(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "monitor", "paths.py"), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("os.path", "realpath", "abspath", "stat(", "open(", "exists("):
            self.assertNotIn(forbidden, source)


class TestClassification(DBase):
    def test_parent_shapes(self):
        self.assertEqual(classify_parent({"id": "x"}), "primary")            # ABSENT
        self.assertEqual(classify_parent({"id": "x", "parentID": "ses_p"}), "child")
        self.assertEqual(classify_parent({"id": "x", "parentID": None}), "unknown")
        self.assertEqual(classify_parent({"id": "x", "parentID": 123}), "unknown")
        self.assertEqual(classify_parent({"id": "x", "parentID": ""}), "unknown")

    def test_archived_tristate(self):
        self.assertFalse(extract_archived({"id": "x"}))
        self.assertTrue(extract_archived({"id": "x", "archived": True}))
        self.assertIsNone(extract_archived({"id": "x", "archived": "yes"}))

    def test_deny_is_exact_id_only_never_name_or_title(self):
        deny = ("ses_exampleprimary000000001",)
        self.assertTrue(is_denied("ses_exampleprimary000000001", deny))
        self.assertFalse(is_denied("ses_exampleprimary00000000", deny))   # substring
        self.assertFalse(is_denied("OpenCode Session Monitor", deny))        # title
        self.assertFalse(is_denied("project-lead", deny))                    # agent name
        # PD4: the monitor-dev id is NOT hardcoded — it is only denied via config.
        item = list_item("ses_exampleprimary000000001", "/w/team")
        self.assertEqual(classify_candidate(item, "/w/team", ())[0], "primary")
        self.assertEqual(classify_candidate(item, "/w/team", deny)[0], "skip")

    def test_never_infer_primary_from_agent_name_or_title(self):
        # A child with a "primary-looking" agent/title must stay a child.
        item = list_item("ses_x", "/w/team", parent="ses_parent")
        item["agent"] = "project-lead"
        item["title"] = "OpenCode Session Monitor"
        self.assertEqual(classify_candidate(item, "/w/team", ())[0], "child")
        # A parentless item in another worktree must not be admitted by name.
        other = list_item("ses_y", "/w/other")
        other["agent"] = "project-lead"
        self.assertEqual(classify_candidate(other, "/w/team", ())[0], "skip")

    def test_classify_candidate_paths(self):
        team_dir = "/w/team"
        self.assertEqual(classify_candidate(list_item("ses_a", directory=team_dir),
                                            team_dir, ())[0], "primary")
        self.assertEqual(classify_candidate(list_item("ses_a", directory="/w/other"),
                                            team_dir, ())[0], "skip")
        self.assertEqual(classify_candidate(list_item("ses_a", directory=team_dir,
                                                      parent="ses_p"), team_dir, ())[0], "child")
        self.assertEqual(classify_candidate(list_item("ses_a", directory=team_dir,
                                                      parent=None), team_dir, ())[0], "unknown")
        # missing directory -> needs hydration; hydrated info supplies it
        needs = list_item("ses_a")
        self.assertEqual(classify_candidate(needs, team_dir, ())[0], "needs_hydrate")
        hydrated = {"ses_a": {"location": {"directory": team_dir}}}
        self.assertEqual(classify_candidate(needs, team_dir, (), hydrated)[0], "primary")
        # archived is excluded, ambiguous archive -> unknown
        self.assertEqual(classify_candidate(list_item("ses_a", directory=team_dir,
                                                      archived=True), team_dir, ())[0], "skip")
        self.assertEqual(classify_candidate(list_item("ses_a", directory=team_dir,
                                                      archived="?"), team_dir, ())[0], "unknown")


# ---------------------------------------------------------------------------
# AC-d1 — full cursor paging, cursor.next, cycle/terminal/dedup
# ---------------------------------------------------------------------------
class TestDiscoveryPaging(DBase):
    def test_next_list_cursor_shapes(self):
        self.assertEqual(next_list_cursor({"cursor": "c1"}), "c1")
        self.assertEqual(next_list_cursor({"cursor": {"next": "c2"}}), "c2")
        self.assertEqual(next_list_cursor({"next": "c3"}), "c3")
        self.assertIsNone(next_list_cursor({"cursor": None}))
        self.assertIsNone(next_list_cursor({"cursor": {"done": True}}))

    def test_full_cursor_paging_terminal_and_dedup(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        p1 = {"data": [list_item("ses_a", "/w/team"), list_item("ses_dup", "/w/team")],
              "cursor": "cur-1"}
        p2 = {"data": [list_item("ses_b", "/w/team"), list_item("ses_dup", "/w/team")],
              "cursor": "cur-2"}
        p3 = {"data": [list_item("ses_c", "/w/other")]}  # terminal
        self.fake.set_session_pages([p1, p2, p3])
        for sid in ("ses_a", "ses_b"):
            self.basic_primary(sid)
        self.fake.set_active([])
        mon.poll_once()
        result = mon._walk_sessions()
        self.assertTrue(result.complete)
        self.assertEqual(result.pages, 3)
        self.assertIn("ses_dup", result.duplicates)          # duplicate detected
        self.assertEqual(len({i["id"] for i in result.items}), 4)  # deduped: a,dup,b,c
        snap = mon.snapshot("t1")
        # 3 unique primaries; the duplicated id yields exactly ONE card, not two.
        self.assertEqual(set(snap.session_ids), {"ses_a", "ses_b", "ses_dup"})
        self.assertEqual(len(snap.session_ids), len(set(snap.session_ids)))

    def test_cursor_next_envelope_is_honored(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        pages = [
            {"data": [list_item("ses_a", "/w/team")], "cursor": {"next": "cur-A"}},
            {"data": [list_item("ses_b", "/w/team")]},
        ]
        self.fake.set_session_pages(pages)
        for sid in ("ses_a", "ses_b"):
            self.basic_primary(sid)
        self.fake.set_active([])
        mon.poll_once()
        self.assertEqual(set(mon.snapshot("t1").session_ids), {"ses_a", "ses_b"})

    def test_cycle_is_incomplete_fail_closed(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([
            {"data": [list_item("ses_a", "/w/team")], "cursor": "loop"},
            {"data": [list_item("ses_b", "/w/team")], "cursor": "loop"},
        ])
        self.basic_primary("ses_a")
        self.basic_primary("ses_b")
        self.fake.set_active([])
        mon.poll_once()
        result = mon._walk_sessions()
        self.assertFalse(result.complete)
        self.assertIn("cycle", result.reason)
        self.assertFalse(mon.snapshot("t1").discovery_complete)

    def test_page_cap_is_incomplete(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([
            {"data": [], "cursor": "c%d" % i} for i in range(MAX_PAGES + 2)
        ])
        self.fake.set_active([])
        result = mon._walk_sessions()
        self.assertFalse(result.complete)
        self.assertIn("cap", result.reason)

    def test_item_metadata_avoids_per_session_get_for_children(self):
        """Children and other-worktree items need no session.get at all."""
        mon, _, _, _ = self.make([team(directory="/w/team")])
        children = [list_item("ses_child_%d" % i, "/w/team", parent="ses_a") for i in range(20)]
        others = [list_item("ses_other_%d" % i, "/w/other") for i in range(50)]
        self.fake.set_session_pages([
            {"data": children + others + [list_item("ses_a", "/w/team")]}
        ])
        self.basic_primary("ses_a")
        self.fake.set_active([])
        mon.poll_once()
        get_calls = [c for c in self.fake.calls if "session.get" in c]
        # exactly one get for the single primary (status), none for 70 non-primaries
        self.assertEqual(len(get_calls), 1)


# ---------------------------------------------------------------------------
# AC-d2 — multiple stacked primary cards, no collapse, children advisory
# ---------------------------------------------------------------------------
class TestMultiPrimaryCards(DBase):
    def test_multiple_primaries_are_separate_cards(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [
            list_item("ses_a", "/w/team"), list_item("ses_b", "/w/team"),
        ]}])
        self.basic_primary("ses_a")
        self.basic_primary("ses_b")
        self.fake.set_active([])
        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertEqual(len(snap.sessions), 2)
        self.assertEqual({s.session_id for s in snap.sessions}, {"ses_a", "ses_b"})
        for s in snap.sessions:
            self.assertEqual(s.status, IDLE)
            self.assertEqual(s.team_key, "t1")

    def test_sibling_unknown_does_not_disable_other_card(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [
            list_item("ses_a", "/w/team"), list_item("ses_b", "/w/team"),
        ]}])
        self.basic_primary("ses_a")
        self.basic_primary("ses_b")
        self.fake.error(("session.message.list", "ses_b"), OSError("one session down"))
        self.fake.set_active([])
        mon.poll_once()
        by_id = {s.session_id: s for s in mon.snapshot("t1").sessions}
        self.assertEqual(by_id["ses_a"].status, IDLE)
        self.assertEqual(by_id["ses_b"].status, UNKNOWN)
        self.assertNotEqual(aggregate_status(mon.snapshot("t1").sessions), UNKNOWN)

    def test_children_are_advisory_no_cards_and_no_child_get(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [
            list_item("ses_a", "/w/team"),
            list_item("ses_child", "/w/team", parent="ses_a"),
        ]}])
        self.basic_primary("ses_a")
        self.fake.set_active([])
        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertEqual([s.session_id for s in snap.sessions], ["ses_a"])
        child = snap.sessions[0]
        self.assertEqual(child.child_ids, ("ses_child",))
        self.assertFalse(child.child_busy)
        get_calls = [c for c in self.fake.calls if "session.get" in c]
        self.assertEqual(len(get_calls), 1)  # no get/message/permission for child

    def test_pinned_and_discovered_coexist(self):
        mon, _, _, _ = self.make([team(directory="/w/team", sid="ses_pinned")])
        self.fake.set_session_pages([{"data": [list_item("ses_new", "/w/team")]}])
        self.basic_primary("ses_pinned")
        self.basic_primary("ses_new")
        self.fake.set_active([])
        mon.poll_once()
        self.assertEqual(set(mon.snapshot("t1").session_ids), {"ses_pinned", "ses_new"})


# ---------------------------------------------------------------------------
# AC-d5 — fail-closed INCOMPLETE, overflow, other teams unaffected
# ---------------------------------------------------------------------------
class TestFailClosed(DBase):
    def test_incomplete_keeps_cards_unknown_and_preserves_pending(self):
        mon, store, ready, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.basic_primary("ses_a")
        self.fake.set_active([])
        mon.poll_once()  # baseline -> IDLE
        self.fake.set_messages("ses_a", [assistant_msg("m2"), assistant_msg("m_ses_a")])
        mon.poll_once()  # READY, pending set
        self.assertEqual(mon.snapshot("t1").sessions[0].status, READY)
        self.assertTrue(ready.has("ses_a", "default"))

        self.fake.error("session.list", OSError("discovery lost"))
        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertFalse(snap.discovery_complete)
        self.assertEqual(snap.sessions[0].status, UNKNOWN)
        self.assertTrue(snap.sessions[0].incomplete)
        self.assertTrue(ready.has("ses_a", "default"))       # pending preserved
        self.assertEqual(store.last_seen("ses_a", "default"), "m2")  # baseline intact
        payload = status_json(mon, 15)
        self.assertTrue(any("incomplete" in b.lower() for b in payload["banners"]))
        page = render_dashboard(mon, 15, 5)
        self.assertIn("Discovery incomplete", page)

    def test_incomplete_cycle_does_not_admit_brand_new_primary(self):
        """M5 regression: a NEW primary first seen during an INCOMPLETE walk
        (cycle) must NOT be admitted, while the existing card stays visible as
        UNKNOWN and its ready_pending survives.

        Guards collector.py `live_ids = existing | set(pinned)` (never
        `| set(candidates)`). Under the M5 mutant a brand-new candidate present
        in the incomplete walk would be admitted as an extra UNKNOWN card, so
        this test fails there and passes on the current source.
        """
        mon, store, ready, _ = self.make([team(directory="/w/team")])
        # Establish an existing card with a READY pending via COMPLETE walks.
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.basic_primary("ses_a", msgs=[assistant_msg("m1")])
        self.fake.set_active([])
        mon.poll_once()  # baseline -> IDLE
        self.basic_primary("ses_a", msgs=[assistant_msg("m2"), assistant_msg("m1")])
        mon.poll_once()  # READY, pending set
        self.assertEqual(mon.snapshot("t1").sessions[0].status, READY)
        self.assertTrue(ready.has("ses_a", "default"))

        # INCOMPLETE walk via a repeated cursor. The FIRST page presents the
        # existing id AND a brand-new valid primary id (ses_new); the repeated
        # "loop" cursor on the next page triggers cycle detection.
        self.fake.set_session_pages([
            {"data": [list_item("ses_a", "/w/team"), list_item("ses_new", "/w/team")],
             "cursor": "loop"},
            {"data": [], "cursor": "loop"},
        ])
        mon.poll_once()

        snap = mon.snapshot("t1")
        self.assertFalse(snap.discovery_complete)
        # ses_new was NEVER seen on a COMPLETE walk -> must not be admitted.
        self.assertEqual(snap.session_ids, ("ses_a",))
        self.assertEqual(len(snap.sessions), 1)
        card = snap.sessions[0]
        self.assertEqual(card.session_id, "ses_a")
        self.assertEqual(card.status, UNKNOWN)   # existing card kept visible as UNKNOWN
        self.assertTrue(card.incomplete)
        self.assertTrue(ready.has("ses_a", "default"))             # pending preserved
        self.assertEqual(store.last_seen("ses_a", "default"), "m2")  # baseline intact

    def test_incomplete_does_not_admit_new_candidates(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.basic_primary("ses_a")
        self.fake.set_active([])
        mon.poll_once()
        # discovery now incomplete and reports a brand-new primary
        self.fake.errors.clear()
        self.fake.error("session.list", OSError("lost"))
        mon.poll_once()
        self.assertEqual(set(mon.snapshot("t1").session_ids), {"ses_a"})  # ses_new NOT admitted

    def test_overflow_marks_only_that_team_unknown(self):
        teams = [team(directory="/w/big"), team("t2", "Team Two", "/w/small")]
        mon, _, _, _ = self.make(teams)
        big = [list_item("ses_big_%02d" % i, "/w/big") for i in range(MAX_PRIMARIES_PER_TEAM + 1)]
        self.fake.set_session_pages([{"data": big + [list_item("ses_small", "/w/small")]}])
        for i in range(MAX_PRIMARIES_PER_TEAM + 1):
            self.basic_primary("ses_big_%02d" % i)
        self.basic_primary("ses_small")
        self.fake.set_active([])
        mon.poll_once()
        self.assertTrue(mon.snapshot("t1").overflow)
        self.assertEqual(mon.snapshot("t1").status, UNKNOWN)
        self.assertFalse(mon.snapshot("t2").overflow)
        self.assertEqual(mon.snapshot("t2").status, IDLE)  # other team unaffected

    def test_denied_id_never_becomes_a_card(self):
        mon, _, _, _ = self.make(
            [team(directory="/w/team")],
            deny_session_ids=["ses_exampleprimary000000001"],
        )
        self.fake.set_session_pages([{"data": [
            list_item("ses_exampleprimary000000001", "/w/team"),
            list_item("ses_ok", "/w/team"),
        ]}])
        self.basic_primary("ses_ok")
        self.fake.set_active([])
        mon.poll_once()
        self.assertEqual(set(mon.snapshot("t1").session_ids), {"ses_ok"})


# ---------------------------------------------------------------------------
# AC-d4 — schema migration, retention, namespaced dedup
# ---------------------------------------------------------------------------
class TestSchemaMigration(DBase):
    def test_config_schema2_and_backcompat1(self):
        cfg2 = self.write_config([team(sid=SID)], schema=2,
                                 deny_session_ids=["ses_deny"], source_key="wsl2")
        self.assertEqual(cfg2.schema, 2)
        self.assertEqual(cfg2.deny_session_ids, ("ses_deny",))
        self.assertEqual(cfg2.source_key, "wsl2")
        cfg1 = self.write_config([team(sid=SID)], schema=1)
        self.assertEqual(cfg1.schema, 1)
        self.assertEqual(cfg1.deny_session_ids, ())
        self.assertEqual(cfg1.source_key, "default")
        with self.assertRaises(ConfigError):
            self.write_config([team(sid=SID)], schema=3)

    def test_state_v1_flat_map_migrates_to_nested(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "last_seen": {"ses_a": "m1", "ses_b": "m2"}}, handle)
        store = StateStore(self.state_path, source_key="wsl2")
        self.assertEqual(store.last_seen("ses_a", LEGACY_SOURCE), "m1")
        self.assertEqual(store.last_seen("ses_b", LEGACY_SOURCE), "m2")
        store.record("ses_c", "m3", "wsl2")
        with open(self.state_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["schema"], STATE_SCHEMA)
        self.assertIn("wsl2", data["last_seen"])
        self.assertIn(LEGACY_SOURCE, data["last_seen"])

    def test_state_v2_namespaced_no_cross_session_or_source(self):
        store = StateStore(self.state_path, source_key="wsl2")
        store.record("ses_a", "m1", "wsl2")
        store.record("ses_a", "m9", "other")
        self.assertEqual(store.last_seen("ses_a", "wsl2"), "m1")
        self.assertEqual(store.last_seen("ses_a", "other"), "m9")
        self.assertIsNone(store.last_seen("ses_b", "wsl2"))


class TestRetention(DBase):
    def test_present_grace_dropped_keeps_baseline(self):
        mon, store, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.basic_primary("ses_a")
        self.fake.set_active([])
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").sessions[0].membership, PRESENT)

        self.fake.set_session_pages([{"data": []}])  # gone on a COMPLETE walk
        for _ in range(GRACE_LIMIT):
            mon.poll_once()
        self.assertEqual(mon.snapshot("t1").sessions[0].membership, GRACE)
        mon.poll_once()  # one more absence -> DROPPED
        self.assertEqual(mon.snapshot("t1").session_ids, ())
        self.assertIsNotNone(store.last_seen("ses_a", "default"))  # baseline retained

    def test_returning_session_does_not_reready(self):
        mon, store, ready, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.basic_primary("ses_a", msgs=[assistant_msg("m1")])
        self.fake.set_active([])
        mon.poll_once()  # baseline m1
        self.fake.set_session_pages([{"data": []}])
        mon.poll_once()  # GRACE
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.fake.set_messages("ses_a", [assistant_msg("m1")])
        mon.poll_once()  # returns with SAME id -> no re-READY
        snap = mon.snapshot("t1")
        self.assertEqual(snap.sessions[0].membership, PRESENT)
        self.assertEqual(snap.sessions[0].status, IDLE)
        self.assertFalse(ready.has("ses_a", "default"))

    def test_restart_no_duplicate_ready_namespaced(self):
        from monitor.api import OpencodeApi
        from monitor.collector import Monitor
        mon, _, _, cfg = self.make([team(directory="/w/team")], source_key="wsl2")
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")]}])
        self.basic_primary("ses_a", msgs=[assistant_msg("m1")])
        self.fake.set_active([])
        mon.poll_once()
        self.basic_primary("ses_a", msgs=[assistant_msg("m2"), assistant_msg("m1")])
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").sessions[0].status, READY)

        restarted = StateStore(self.state_path, source_key="wsl2")
        ready2 = ReadyPending()
        mon2 = Monitor(cfg, OpencodeApi(cfg.opencode_bin, runner=self.fake), restarted, ready2)
        mon2.poll_once()
        self.assertEqual(mon2.snapshot("t1").sessions[0].status, IDLE)
        self.assertFalse(ready2.has("ses_a", "wsl2"))


# ---------------------------------------------------------------------------
# AC-d2/d5 — per-session Gezien and one-view consistency
# ---------------------------------------------------------------------------
class TestAckAndWeb(DBase):
    def _two_ready(self):
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.fake.set_session_pages([{"data": [
            list_item("ses_a", "/w/team"), list_item("ses_b", "/w/team"),
        ]}])
        self.basic_primary("ses_a", msgs=[assistant_msg("m1")])
        self.basic_primary("ses_b", msgs=[assistant_msg("n1")])
        self.fake.set_active([])
        mon.poll_once()  # baselines
        self.basic_primary("ses_a", msgs=[assistant_msg("m2"), assistant_msg("m1")])
        self.basic_primary("ses_b", msgs=[assistant_msg("n2"), assistant_msg("n1")])
        mon.poll_once()  # both READY
        return mon

    def test_acknowledge_session_leaves_sibling_untouched(self):
        from monitor.web import handle_ack
        mon = self._two_ready()
        by_id = {s.session_id: s for s in mon.snapshot("t1").sessions}
        self.assertEqual(by_id["ses_a"].status, READY)
        self.assertEqual(by_id["ses_b"].status, READY)
        before = list(self.fake.calls)
        self.assertTrue(handle_ack(mon, session_id="ses_a", source_key="default"))
        by_id = {s.session_id: s for s in mon.snapshot("t1").sessions}
        self.assertEqual(by_id["ses_a"].status, IDLE)
        self.assertEqual(by_id["ses_b"].status, READY)     # sibling untouched
        self.assertEqual(self.fake.calls, before)          # no OpenCode call

    def test_collect_view_is_read_once_by_cards_and_json(self):
        mon = self._two_ready()
        original = mon.collect_view
        calls = []

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        mon.collect_view = counted
        render_dashboard(mon, 15, 5)
        self.assertEqual(len(calls), 1)
        calls.clear()
        status_json(mon, 15)
        self.assertEqual(len(calls), 1)

    def test_awaiting_count_matches_view_sessions(self):
        mon = self._two_ready()
        view = mon.collect_view(15)
        awaiting = [s for s in view.sessions if s.status in (READY, WAITING_PERMISSION)]
        self.assertEqual(view.awaiting_count, len(awaiting))
        self.assertEqual(status_json(mon, 15)["awaiting_count"], view.awaiting_count)
        self.assertEqual(len(status_json(mon, 15)["sessions"]), 2)


# ---------------------------------------------------------------------------
# AC-d1/d3 — invariants preserved (GET-only, argv, frozen argv, caps)
# ---------------------------------------------------------------------------
class TestInvariants(DBase):
    def test_paged_session_list_stays_get_only_with_frozen_argv(self):
        api_calls = []
        self.fake.set_session_pages([{"data": [list_item("ses_a", "/w/team")], "cursor": "c1"}])
        mon, _, _, _ = self.make([team(directory="/w/team")])
        self.basic_primary("ses_a")
        self.fake.set_active([])
        mon.poll_once()
        list_calls = [c for c in self.fake.calls if "session.list" in c]
        self.assertTrue(list_calls)
        for call in self.fake.calls:
            self.assertEqual(call[0], "/fake/opencode")
            self.assertEqual(call[1], "api")
            self.assertNotIn("-d", call)
            self.assertNotIn("POST", call)
            self.assertFalse(any(t.startswith("/api/") for t in call))
        self.assertTrue(any("limit=%d" % 50 in c for c in list_calls))
        self.assertTrue(any("cursor=c1" in c for c in list_calls))

    def test_caps_measured_from_e3(self):
        self.assertGreaterEqual(MAX_PAGES, 2 * 18)     # ~18 pages observed, headroom
        self.assertGreaterEqual(MAX_PRIMARIES_PER_TEAM, 9)  # Team Alpha 9 primaries

    def test_poll_and_stale_defaults_unchanged(self):
        cfg = self.write_config([team(sid=SID)], schema=1)
        self.assertEqual(cfg.poll_interval, 3.0)
        self.assertEqual(cfg.stale_threshold, 15.0)


if __name__ == "__main__":
    unittest.main()
