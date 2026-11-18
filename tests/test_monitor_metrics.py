"""INC027e METRICS offline tests — additive per-cycle instrumentation only.

Fully offline: the same recording fake CLI used by the rest of the suite, an
injected monotonic clock and temporary state/JSONL paths. No network, no real
binary, no network service, no live port, no host-specific path.

Run with::

    cd opencode-session-monitor && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor.api import ALLOWED_OPERATIONS, MonitorApiError, OpencodeApi  # noqa: E402
from monitor.collector import IDLE, UNKNOWN, Monitor  # noqa: E402
from monitor.config import Config, SourceConfig, TeamConfig  # noqa: E402
from monitor.metrics import (  # noqa: E402
    LATENCY_SAMPLE_CAP, MAX_JSONL_BYTES, JsonlMetricsWriter, MetricsRecorder,
    NullMetricsRecorder, OPERATIONS,
)
from monitor.state import ReadyPending, StateStore  # noqa: E402
from monitor.web import status_json  # noqa: E402

from fake_cli import FakeCli, WindowsFakeCli  # noqa: E402
from test_monitor import (  # noqa: E402
    SID, LimitAwareCli, assistant_msg, big_history, session_info,
    truncated_json_stdout,
)

WIN_SID = "ses_win_primary"
WIN_DIR = "C:\\Users\\you\\projects\\team-windows"
WIN_BIN = "/mnt/c/Users/you/tools/opencode.exe"


class FakeClock:
    """Deterministic monotonic clock: only tests advance it."""

    def __init__(self, start=1000.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class BrokenRecorder:
    """Every hook raises: proves instrumentation can never break a poll."""

    source_key = "broken"

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise RuntimeError("metrics fault")
        return boom


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")

    def config(self, teams=None, poll=3.0, stale=15.0, sources=(), source_key="wsl2"):
        teams = tuple(teams or (
            TeamConfig(key="t1", label="Team One", worktree_dir="/w/t1", session_id=SID),
        ))
        return Config(
            opencode_bin="/fake/opencode",
            server_url=None,
            poll_interval=poll,
            stale_threshold=stale,
            bind="127.0.0.1",
            port=8765,
            teams=teams,
            source_key=source_key,
            sources=tuple(sources),
        )

    def monitor(self, fake=None, metrics=None, teams=None, state_path=None,
                poll=3.0, stale=15.0):
        fake = fake if fake is not None else FakeCli()
        api = OpencodeApi("/fake/opencode", runner=fake, metrics=metrics)
        store = StateStore(state_path or self.state_path)
        mon = Monitor(
            self.config(teams, poll, stale), api, store, ReadyPending(),
            metrics=metrics,
        )
        return mon, fake, store

    @staticmethod
    def seed_one(fake, sid=SID, directory="/w/t1", outcome="succeeded",
                 messages=None, active=()):
        fake.set_active(list(active))
        fake.set_session_pages([{"data": []}])
        fake.set_session(sid, session_info(sid, directory, outcome))
        fake.set_permissions(sid, [])
        fake.set_messages(sid, messages if messages is not None else [assistant_msg("m1")])


# ---------------------------------------------------------------------------
# Calls / latency / errors
# ---------------------------------------------------------------------------
class TestCallInstrumentation(Base):
    def test_call_counts_equal_recorded_argv_and_frozen_six_ops(self):
        rec = MetricsRecorder(source_key="wsl2")
        mon, fake, _store = self.monitor(metrics=rec)
        self.seed_one(fake)
        mon.poll_once()
        snap = rec.snapshot()
        self.assertIsNotNone(snap)

        recorded_ops = []
        for argv in fake.calls:
            ops = [token for token in argv if token in ALLOWED_OPERATIONS]
            self.assertEqual(len(ops), 1, argv)  # exactly one frozen GET op
            recorded_ops.append(ops[0])

        calls = snap["calls"]
        self.assertEqual(calls["total"], len(fake.calls))
        self.assertEqual(sum(calls["by_operation"].values()), calls["total"])
        for op in OPERATIONS:
            self.assertEqual(calls["by_operation"][op], recorded_ops.count(op), op)
        self.assertEqual(len(ALLOWED_OPERATIONS), 6)
        self.assertEqual(tuple(sorted(OPERATIONS)), tuple(sorted(ALLOWED_OPERATIONS)))
        self.assertEqual(snap["cycle"]["state"], "OK")
        self.assertEqual(snap["cycle"]["id"], 1)

    def test_errors_are_classified_timeout_rc_malformed(self):
        # timeout
        fake = FakeCli()
        fake.error("session.active", subprocess.TimeoutExpired(cmd="x", timeout=1))
        rec = MetricsRecorder()
        mon, _fake, _store = self.monitor(fake=fake, metrics=rec)
        mon.poll_once()
        self.assertEqual(rec.snapshot()["calls"]["errors_by_class"]["timeout"], 1)

        # rc != 0 (native-Windows fake path, no shell)
        win = WindowsFakeCli()
        win.set_rc("session.active", 2)
        rec_rc = MetricsRecorder()
        api = OpencodeApi(WIN_BIN, runner=win, metrics=rec_rc)
        mon_rc = Monitor(
            self.config(teams=(TeamConfig("t1", "Team One", WIN_DIR, session_id=WIN_SID),)),
            api, StateStore(os.path.join(self.tmp.name, "rc.json")), ReadyPending(),
            metrics=rec_rc,
        )
        mon_rc.poll_once()
        snap_rc = rec_rc.snapshot()
        self.assertEqual(snap_rc["calls"]["errors_by_class"]["rc_nonzero"], 1)
        self.assertEqual(sum(snap_rc["calls"]["errors_by_class"].values()), 1)

        # malformed JSON (rc 0, incomplete document)
        bad = FakeCli()
        bad.set("session.active", "{this is not json")
        rec_bad = MetricsRecorder()
        mon_bad, _fake_bad, _store_bad = self.monitor(fake=bad, metrics=rec_bad)
        mon_bad.poll_once()
        snap_bad = rec_bad.snapshot()
        self.assertEqual(snap_bad["calls"]["errors_by_class"]["malformed_json"], 1)

    def test_user_facing_errors_still_fail_closed(self):
        fake = FakeCli()
        fake.error("session.active", subprocess.TimeoutExpired(cmd="x", timeout=1))
        rec = MetricsRecorder()
        mon, _fake, _store = self.monitor(fake=fake, metrics=rec)
        mon.poll_once()
        team = mon.snapshot("t1")
        self.assertEqual(team.status, UNKNOWN)
        self.assertIn("session.active unavailable", team.reason)
        self.assertEqual(rec.snapshot()["cycle"]["state"], "OK")

    def test_refused_guard_attempts_are_counted_without_execution(self):
        fake = FakeCli()
        rec = MetricsRecorder()
        api = OpencodeApi("/fake/opencode", runner=fake, metrics=rec)
        rec.begin_cycle()
        with self.assertRaises(MonitorApiError):
            api._call("session.get", {"bad": "x"})
        with self.assertRaises(MonitorApiError):
            api._call("session.get", [])  # params refused
        snap = rec.end_cycle()
        self.assertEqual(snap["calls"]["total"], 2)
        self.assertEqual(snap["calls"]["by_operation"]["session.get"], 2)
        self.assertEqual(snap["calls"]["errors_by_class"]["refused"], 2)
        self.assertEqual(fake.calls, [])  # no argv was ever executed

    def test_latency_percentiles_nearest_rank_and_truncation(self):
        rec = MetricsRecorder()
        rec.begin_cycle()
        for value in range(1, 11):
            rec.record_call("session.get", float(value))
        snap = rec.end_cycle()

        stats = snap["latency_ms"]["session.get"]
        self.assertEqual(stats["count"], 10)
        self.assertEqual(stats["p50"], 5.0)   # ceil(0.50*10) = 5th
        self.assertEqual(stats["p95"], 10.0)  # ceil(0.95*10) = 10th
        self.assertEqual(stats["max"], 10.0)
        self.assertEqual(stats["samples"], 10)
        self.assertFalse(stats["truncated"])

        rec.begin_cycle()
        for value in range(1, 601):
            rec.record_call("session.get", float(value))
        snap = rec.end_cycle()
        stats = snap["latency_ms"]["session.get"]
        self.assertEqual(stats["count"], 600)
        self.assertEqual(stats["samples"], LATENCY_SAMPLE_CAP)
        self.assertTrue(stats["truncated"])
        self.assertEqual(stats["max"], 600.0)
        self.assertEqual(stats["p50"], 256.0)   # nearest-rank over retained 1..512
        self.assertEqual(stats["p95"], 487.0)

    def test_per_cycle_reset_counts_do_not_accumulate(self):
        rec = MetricsRecorder()
        mon, fake, _store = self.monitor(metrics=rec)
        self.seed_one(fake, messages=[assistant_msg("m1")])
        mon.poll_once()
        first = rec.snapshot()
        mon.poll_once()
        second = rec.snapshot()

        self.assertEqual(first["cycle"]["id"], 1)
        self.assertEqual(second["cycle"]["id"], 2)
        self.assertEqual(first["calls"]["total"], second["calls"]["total"])
        self.assertEqual(second["latency_ms"]["session.get"]["count"], 1)
        self.assertEqual(second["latency_ms"]["session.get"]["samples"], 1)
        self.assertEqual(second["sessions"]["polled"], 1)

    def test_hydration_and_message_fallback_are_counted(self):
        fake = FakeCli()
        ids = ["ses_h1", "ses_h2"]
        fake.set_active([])
        fake.set_session_pages([{"data": [{"id": sid} for sid in ids]}])  # no directory
        for sid in ids:
            fake.set_session(sid, session_info(sid, "/w/t1"))
            fake.set_permissions(sid, [])
            fake.set_messages(sid, [])
        rec = MetricsRecorder()
        teams = (TeamConfig(key="t1", label="Team One", worktree_dir="/w/t1"),)
        mon, _fake, _store = self.monitor(fake=fake, metrics=rec, teams=teams)
        mon.poll_once()
        snap = rec.snapshot()
        self.assertEqual(snap["calls"]["hydration_gets"], 2)
        self.assertEqual(snap["calls"]["hydration_failures"], 0)
        self.assertEqual(snap["calls"]["by_operation"]["session.get"], 2)
        self.assertEqual(snap["sessions"]["primaries_seen"], 2)

        # Malformed limit=5 page -> exactly one bounded fallback per session.
        limited = LimitAwareCli()
        limited.set_active([])
        limited.set_session_pages([{"data": []}])
        limited.set_session(SID, session_info(SID, "/w/t1"))
        limited.set_permissions(SID, [])
        truncated = truncated_json_stdout(big_history(), cut_char=257600)
        limited.set_limit(SID, 5, truncated)
        limited.set_limit(SID, 1, {"data": [assistant_msg("fallback_m")]})
        rec_fb = MetricsRecorder()
        mon_fb, _fake_fb, _store_fb = self.monitor(fake=limited, metrics=rec_fb)
        mon_fb.poll_once()
        snap_fb = rec_fb.snapshot()
        self.assertEqual(snap_fb["calls"]["message_list_fallbacks"], 1)
        self.assertEqual(snap_fb["calls"]["errors_by_class"]["malformed_json"], 1)
        self.assertEqual(snap_fb["calls"]["by_operation"]["session.message.list"], 2)


# ---------------------------------------------------------------------------
# Outcome buckets / sessions / discovery
# ---------------------------------------------------------------------------
class TestOutcomeInstrumentation(Base):
    def test_outcome_buckets_split_missing_null_unrecognized_known(self):
        missing = "ses_missing"
        null_sid = "ses_null"
        weird = "ses_weird"
        ok = "ses_ok"
        failed = "ses_failed"
        interrupted = "ses_interrupted"
        ids = [missing, null_sid, weird, ok, failed, interrupted]

        fake = FakeCli()
        fake.set_active([missing])  # missing outcome while BUSY
        fake.set_session_pages([{"data": [
            {"id": sid, "location": {"directory": "/w/t1"}} for sid in ids
        ]}])
        infos = {
            missing: {"id": missing, "time": {"updated": 1000},
                      "location": {"directory": "/w/t1"}},          # key absent
            null_sid: {"id": null_sid, "outcome": None, "time": {"updated": 1000},
                       "location": {"directory": "/w/t1"}},          # explicit null
            weird: {"id": weird, "outcome": "surprise-literal", "time": {"updated": 1000},
                    "location": {"directory": "/w/t1"}},
            ok: {"id": ok, "outcome": "succeeded", "time": {"updated": 1000},
                 "location": {"directory": "/w/t1"}},
            failed: {"id": failed, "outcome": "failed", "time": {"updated": 1000},
                     "location": {"directory": "/w/t1"}},
            interrupted: {"id": interrupted, "outcome": "interrupted",
                          "time": {"updated": 1000},
                          "location": {"directory": "/w/t1"}},
        }
        for sid in ids:
            fake.set_session(sid, infos[sid])
            fake.set_permissions(sid, [])
            fake.set_messages(sid, [])

        rec = MetricsRecorder()
        teams = (TeamConfig(key="t1", label="Team One", worktree_dir="/w/t1"),)
        mon, _fake, _store = self.monitor(fake=fake, metrics=rec, teams=teams)
        mon.poll_once()
        snap = rec.snapshot()

        outcomes = snap["outcomes"]
        self.assertEqual(outcomes["missing_key"], 1)
        self.assertEqual(outcomes["null"], 1)
        self.assertEqual(outcomes["unrecognized_str"], 1)
        self.assertEqual(outcomes["other_type"], 0)
        self.assertEqual(outcomes["succeeded"], 1)
        self.assertEqual(outcomes["failed"], 1)
        self.assertEqual(outcomes["interrupted"], 1)
        self.assertEqual(outcomes["missing_while_busy"], 1)
        self.assertEqual(outcomes["missing_while_idle"], 1)
        self.assertEqual(outcomes["unrecognized_sample"], "surprise-literal")

        self.assertEqual(snap["sessions"]["status_counts"]["UNKNOWN"], 3)
        self.assertEqual(snap["sessions"]["status_counts"]["ERROR"], 2)
        self.assertEqual(snap["sessions"]["status_counts"]["IDLE"], 1)
        self.assertEqual(snap["sessions"]["unknown_reason_classes"]["outcome_missing"], 3)
        self.assertEqual(snap["sessions"]["primaries_seen"], 6)
        self.assertEqual(snap["sessions"]["polled"], 6)
        self.assertEqual(snap["discovery"]["complete"], True)
        self.assertEqual(snap["discovery"]["primaries"], 6)
        self.assertFalse(snap["discovery"]["overflow"])

        # Derived status/reason are untouched by the metrics branch.
        statuses = {s.session_id: s.status for s in mon.snapshot("t1").sessions}
        self.assertEqual(statuses[missing], UNKNOWN)
        self.assertEqual(statuses[null_sid], UNKNOWN)
        self.assertEqual(statuses[weird], UNKNOWN)
        self.assertEqual(statuses[failed], "ERROR")
        self.assertEqual(statuses[interrupted], "ERROR")
        self.assertEqual(statuses[ok], IDLE)

    def test_unrecognized_sample_is_capped_and_redacted(self):
        rec = MetricsRecorder()
        rec.begin_cycle()
        rec.note_outcome(
            "unrecognized_str", False,
            "x" * 200 + " token=secretvalue /home/you/state.json ses_abc123",
        )
        snap = rec.end_cycle()
        sample = snap["outcomes"]["unrecognized_sample"]
        self.assertLessEqual(len(sample), 40)
        self.assertNotIn("secretvalue", sample)
        self.assertNotIn("/home", sample)
        self.assertNotIn("ses_", sample)

    def test_discovery_reason_scrubs_paths_and_session_ids(self):
        rec = MetricsRecorder()
        rec.begin_cycle()
        rec.note_discovery(
            0, False,
            "session.list error: cannot open /home/you/.local/state ses_abc123",
        )
        snap = rec.end_cycle()
        reason = snap["discovery"]["reason"]
        self.assertNotIn("/home", reason)
        self.assertNotIn("ses_", reason)
        self.assertIn("session.list error", reason)

    def test_archived_and_discovery_incomplete_unknown_classes(self):
        fake = FakeCli()
        fake.set_active([])
        fake.set_session_pages([{"data": []}])  # COMPLETE, empty
        fake.set_session(SID, {"id": SID, "archived": True, "outcome": "succeeded",
                               "time": {"updated": 1000}})
        fake.set_permissions(SID, [])
        fake.set_messages(SID, [])
        rec = MetricsRecorder()
        mon, _fake, _store = self.monitor(fake=fake, metrics=rec)
        mon.poll_once()
        snap = rec.snapshot()
        self.assertEqual(snap["sessions"]["unknown_reason_classes"]["archived"], 1)

        # INCOMPLETE discovery retains the pinned card as UNKNOWN.
        broken = FakeCli()
        broken.set_active([])
        broken.set_session_pages([
            {"data": [], "cursor": "loop"},
            {"data": [], "cursor": "loop"},  # repeated cursor -> cycle -> INCOMPLETE
        ])
        rec_inc = MetricsRecorder()
        mon_inc, _fake_inc, _store_inc = self.monitor(fake=broken, metrics=rec_inc)
        mon_inc.poll_once()
        snap_inc = rec_inc.snapshot()
        self.assertFalse(snap_inc["discovery"]["complete"])
        self.assertTrue(snap_inc["discovery"]["reason"])
        self.assertEqual(
            snap_inc["sessions"]["unknown_reason_classes"]["retained_unknown"], 1
        )
        self.assertEqual(snap_inc["sessions"]["polled"], 0)


# ---------------------------------------------------------------------------
# No behaviour change / fault isolation / budget boundary
# ---------------------------------------------------------------------------
class TestNoBehaviourChange(Base):
    def _run(self, metrics, state_path):
        fake = FakeCli()
        mon, fake, _store = self.monitor(
            fake=fake, metrics=metrics, state_path=state_path
        )
        self.seed_one(fake, messages=[assistant_msg("m1")])
        mon.poll_once()
        self.seed_one(fake, messages=[assistant_msg("m2"), assistant_msg("m1")])
        mon.poll_once()
        return mon, fake

    def test_status_and_calls_identical_with_metrics_stripped(self):
        mon_a, fake_a = self._run(MetricsRecorder(), os.path.join(self.tmp.name, "a.json"))
        mon_b, fake_b = self._run(None, os.path.join(self.tmp.name, "b.json"))
        self.assertEqual(fake_a.calls, fake_b.calls)  # identical call order/argv

        payload_a = status_json(mon_a, 15)
        payload_b = status_json(mon_b, 15)
        self.assertEqual(payload_a["teams"], payload_b["teams"])
        self.assertEqual(payload_a["awaiting_count"], payload_b["awaiting_count"])
        self.assertEqual(payload_a["banners"], payload_b["banners"])

        def strip(sessions):
            return [
                {k: v for k, v in session.items() if k not in ("age_seconds", "stale")}
                for session in sessions
            ]

        self.assertEqual(strip(payload_a["sessions"]), strip(payload_b["sessions"]))
        self.assertEqual(payload_a["sessions"][0]["status"], "READY")
        self.assertIsInstance(payload_b["metrics"], dict)
        self.assertEqual(payload_b["metrics"], {})  # no recorder injected
        self.assertEqual(payload_a["metrics"]["wsl2"]["cycle"]["id"], 2)

    def test_broken_recorder_cannot_break_poll_and_self_disables(self):
        broken = BrokenRecorder()
        mon, fake, _store = self.monitor(fake=FakeCli(), metrics=broken)
        self.seed_one(fake)
        mon.poll_once()  # must not raise
        team = mon.snapshot("t1")
        self.assertEqual(team.status, IDLE)
        self.assertIsInstance(mon.metrics, NullMetricsRecorder)
        self.assertEqual(len([a for a in fake.calls if a[1] == "api"]), 5)

    def test_budget_boundary_1499_passes_and_1500_breaches(self):
        clock = FakeClock(100.0)
        rec = MetricsRecorder(poll_interval=3.0, stale_threshold=15.0, clock=clock)
        rec.begin_cycle()
        clock.advance(11.99)
        snap = rec.end_cycle()
        self.assertEqual(snap["cycle"]["wall_plus_poll_seconds"], 14.99)
        self.assertFalse(snap["cycle"]["stale_breached"])

        rec.begin_cycle()
        clock.advance(12.0)
        snap = rec.end_cycle()
        self.assertEqual(snap["cycle"]["wall_plus_poll_seconds"], 15.0)
        self.assertTrue(snap["cycle"]["stale_breached"])  # >= 15 is BLOCK

    def test_status_json_age_and_stale_share_the_effective_now(self):
        mon, fake, _store = self.monitor()
        self.seed_one(fake)
        mon.poll_once()
        session = mon.snapshot("t1").sessions[0]

        current = status_json(mon, 15, now=session.updated_at + 5.0)
        row = current["sessions"][0]
        self.assertEqual(row["age_seconds"], 5.0)
        self.assertFalse(row["stale"])
        self.assertEqual(row["status"], IDLE)

        # effective_status/age use strict '>' (15.0 is not yet stale).
        edge = status_json(mon, 15, now=session.updated_at + 15.0)["sessions"][0]
        self.assertEqual(edge["age_seconds"], 15.0)
        self.assertFalse(edge["stale"])
        self.assertEqual(edge["status"], IDLE)

        breach = status_json(mon, 15, now=session.updated_at + 15.001)["sessions"][0]
        self.assertTrue(breach["stale"])
        self.assertEqual(breach["status"], UNKNOWN)

    def test_cycle_wall_is_measured_before_any_jsonl_write(self):
        clock = FakeClock(500.0)
        rec = MetricsRecorder(poll_interval=3.0, stale_threshold=15.0, clock=clock)
        rec.begin_cycle()
        clock.advance(2.0)
        snap = rec.end_cycle()
        self.assertEqual(snap["cycle"]["wall_seconds"], 2.0)
        self.assertFalse(snap["cycle"]["stale_breached"])


# ---------------------------------------------------------------------------
# JSONL (opt-in)
# ---------------------------------------------------------------------------
class TestJsonl(Base):
    @staticmethod
    def snap(cycle_id):
        rec = MetricsRecorder()
        rec.begin_cycle()
        rec.record_call("server.info", 1.0)
        rec.end_cycle()
        snapshot = rec.snapshot()
        snapshot["cycle"]["id"] = cycle_id
        return snapshot

    def test_jsonl_is_off_by_default(self):
        from monitor.__main__ import build_parser
        args = build_parser().parse_args([])
        self.assertIsNone(args.metrics_jsonl)

    def test_one_line_per_completed_cycle_no_duplicates(self):
        path = os.path.join(self.tmp.name, "metrics.jsonl")
        writer = JsonlMetricsWriter(path)
        first = self.snap(1)
        second = self.snap(2)
        self.assertTrue(writer.write("wsl2", first))
        self.assertFalse(writer.write("wsl2", first))  # duplicate cycle id
        self.assertTrue(writer.write("wsl2", second))
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line)["cycle"]["id"] for line in lines], [1, 2])
        self.assertEqual(json.loads(lines[0])["cycle"]["state"], "OK")

    def test_jsonl_lines_carry_source_and_distinguish_equal_cycle_ids(self):
        path = os.path.join(self.tmp.name, "two-sources.jsonl")
        writer = JsonlMetricsWriter(path)
        wsl = self.snap(1)
        win = self.snap(1)  # per-recorder cycle ids are both 1
        self.assertTrue(writer.write("wsl2", wsl))
        self.assertTrue(writer.write("win-windows", win))
        with open(path, "r", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle.read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual({line["source"] for line in lines}, {"wsl2", "win-windows"})
        self.assertEqual([line["cycle"]["id"] for line in lines], [1, 1])
        self.assertEqual({line["source"]: line["calls"]["total"] for line in lines},
                         {"wsl2": 1, "win-windows": 1})
        # Archival annotation only: the recorder snapshots are not mutated.
        self.assertNotIn("source", wsl)
        self.assertNotIn("source", win)

    def test_jsonl_fixed_size_cap_never_grows_past_limit(self):
        path = os.path.join(self.tmp.name, "capped.jsonl")
        self.assertEqual(MAX_JSONL_BYTES, 4 * 1024 * 1024)
        self.assertEqual(JsonlMetricsWriter(path).max_bytes, 4 * 1024 * 1024)
        first = self.snap(1)
        line = json.dumps(dict(first, source="wsl2"), sort_keys=True,
                          separators=(",", ":")) + "\n"
        writer = JsonlMetricsWriter(path, max_bytes=len(line.encode("utf-8")) + 5)
        self.assertTrue(writer.write("wsl2", first))
        self.assertFalse(writer.write("wsl2", self.snap(2)))
        self.assertTrue(writer.disabled)
        self.assertLessEqual(os.path.getsize(path), writer.max_bytes)
        self.assertLessEqual(os.path.getsize(path), MAX_JSONL_BYTES)

    def test_jsonl_write_error_is_safe(self):
        path = os.path.join(self.tmp.name, "missing-dir", "metrics.jsonl")
        writer = JsonlMetricsWriter(path)  # getsize OSError tolerated
        self.assertFalse(writer.write("wsl2", self.snap(1)))
        self.assertTrue(writer.disabled)


# ---------------------------------------------------------------------------
# Multi-source isolation / frozen-API source scan
# ---------------------------------------------------------------------------
class TestTwoSourcesAndSourceScan(Base):
    def _two_source_hub(self):
        wsl_fake = FakeCli()
        win_fake = WindowsFakeCli()
        rec_wsl = MetricsRecorder(source_key="wsl2")
        rec_win = MetricsRecorder(source_key="win-windows")
        wsl_team = TeamConfig("team-alpha", "Team Alpha", "/w/pl", session_id=SID)
        win_team = TeamConfig("team-windows", "Team Windows", WIN_DIR, session_id=WIN_SID)
        cfg = self.config(
            teams=(wsl_team,),
            sources=(
                SourceConfig("wsl2", "/fake/wsl", None, (wsl_team,)),
                SourceConfig("win-windows", WIN_BIN, None, (win_team,)),
            ),
        )
        store = StateStore(self.state_path)
        ready = ReadyPending()
        from monitor.collector import SnapshotRegistry, MultiSourceMonitor
        from monitor.__main__ import build_source_monitors
        registry = SnapshotRegistry()
        monitors = build_source_monitors(
            cfg, store, ready, registry, 15.0,
            runners={"wsl2": wsl_fake, "win-windows": win_fake},
            recorders={"wsl2": rec_wsl, "win-windows": rec_win},
        )
        hub = MultiSourceMonitor(monitors, store, ready, registry)

        wsl_fake.set_active([])
        wsl_fake.set_session_pages([{"data": []}])
        wsl_fake.set_session(SID, session_info(SID, "/w/pl"))
        wsl_fake.set_permissions(SID, [])
        wsl_fake.set_messages(SID, [assistant_msg("w1")])
        win_fake.set_rc("session.active", 1)  # Windows source fault only
        return hub, wsl_fake, win_fake, rec_wsl, rec_win

    def test_windows_fault_does_not_zero_wsl2_metrics(self):
        hub, _wsl, _win, rec_wsl, rec_win = self._two_source_hub()
        hub.poll_once()

        wsl = rec_wsl.snapshot()
        win = rec_win.snapshot()
        self.assertEqual(wsl["cycle"]["id"], 1)
        self.assertEqual(win["cycle"]["id"], 1)
        self.assertEqual(sum(wsl["calls"]["errors_by_class"].values()), 0)
        self.assertGreater(sum(wsl["sessions"]["status_counts"].values()), 0)
        self.assertEqual(win["calls"]["errors_by_class"]["rc_nonzero"], 1)
        self.assertEqual(win["sessions"]["polled"], 0)
        self.assertFalse(win["discovery"]["complete"])

        block = status_json(hub, 15)["metrics"]
        self.assertEqual(set(block), {"wsl2", "win-windows"})
        self.assertEqual(block["wsl2"]["cycle"]["id"], 1)
        self.assertEqual(block["win-windows"]["calls"]["errors_by_class"]["rc_nonzero"], 1)

    def test_frozen_api_surface_and_no_shell_constructs(self):
        self.assertEqual(len(ALLOWED_OPERATIONS), 6)
        monitor_dir = os.path.join(BASE, "monitor")
        forbidden = ("powershell", "cmd.exe", "-command", "shell=True")
        for name in ("api.py", "collector.py", "metrics.py", "web.py", "__main__.py"):
            with open(os.path.join(monitor_dir, name), "r", encoding="utf-8") as handle:
                text = handle.read().lower()
            for token in forbidden:
                self.assertNotIn(token, text, "%s contains %r" % (name, token))


if __name__ == "__main__":
    unittest.main()
