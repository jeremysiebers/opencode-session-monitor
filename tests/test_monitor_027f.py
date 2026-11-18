"""INC027f offline tests — bounded per-session fetch concurrency.

Fully offline: recording fake CLIs supply canned API responses. No network,
no real binary, no OpenCode service, no live config, no Windows host. The
production code path is unchanged except for the bounded ThreadPoolExecutor in
``Monitor._fetch_sessions``; every state mutation still happens on the poll
thread.

Run with::

    cd opencode-session-monitor && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import monitor.collector as collector_module  # noqa: E402
from monitor.api import (  # noqa: E402
    ALLOWED_OPERATIONS, MalformedApiResponse, OpencodeApi, _ALLOWED_PARAM_KEYS,
)
from monitor.collector import (  # noqa: E402
    ERROR, FETCH_CONCURRENCY, GRACE, GRACE_LIMIT, IDLE, MAX_CHILDREN, MAX_PAGES,
    MAX_PRIMARIES_PER_TEAM, MESSAGE_FALLBACK_LIMIT, MESSAGE_LIMIT, PRESENT,
    READY, UNKNOWN, WAITING_PERMISSION, Monitor, MultiSourceMonitor,
    SnapshotRegistry,
)
from monitor.config import ConfigError, load_config  # noqa: E402
from monitor.metrics import MetricsRecorder, NullMetricsRecorder, OPERATIONS  # noqa: E402
from monitor.state import ReadyPending, StateStore  # noqa: E402
from monitor.web import handle_ack, render_dashboard, status_json  # noqa: E402

from fake_cli import FakeCli  # noqa: E402
from test_monitor import assistant_msg, session_info, team  # noqa: E402

ABSENT = object()
WT1 = "/w/t1"
WT2 = "/w/t2"
WIN_DIR = "C:\\Users\\you\\projects\\team-windows"
WIN_BIN = "/mnt/c/Users/you/tools/opencode.exe"


# ---------------------------------------------------------------------------
# Fixtures / harness
# ---------------------------------------------------------------------------
def list_item(sid, directory=WT1, parent=ABSENT, archived=ABSENT):
    item = {"id": sid}
    if directory is not None:
        item["location"] = {"directory": directory}
    if parent is not ABSENT:
        item["parentID"] = parent
    if archived is not ABSENT:
        item["archived"] = archived
    return item


def pages_for(sids, page_size=2, directory=WT1, hydrated=(), empty_tail=False):
    """Cursor-paged session.list pages; ``hydrated`` items omit directory."""
    pages = []
    for start in range(0, len(sids), page_size):
        chunk = sids[start:start + page_size]
        data = [
            {"id": sid} if sid in hydrated else list_item(sid, directory)
            for sid in chunk
        ]
        page = {"data": data}
        if start + page_size < len(sids):
            page["cursor"] = "cur-%d" % (start // page_size + 1)
        pages.append(page)
    if empty_tail:
        pages[-1] = dict(pages[-1], cursor="cur-tail")
        pages.append({"data": []})
    return pages


def seed_primary(fake, sid, directory=WT1, messages=None, permissions=None,
                 info=None, archived=False):
    if info is None:
        info = session_info(sid, directory)
    if archived:
        info = dict(info, archived=True)
    fake.set_session(sid, info)
    fake.set_permissions(sid, [] if permissions is None else permissions)
    fake.set_messages(
        sid, [assistant_msg("m_" + sid)] if messages is None else messages
    )


def prime_ids(prefix, count, start=0):
    return ["ses_%s_%03d" % (prefix, i) for i in range(start, start + count)]


def calls_for(fake, session_id):
    """Argv records whose frozen param tokens mention this exact session id."""
    return [
        argv for argv in fake.calls
        if any(session_id in token for token in argv)
    ]


class FBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.fake = FakeCli()

    def write_config(self, teams, source_key="wsl2", sources=None):
        data = {
            "schema": 2,
            "opencode_bin": "/fake/opencode",
            "server_url": "",
            "poll_interval_seconds": 3,
            "stale_threshold_seconds": 15,
            "bind": "127.0.0.1",
            "port": 8765,
            "source_key": source_key,
            "teams": teams,
        }
        if sources is not None:
            data["sources"] = sources
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return load_config(self.config_path)

    def make(self, teams, fake=None, source_key="wsl2", metrics=None, cfg=None):
        fake = fake if fake is not None else self.fake
        cfg = cfg if cfg is not None else self.write_config(teams, source_key)
        store = StateStore(self.state_path, source_key=cfg.source_key)
        ready = ReadyPending()
        api = OpencodeApi(cfg.opencode_bin, runner=fake, metrics=metrics)
        mon = Monitor(cfg, api, store, ready, metrics=metrics)
        return mon, store, ready, cfg


def build_hub(tmpdir, sources, runners):
    config_path = os.path.join(tmpdir, "config.json")
    data = {
        "schema": 2,
        "opencode_bin": "/fake/opencode",
        "server_url": "",
        "poll_interval_seconds": 3,
        "stale_threshold_seconds": 15,
        "bind": "127.0.0.1",
        "port": 8765,
        "teams": [team("compat", "Compat", "/w/compat")],
        "sources": sources,
    }
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    cfg = load_config(config_path)
    store = StateStore(os.path.join(tmpdir, "state.json"))
    ready = ReadyPending()
    registry = SnapshotRegistry()
    from monitor.__main__ import build_source_monitors
    monitors = build_source_monitors(
        cfg, store, ready, registry, 15.0, runners=runners,
    )
    return MultiSourceMonitor(monitors, store, ready, registry), store, ready, cfg


def source_entry(key, teams, binary="/fake/opencode"):
    return {
        "key": key, "opencode_bin": binary, "server_url": "",
        "teams": teams, "deny_session_ids": [],
    }


def strip_volatile(payload):
    """Drop only timing/metrics fields; every decision field is compared."""
    clean = dict(payload)
    clean.pop("metrics", None)
    for section in ("teams", "sessions"):
        clean[section] = [
            {k: v for k, v in item.items() if k not in ("age_seconds", "stale")}
            for item in payload[section]
        ]
    return clean


def status_payload(mon):
    return strip_volatile(status_json(mon, 15))


def rich_scenario(fake):
    """Two teams: archived pinned card, WAITING, BUSY, ERROR, UNKNOWN, child."""
    teams = [
        team("t1", "Team One", WT1, "ses_arch"),
        team("t2", "Team Two", WT2),
    ]
    fake.set_active(["ses_c"])
    fake.set_session_pages([{"data": [
        list_item("ses_a", WT1),
        list_item("ses_b", WT1),
        list_item("ses_child", WT1, parent="ses_a"),
        list_item("ses_c", WT2),
        list_item("ses_d", WT2),
        list_item("ses_e", WT2),
    ]}])
    seed_primary(fake, "ses_a", WT1, messages=[assistant_msg("m_a1")])
    seed_primary(fake, "ses_b", WT1, messages=[assistant_msg("m_b1")],
                 permissions=[{"id": "perm-1"}])
    missing = session_info("ses_e", WT2)
    missing.pop("outcome")
    seed_primary(fake, "ses_e", WT2, info=missing)
    seed_primary(fake, "ses_c", WT2, messages=[assistant_msg("m_c1")])
    seed_primary(fake, "ses_d", WT2, messages=[assistant_msg("m_d1")],
                 info=session_info("ses_d", WT2, outcome="failed"))
    seed_primary(fake, "ses_arch", WT1, messages=[assistant_msg("m_arch")],
                 archived=True)
    return teams


# ---------------------------------------------------------------------------
# T1 — identical results vs a FETCH_CONCURRENCY=1 oracle
# ---------------------------------------------------------------------------
class T1IdenticalResults(FBase):
    @staticmethod
    def _run(concurrency, tmpdir):
        fake = FakeCli()
        teams = rich_scenario(fake)
        config_path = os.path.join(tmpdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": 2, "opencode_bin": "/fake/opencode",
                "server_url": "", "poll_interval_seconds": 3,
                "stale_threshold_seconds": 15, "bind": "127.0.0.1",
                "port": 8765, "source_key": "wsl2", "teams": teams,
            }, handle)
        cfg = load_config(config_path)
        store = StateStore(os.path.join(tmpdir, "state.json"), source_key="wsl2")
        ready = ReadyPending()
        api = OpencodeApi(cfg.opencode_bin, runner=fake)
        mon = Monitor(cfg, api, store, ready)
        with mock.patch.object(collector_module, "FETCH_CONCURRENCY", concurrency):
            mon.poll_once()  # baseline
            seed_primary(fake, "ses_a", WT1,
                         messages=[assistant_msg("m_a2"), assistant_msg("m_a1")])
            mon.poll_once()  # ses_a READY; everything else stable
        return status_payload(mon), fake.calls

    def test_cap_8_matches_cap_1_oracle(self):
        self.assertEqual(FETCH_CONCURRENCY, 8)
        with tempfile.TemporaryDirectory() as tmp1, \
                tempfile.TemporaryDirectory() as tmp8:
            oracle, oracle_calls = self._run(1, tmp1)
            parallel, parallel_calls = self._run(8, tmp8)
        self.assertEqual(oracle, parallel)
        self.assertEqual(len(oracle_calls), len(parallel_calls))
        # The decisions are materially non-trivial, not an all-IDLE fixture.
        statuses = {(s["team_key"], s["session_id"]): s["status"]
                    for s in oracle["sessions"]}
        self.assertEqual(statuses[("t1", "ses_a")], READY)
        self.assertEqual(statuses[("t1", "ses_b")], WAITING_PERMISSION)
        self.assertEqual(statuses[("t1", "ses_arch")], UNKNOWN)
        self.assertEqual(statuses[("t2", "ses_c")], "BUSY")
        self.assertEqual(statuses[("t2", "ses_d")], ERROR)
        self.assertEqual(statuses[("t2", "ses_e")], UNKNOWN)


# ---------------------------------------------------------------------------
# T2 — call budget 83 + histogram; hydration/fallback/errors cap-invariant
# ---------------------------------------------------------------------------
def budget_fixture(fake, count=23):
    """23 primaries, 13 discovery pages, one hydrated (no extra get)."""
    sids = prime_ids("b23", count)
    hydrated = {sids[5]}
    fake.set_active([])
    fake.set_session_pages(pages_for(
        sids, page_size=2, directory="/w/big", hydrated=hydrated, empty_tail=True,
    ))
    for sid in sids:
        seed_primary(fake, sid, "/w/big")
    return sids


class T2CallBudget(FBase):
    def _metrics(self, concurrency):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema": 2, "opencode_bin": "/fake/opencode",
                    "server_url": "", "poll_interval_seconds": 3,
                    "stale_threshold_seconds": 15, "bind": "127.0.0.1",
                    "port": 8765, "source_key": "wsl2",
                    "teams": [team("big", "Big", "/w/big")],
                }, handle)
            cfg = load_config(config_path)
            fake = FakeCli()
            sids = budget_fixture(fake)
            recorder = MetricsRecorder(source_key="wsl2", poll_interval=3.0,
                                       stale_threshold=15.0)
            api = OpencodeApi(cfg.opencode_bin, runner=fake, metrics=recorder)
            mon = Monitor(cfg, api, StateStore(state_path, "wsl2"),
                          ReadyPending(), metrics=recorder)
            with mock.patch.object(collector_module, "FETCH_CONCURRENCY", concurrency):
                mon.poll_once()
            return mon.metrics_snapshot(), len(fake.calls), sids

    def test_budget_83_and_histogram_at_cap_1_and_8(self):
        self.assertEqual(FETCH_CONCURRENCY, 8)
        m1, calls1, sids = self._metrics(1)
        m8, calls8, _ = self._metrics(8)
        for metrics, calls in ((m1, calls1), (m8, calls8)):
            self.assertEqual(len(sids), 23)
            self.assertEqual(calls, 83)
            self.assertEqual(calls, 1 + 13 + 23 * 3)
            calls_block = metrics["calls"]
            self.assertEqual(calls_block["total"], 83)
            self.assertEqual(calls_block["by_operation"], {
                "server.info": 0,
                "session.list": 13,
                "session.active": 1,
                "session.get": 23,
                "session.message.list": 23,
                "session.permission.list": 23,
            })
            self.assertEqual(calls_block["hydration_gets"], 1)
            self.assertEqual(calls_block["hydration_failures"], 0)
            self.assertEqual(calls_block["message_list_fallbacks"], 0)
            self.assertEqual(set(calls_block["errors_by_class"].values()), {0})
            self.assertEqual(metrics["sessions"]["primaries_seen"], 23)
            self.assertEqual(metrics["sessions"]["polled"], 23)
            self.assertEqual(metrics["sessions"]["status_counts"]["IDLE"], 23)
            self.assertEqual(metrics["discovery"]["pages"], 13)
            self.assertTrue(metrics["discovery"]["complete"])
            self.assertEqual(metrics["discovery"]["primaries"], 23)
        # Concurrency never changes a count, only the latency distribution.
        self.assertEqual(m1["calls"], m8["calls"])
        self.assertEqual(m1["sessions"], m8["sessions"])
        self.assertEqual(m1["discovery"], m8["discovery"])

    def test_hydration_fallback_and_errors_reach_identical_counts(self):
        def run(concurrency):
            with tempfile.TemporaryDirectory() as tmp:
                fake = FallbackCli()
                teams = [team("t1", "Team One", WT1)]
                fake.set_active([])
                fake.set_session_pages([{"data": [
                    {"id": "ses_h"},  # no directory -> hydration get, cached
                    list_item("ses_f", WT1),
                    list_item("ses_ok", WT1),
                ]}])
                seed_primary(fake, "ses_h", WT1)
                seed_primary(fake, "ses_f", WT1)
                seed_primary(fake, "ses_ok", WT1)
                fake.malformed.add("ses_f")
                fake.error(("session.permission.list", "ses_ok"), OSError("perm down"))
                recorder = MetricsRecorder()
                cfg_path = os.path.join(tmp, "config.json")
                with open(cfg_path, "w", encoding="utf-8") as handle:
                    json.dump({
                        "schema": 2, "opencode_bin": "/fake/opencode",
                        "server_url": "", "poll_interval_seconds": 3,
                        "stale_threshold_seconds": 15, "bind": "127.0.0.1",
                        "port": 8765, "source_key": "wsl2", "teams": teams,
                    }, handle)
                cfg = load_config(cfg_path)
                mon = Monitor(
                    cfg, OpencodeApi(cfg.opencode_bin, runner=fake, metrics=recorder),
                    StateStore(os.path.join(tmp, "state.json"), "wsl2"),
                    ReadyPending(), metrics=recorder,
                )
                with mock.patch.object(collector_module, "FETCH_CONCURRENCY",
                                       concurrency):
                    mon.poll_once()
                return mon.metrics_snapshot()["calls"], status_payload(mon)

        calls1, status1 = run(1)
        calls8, status8 = run(8)
        self.assertEqual(calls1, calls8)
        self.assertEqual(status1, status8)
        # Hydration get is reused by the fetch (no duplicate session.get).
        self.assertEqual(calls1["hydration_gets"], 1)
        self.assertEqual(calls1["by_operation"]["session.get"], 3)
        self.assertEqual(calls1["message_list_fallbacks"], 1)
        self.assertEqual(calls1["by_operation"]["session.message.list"], 3)
        self.assertEqual(calls1["errors_by_class"]["transport"], 1)


class FallbackCli(FakeCli):
    """FakeCli that fails the first (limit=5) message page for one session."""

    def __init__(self):
        super().__init__()
        self.malformed = set()

    def _handle(self, argv):
        operation, params = self._parse(argv)
        sid = params.get("sessionID")
        if (operation == "session.message.list" and sid in self.malformed
                and params.get("limit") == str(MESSAGE_LIMIT)):
            raise MalformedApiResponse("malformed api response")
        return super()._handle(argv)


# ---------------------------------------------------------------------------
# T3 — bounded in-flight count
# ---------------------------------------------------------------------------
class T3InFlightBound(FBase):
    def _run(self, concurrency):
        fake = FakeCli()
        sids = prime_ids("inflight", 23)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=12, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        fake.delay = 0.03
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake)
        with mock.patch.object(collector_module, "FETCH_CONCURRENCY", concurrency):
            mon.poll_once()
        return fake.max_in_flight

    def test_parallelism_is_bounded_by_1_and_fetch_concurrency(self):
        self.assertGreater(self._run(8), 1)   # actually parallel
        self.assertLessEqual(self._run(8), 8)  # never above the cap
        self.assertEqual(self._run(1), 1)      # oracle: strictly sequential


# ---------------------------------------------------------------------------
# T4 — single-writer state: store/ready mutations only on the poll thread
# ---------------------------------------------------------------------------
class T4SingleWriter(FBase):
    def _three_primaries(self, fake):
        sids = prime_ids("writer", 3)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        return sids

    def test_store_record_and_ready_set_run_on_poll_thread(self):
        fake = FakeCli()
        sids = self._three_primaries(fake)
        mon, store, ready, _ = self.make([team("big", "Big", "/w/big")], fake=fake)

        records = []
        orig_record, orig_set = store.record, ready.set

        def record(*args, **kwargs):
            records.append(("record", threading.get_ident(),
                            threading.current_thread().name))
            return orig_record(*args, **kwargs)

        def set_ready(*args, **kwargs):
            records.append(("set", threading.get_ident(),
                            threading.current_thread().name))
            return orig_set(*args, **kwargs)

        store.record = record
        ready.set = set_ready
        first = threading.Thread(target=mon.poll_once, name="monitor-poll-baseline")
        first.start()
        first.join(10)
        self.assertFalse(first.is_alive())
        baseline_records = list(records)
        records.clear()
        self.assertTrue(any(kind == "record" for kind, _i, _n in baseline_records))

        seed_primary(fake, sids[0], "/w/big",
                     messages=[assistant_msg("m2_" + sids[0]),
                               assistant_msg("m_" + sids[0])])
        second = threading.Thread(target=mon.poll_once, name="monitor-poll-change")
        second.start()
        second.join(10)
        self.assertFalse(second.is_alive())

        self.assertEqual({kind for kind, _i, _n in records}, {"record", "set"})
        for kind, ident, name in baseline_records + records:
            self.assertFalse(name.startswith("monitor-fetch-"), (kind, name))
            self.assertIn(ident, {first.ident, second.ident})
        self.assertEqual(
            {i for _k, i, _n in baseline_records}, {first.ident}
        )
        self.assertEqual({i for _k, i, _n in records}, {second.ident})

    def test_info_cache_is_written_only_by_the_calling_poll_thread(self):
        fake = FakeCli()
        sids = self._three_primaries(fake)
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake)
        cache = RecordingDict()
        results = []

        def run():
            results.append(mon._fetch_sessions(sorted(sids), cache))

        caller = threading.Thread(target=run, name="poll-cache-writer")
        caller.start()
        caller.join(10)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(set(cache), set(sids))
        self.assertTrue(cache.write_idents)
        for ident, name in cache.write_idents:
            self.assertEqual(ident, caller.ident)
            self.assertFalse(name.startswith("monitor-fetch-"))

    def test_note_polled_and_note_status_stay_on_poll_thread(self):
        fake = FakeCli()
        sids = self._three_primaries(fake)
        recorder = HookThreadRecorder()
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake,
                                 metrics=recorder)
        caller = threading.Thread(target=mon.poll_once, name="poll-hooks")
        caller.start()
        caller.join(10)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(recorder.polled), len(sids))
        self.assertEqual(len(recorder.statuses), len(sids))
        for ident, name in recorder.polled + recorder.statuses:
            self.assertEqual(ident, caller.ident)
            self.assertFalse(name.startswith("monitor-fetch-"))

    def test_derive_order_is_sorted_live_ids_not_completion_order(self):
        fake = JitterCli()
        sids = prime_ids("order", 6)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for index, sid in enumerate(sids):
            seed_primary(fake, sid, "/w/big")
            # The first sorted id is the slowest; a completion-order derive
            # would observe approximately the reverse order.
            fake.jitter[sid] = (len(sids) - index) * 0.01
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake)
        order = []
        original = mon._derive_session

        def derive(team_obj, sid, *args, **kwargs):
            order.append(sid)
            return original(team_obj, sid, *args, **kwargs)

        mon._derive_session = derive
        mon.poll_once()
        self.assertEqual(order, sorted(sids))
        self.assertTrue(all(s.status == IDLE for s in mon.snapshot("big").sessions))


class RecordingDict(dict):
    """dict that records the thread of every ``__setitem__`` (cache writer)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_idents = []

    def __setitem__(self, key, value):
        self.write_idents.append(
            (threading.get_ident(), threading.current_thread().name)
        )
        super().__setitem__(key, value)


class HookThreadRecorder(MetricsRecorder):
    def __init__(self):
        super().__init__()
        self.polled = []
        self.statuses = []

    def note_polled(self):
        self.polled.append((threading.get_ident(), threading.current_thread().name))
        super().note_polled()

    def note_status(self, status):
        self.statuses.append((threading.get_ident(), threading.current_thread().name))
        super().note_status(status)


class JitterCli(FakeCli):
    """FakeCli with a per-session delay so fetch completion order is reversed."""

    def __init__(self):
        super().__init__()
        self.jitter = {}

    def _begin(self, argv):
        _operation, params = self._parse(argv)
        sid = params.get("sessionID")
        if sid in self.jitter:
            time.sleep(self.jitter[sid])
        super()._begin(argv)


# ---------------------------------------------------------------------------
# T5 — per-operation error isolation inside the pool
# ---------------------------------------------------------------------------
class T5ErrorIsolation(FBase):
    def test_each_fetch_op_error_only_marks_its_own_card(self):
        fake = FakeCli()
        sids = prime_ids("err", 5)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        fake.error(("session.get", sids[0]), OSError("get down"))
        fake.error(("session.permission.list", sids[1]), OSError("perm down"))
        fake.error(("session.message.list", sids[2]), OSError("msgs down"))
        recorder = MetricsRecorder()
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake,
                                 metrics=recorder)
        mon.poll_once()
        cards = {s.session_id: s for s in mon.snapshot("big").sessions}
        for sid in sids[:3]:
            self.assertEqual(cards[sid].status, UNKNOWN, sid)
            self.assertTrue(cards[sid].reason.startswith("session error:"), sid)
        for sid in sids[3:]:
            self.assertEqual(cards[sid].status, IDLE, sid)
        self.assertNotEqual(mon.snapshot("big").status, UNKNOWN)
        self.assertEqual(recorder.snapshot()["calls"]["errors_by_class"]["transport"], 3)


# ---------------------------------------------------------------------------
# T6 — a hung call times out; sibling cards remain healthy
# ---------------------------------------------------------------------------
class T6HungCall(FBase):
    def test_timeout_card_and_metrics(self):
        fake = FakeCli()
        sids = prime_ids("hang", 4)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        fake.error(("session.message.list", sids[1]),
                   subprocess.TimeoutExpired(cmd=["opencode"], timeout=15))
        recorder = MetricsRecorder()
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake,
                                 metrics=recorder)
        mon.poll_once()
        cards = {s.session_id: s for s in mon.snapshot("big").sessions}
        self.assertEqual(cards[sids[1]].status, UNKNOWN)
        self.assertEqual(cards[sids[1]].reason, "session error: api timeout")
        for sid in (sids[0], sids[2], sids[3]):
            self.assertEqual(cards[sid].status, IDLE)
        self.assertEqual(recorder.snapshot()["calls"]["errors_by_class"]["timeout"], 1)


# ---------------------------------------------------------------------------
# T7 — no worker thread survives a poll
# ---------------------------------------------------------------------------
class T7NoThreadLeak(FBase):
    def test_no_monitor_fetch_threads_after_five_polls(self):
        fake = FakeCli()
        sids = prime_ids("leak", 23)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=12, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        fake.delay = 0.001
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake)
        for _ in range(5):
            mon.poll_once()
            leaked = [t.name for t in threading.enumerate()
                      if t.name.startswith("monitor-fetch-")]
            self.assertEqual(leaked, [])


# ---------------------------------------------------------------------------
# T8 — exact metrics; a broken recorder in a worker cannot corrupt the cycle
# ---------------------------------------------------------------------------
class T8Metrics(FBase):
    def test_metrics_are_exact_for_the_cap8_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeCli()
            budget_fixture(fake)
            recorder = MetricsRecorder(source_key="wsl2", poll_interval=3.0,
                                       stale_threshold=15.0)
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema": 2, "opencode_bin": "/fake/opencode",
                    "server_url": "", "poll_interval_seconds": 3,
                    "stale_threshold_seconds": 15, "bind": "127.0.0.1",
                    "port": 8765, "source_key": "wsl2",
                    "teams": [team("big", "Big", "/w/big")],
                }, handle)
            cfg = load_config(cfg_path)
            mon = Monitor(
                cfg, OpencodeApi(cfg.opencode_bin, runner=fake, metrics=recorder),
                StateStore(os.path.join(tmp, "state.json"), "wsl2"),
                ReadyPending(), metrics=recorder,
            )
            mon.poll_once()
            snapshot = mon.metrics_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["calls"]["total"], 83)
        self.assertEqual(snapshot["calls"]["by_operation"]["session.get"], 23)
        for operation in OPERATIONS:
            stats = snapshot["latency_ms"][operation]
            count = snapshot["calls"]["by_operation"][operation]
            self.assertEqual(stats["count"], count)
            self.assertEqual(stats["samples"], min(count, 512))
            if count:
                self.assertIsNotNone(stats["p50"])
                self.assertIsNotNone(stats["p95"])
                self.assertIsNotNone(stats["max"])
                self.assertLessEqual(stats["p50"], stats["p95"])
                self.assertLessEqual(stats["p95"], stats["max"])
            else:
                self.assertIsNone(stats["max"])

    def test_worker_metrics_fault_does_not_corrupt_the_cycle(self):
        fake = FallbackCli()
        sids = prime_ids("broken", 3)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        fake.malformed.add(sids[1])  # worker calls note_message_list_fallback
        recorder = ExplodingRecorder()
        mon, _, _, _ = self.make([team("big", "Big", "/w/big")], fake=fake,
                                 metrics=recorder)
        mon.poll_once()  # must not raise
        cards = {s.session_id: s for s in mon.snapshot("big").sessions}
        self.assertEqual(len(cards), 3)
        self.assertTrue(all(c.status == IDLE for c in cards.values()))
        # The monitor's metrics recorder self-disabled; status is unaffected.
        self.assertIs(mon.metrics.__class__, NullMetricsRecorder)
        self.assertIsNone(mon.metrics_snapshot())


class ExplodingRecorder(MetricsRecorder):
    """Raises from a worker-called mutator; production code must absorb it."""

    def record_call(self, operation, ms, error_class=None):
        raise RuntimeError("metrics recorder down")

    def note_message_list_fallback(self):
        raise RuntimeError("metrics recorder down")


# ---------------------------------------------------------------------------
# T9 — no cross-poll info cache
# ---------------------------------------------------------------------------
class T9NoCrossPollCache(FBase):
    def test_mutated_api_responses_are_visible_on_the_next_poll(self):
        fake = FakeCli()
        fake.set_active([])
        fake.set_session_pages([{"data": [list_item("ses_a", WT1)]}])
        seed_primary(fake, "ses_a", WT1, messages=[assistant_msg("m1")])
        mon, _, _, _ = self.make([team("t1", "Team One", WT1)], fake=fake)
        mon.poll_once()
        card = mon.snapshot("t1").sessions[0]
        self.assertEqual(card.status, IDLE)
        self.assertEqual(card.title, "")

        seed_primary(fake, "ses_a", WT1,
                     messages=[assistant_msg("m2", text="new answer"),
                               assistant_msg("m1")],
                     permissions=[{"id": "perm-1"}],
                     info=session_info("ses_a", WT1, title="changed title"))
        mon.poll_once()
        card = mon.snapshot("t1").sessions[0]
        self.assertEqual(card.status, WAITING_PERMISSION)
        self.assertEqual(card.title, "changed title")
        self.assertEqual(card.summary, "new answer")


# ---------------------------------------------------------------------------
# T10 — two sources use isolated, per-source pools
# ---------------------------------------------------------------------------
class ThreadRecordingCli(FakeCli):
    def __init__(self):
        super().__init__()
        self.thread_names = []

    def _begin(self, argv):
        operation, _params = self._parse(argv)
        with self._lock:
            self.thread_names.append((threading.current_thread().name, operation))
        super()._begin(argv)


class T10TwoSourcePools(FBase):
    def test_pools_are_per_source_and_never_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            wsl_cli = ThreadRecordingCli()
            win_cli = ThreadRecordingCli()
            wsl_sids = prime_ids("wsl", 5)
            win_sids = prime_ids("win", 5)
            for cli, sids, directory in (
                (wsl_cli, wsl_sids, "/w/pl"),
                (win_cli, win_sids, WIN_DIR),
            ):
                cli.set_active([])
                cli.set_session_pages(pages_for(sids, page_size=10,
                                                directory=directory))
                for sid in sids:
                    seed_primary(cli, sid, directory)
                cli.delay = 0.01
            sources = [
                source_entry("wsl2", [team("pl", "PL", "/w/pl")]),
                source_entry("win-windows",
                             [team("team-windows", "Team Windows", WIN_DIR)],
                             binary=WIN_BIN),
            ]
            hub, _, _, _ = build_hub(
                tmp, sources,
                {"wsl2": wsl_cli, "win-windows": win_cli},
            )
            hub.poll_once()
        fetch_ops = {"session.get", "session.message.list", "session.permission.list"}
        wsl_fetch = [name for name, op in wsl_cli.thread_names if op in fetch_ops]
        win_fetch = [name for name, op in win_cli.thread_names if op in fetch_ops]
        self.assertTrue(wsl_fetch)
        self.assertTrue(win_fetch)
        self.assertTrue(all(n.startswith("monitor-fetch-wsl2") for n in wsl_fetch))
        self.assertTrue(all(n.startswith("monitor-fetch-win-windows")
                            for n in win_fetch))
        self.assertFalse(set(wsl_fetch) & set(win_fetch))
        self.assertGreater(wsl_cli.max_in_flight, 1)
        self.assertGreater(win_cli.max_in_flight, 1)
        self.assertLessEqual(wsl_cli.max_in_flight, FETCH_CONCURRENCY)
        self.assertLessEqual(win_cli.max_in_flight, FETCH_CONCURRENCY)
        self.assertEqual(hub.snapshot("pl").status, IDLE)
        self.assertEqual(hub.snapshot("team-windows").status, IDLE)


# ---------------------------------------------------------------------------
# T11 — determinism under random per-call latency (20 runs)
# ---------------------------------------------------------------------------
class T11Determinism(FBase):
    def test_twenty_runs_with_random_latency_are_identical(self):
        payloads = []
        for run in range(20):
            with tempfile.TemporaryDirectory() as tmp:
                fake = FakeCli()
                teams = rich_scenario(fake)
                fake.delay = random.Random(run).uniform(0.0, 0.002)
                cfg_path = os.path.join(tmp, "config.json")
                with open(cfg_path, "w", encoding="utf-8") as handle:
                    json.dump({
                        "schema": 2, "opencode_bin": "/fake/opencode",
                        "server_url": "", "poll_interval_seconds": 3,
                        "stale_threshold_seconds": 15, "bind": "127.0.0.1",
                        "port": 8765, "source_key": "wsl2", "teams": teams,
                    }, handle)
                cfg = load_config(cfg_path)
                mon = Monitor(
                    cfg, OpencodeApi(cfg.opencode_bin, runner=fake),
                    StateStore(os.path.join(tmp, "state.json"), "wsl2"),
                    ReadyPending(),
                )
                mon.poll_once()  # baseline
                seed_primary(fake, "ses_a", WT1,
                             messages=[assistant_msg("m_a2"),
                                       assistant_msg("m_a1")])
                mon.poll_once()  # READY
                payloads.append(status_payload(mon))
        for payload in payloads[1:]:
            self.assertEqual(payload, payloads[0])


# ---------------------------------------------------------------------------
# T12 — preservation of every INC027d/e behaviour
# ---------------------------------------------------------------------------
class T12Preservation(FBase):
    def test_six_groups_and_23_primary_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            wsl_cli = FakeCli()
            win_cli = FakeCli()
            wsl_cli.set_active([])
            wsl_cli.set_session_pages([{"data": []}])
            seed_primary(wsl_cli, "ses_wsl_pl", "/w/pl")
            win_sids = prime_ids("win", 23)
            win_cli.set_active([])
            win_cli.set_session_pages(pages_for(
                win_sids, page_size=2, directory=WIN_DIR, empty_tail=True,
            ))
            for sid in win_sids:
                seed_primary(win_cli, sid, WIN_DIR)
            sources = [
                source_entry("wsl2", [
                    team("team-alpha", "Team Alpha", "/w/pl", "ses_wsl_pl"),
                    team("team-beta", "Team Beta", "/w/wp"),
                    team("team-gamma", "Team Gamma", "/w/team-gamma"),
                    team("team-delta", "Team Delta", "/w/team-delta"),
                    team("team-epsilon", "Team Epsilon", "/w/team-epsilon"),
                ]),
                source_entry("win-windows", [
                    team("team-windows", "Team Windows", WIN_DIR),
                ], binary=WIN_BIN),
            ]
            hub, _, _, _ = build_hub(
                tmp, sources, {"wsl2": wsl_cli, "win-windows": win_cli},
            )
            hub.poll_once()
            page = render_dashboard(hub, 15, 5)
            payload = status_json(hub, 15)
            win_snap = hub.snapshot("team-windows")
            win_calls = len(win_cli.calls)
        for label in ("Team Alpha", "Team Beta", "Team Gamma", "Team Delta",
                      "Team Epsilon", "Team Windows"):
            self.assertIn(label, page)
        self.assertIn("source: wsl2", page)
        self.assertIn("source: win-windows", page)
        self.assertEqual(len(payload["teams"]), 6)
        self.assertTrue(win_snap.discovery_complete)
        self.assertFalse(win_snap.overflow)
        self.assertEqual(len(win_snap.sessions), 23)
        self.assertEqual(set(win_snap.session_ids), set(win_sids))
        self.assertEqual(win_snap.status, IDLE)
        self.assertEqual(win_calls, 1 + 13 + 23 * 3)  # still 83

    def test_ready_gezien_grace_dropped_unchanged(self):
        fake = FakeCli()
        sids = prime_ids("retain", 3)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        mon, store, ready, _ = self.make([team("big", "Big", "/w/big")], fake=fake)
        mon.poll_once()
        seed_primary(fake, sids[0], "/w/big",
                     messages=[assistant_msg("new"), assistant_msg("m_" + sids[0])])
        mon.poll_once()
        self.assertEqual(
            {s.session_id: s.status for s in mon.snapshot("big").sessions}[sids[0]],
            READY,
        )
        before = len(fake.calls)
        self.assertTrue(handle_ack(mon, session_id=sids[0], source_key="wsl2"))
        self.assertEqual(len(fake.calls), before)  # Gezien never calls OpenCode
        self.assertEqual(
            {s.session_id: s.status for s in mon.snapshot("big").sessions}[sids[0]],
            IDLE,
        )
        fake.set_session_pages(pages_for(sids[1:], page_size=10, directory="/w/big"))
        mon.poll_once()
        self.assertEqual(
            {s.session_id: s for s in mon.snapshot("big").sessions}[sids[0]].membership,
            GRACE,
        )
        mon.poll_once()
        self.assertEqual(
            {s.session_id: s for s in mon.snapshot("big").sessions}[sids[0]].membership,
            GRACE,
        )
        mon.poll_once()
        dropped = {s.session_id: s for s in mon.snapshot("big").sessions}
        self.assertNotIn(sids[0], dropped)
        self.assertIsNotNone(store.last_seen(sids[0], "wsl2"))  # baseline kept

    def test_overflow_33_still_incomplete_and_unadmitted_calls_zero(self):
        fake = FakeCli()
        prime = "ses_win_000"
        fake.set_active([])
        fake.set_session_pages([{"data": [list_item(prime, "/w/big")]}])
        seed_primary(fake, prime, "/w/big")
        mon, _, _, _ = self.make([team("big", "Big", "/w/big", prime)], fake=fake)
        mon.poll_once()
        self.assertEqual(mon.snapshot("big").session_ids, (prime,))

        overflow = prime_ids("win", 33)
        fake.set_session_pages(pages_for(overflow, page_size=12, directory="/w/big"))
        for sid in overflow:
            seed_primary(fake, sid, "/w/big")
        new_ids = set(overflow) - {prime}
        before = {sid: len(calls_for(fake, sid)) for sid in new_ids}
        mon.poll_once()
        snap = mon.snapshot("big")
        self.assertFalse(snap.discovery_complete)
        self.assertTrue(snap.overflow)
        self.assertIn("primary overflow (33 > 32)", snap.discovery_reason)
        self.assertEqual(snap.session_ids, (prime,))
        self.assertEqual(snap.status, UNKNOWN)
        for sid in new_ids:
            self.assertEqual(
                len(calls_for(fake, sid)), before[sid],
                "unadmitted candidate %s was fetched" % sid,
            )

    def test_discovery_incomplete_does_zero_per_session_calls(self):
        fake = FakeCli()
        sids = prime_ids("inc", 3)
        fake.set_active([])
        fake.set_session_pages(pages_for(sids, page_size=10, directory="/w/big"))
        for sid in sids:
            seed_primary(fake, sid, "/w/big")
        mon, store, ready, _ = self.make([team("big", "Big", "/w/big")], fake=fake)
        mon.poll_once()
        seed_primary(fake, sids[0], "/w/big",
                     messages=[assistant_msg("new"), assistant_msg("m_" + sids[0])])
        mon.poll_once()
        self.assertTrue(ready.has(sids[0], "wsl2"))
        before = {sid: len(calls_for(fake, sid)) for sid in sids}
        fake.error("session.list", OSError("discovery lost"))
        mon.poll_once()
        snap = mon.snapshot("big")
        self.assertFalse(snap.discovery_complete)
        for card in snap.sessions:
            self.assertEqual(card.status, UNKNOWN)
            self.assertTrue(card.incomplete)
        for sid in sids:
            self.assertEqual(len(calls_for(fake, sid)), before[sid])
        self.assertTrue(ready.has(sids[0], "wsl2"))
        self.assertEqual(store.last_seen(sids[0], "wsl2"), "new")

    def test_archived_primary_uses_exactly_one_get(self):
        fake = FakeCli()
        fake.set_active([])
        fake.set_session_pages([{"data": []}])
        seed_primary(fake, "ses_arch", WT1, archived=True)
        mon, _, _, _ = self.make([team("t1", "Team One", WT1, "ses_arch")], fake=fake)
        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertEqual(snap.session_ids, ("ses_arch",))
        card = snap.sessions[0]
        self.assertEqual(card.status, UNKNOWN)
        self.assertEqual(card.reason, "session archived")
        self.assertEqual(len(calls_for(fake, "ses_arch")), 1)
        self.assertFalse(any("session.permission.list" in c for c in fake.calls))
        self.assertFalse(any("session.message.list" in c for c in fake.calls))

    def test_child_is_advisory_busy_and_never_fetched(self):
        fake = FakeCli()
        fake.set_active(["ses_child"])
        fake.set_session_pages([{"data": [
            list_item("ses_a", WT1),
            list_item("ses_child", WT1, parent="ses_a"),
        ]}])
        seed_primary(fake, "ses_a", WT1)
        seed_primary(fake, "ses_child", WT1)
        mon, _, _, _ = self.make([team("t1", "Team One", WT1)], fake=fake)
        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertEqual([s.session_id for s in snap.sessions], ["ses_a"])
        self.assertEqual(snap.sessions[0].status, "BUSY")
        self.assertEqual(snap.sessions[0].reason, "child session active")
        self.assertEqual(snap.sessions[0].child_ids, ("ses_child",))
        self.assertEqual(calls_for(fake, "ses_child"), [])

    def test_current_state_precedes_failed_outcome(self):
        fake = FakeCli()
        fake.set_active(["ses_a"])  # busy must lose to a pending permission
        fake.set_session_pages([{"data": [list_item("ses_a", WT1)]}])
        seed_primary(fake, "ses_a", WT1,
                     permissions=[{"id": "perm-1"}])
        mon, _, _, _ = self.make([team("t1", "Team One", WT1)], fake=fake)
        mon.poll_once()
        card = mon.snapshot("t1").sessions[0]
        self.assertEqual(card.status, WAITING_PERMISSION)
        self.assertEqual(card.reason, "permission request pending")

        seed_primary(fake, "ses_a", WT1,
                     permissions=[{"id": "perm-1"}],
                     info=session_info("ses_a", WT1, outcome="failed"))
        mon.poll_once()
        card = mon.snapshot("t1").sessions[0]
        # INC027i: a pending permission is a CURRENT state and outranks the
        # PREVIOUS turn's failed outcome; the failure is history, not current.
        self.assertEqual(card.status, WAITING_PERMISSION)
        self.assertEqual(card.reason, "permission request pending")


# ---------------------------------------------------------------------------
# T13 — no new external surface
# ---------------------------------------------------------------------------
class T13NoNewSurface(unittest.TestCase):
    def test_collector_uses_only_stdlib_concurrent_futures(self):
        path = os.path.join(BASE, "monitor", "collector.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        roots = set()
        has_concurrent = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
                    if alias.name == "concurrent.futures":
                        has_concurrent = True
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # package-relative monitor imports
                if node.module:
                    roots.add(node.module.split(".")[0])
                    if node.module == "concurrent.futures":
                        has_concurrent = True
        self.assertTrue(has_concurrent)
        self.assertLessEqual(
            roots,
            {"__future__", "concurrent", "threading", "time", "dataclasses"},
        )
        self.assertNotIn("shell=true", source.lower())
        self.assertNotIn("subprocess", source)

    def test_operations_params_and_caps_are_unchanged(self):
        self.assertEqual(ALLOWED_OPERATIONS, frozenset({
            "server.info", "session.list", "session.active",
            "session.get", "session.message.list", "session.permission.list",
        }))
        self.assertEqual(
            _ALLOWED_PARAM_KEYS,
            frozenset({"sessionID", "limit", "order", "cursor", "type"}),
        )
        self.assertEqual(FETCH_CONCURRENCY, 8)
        self.assertEqual(MESSAGE_LIMIT, 5)
        self.assertEqual(MESSAGE_FALLBACK_LIMIT, 1)
        self.assertEqual(MAX_PRIMARIES_PER_TEAM, 32)
        self.assertEqual(MAX_CHILDREN, 16)
        self.assertEqual(GRACE_LIMIT, 2)
        self.assertEqual(MAX_PAGES, 64)
        from monitor.collector import PAGE_LIMIT
        self.assertEqual(PAGE_LIMIT, 50)
        from monitor.config import (DEFAULT_POLL_INTERVAL,
                                    DEFAULT_STALE_THRESHOLD)
        self.assertEqual(DEFAULT_POLL_INTERVAL, 3.0)
        self.assertEqual(DEFAULT_STALE_THRESHOLD, 15.0)

    def test_no_fetch_concurrency_config_key_or_cli_flag(self):
        fake = FakeCli()  # unused; keeps this test independent of the network
        self.assertIsNotNone(fake)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema": 2, "opencode_bin": "/fake/opencode",
                    "server_url": "", "poll_interval_seconds": 3,
                    "stale_threshold_seconds": 15, "bind": "127.0.0.1",
                    "port": 8765, "teams": [team("t1", "Team One", WT1)],
                    "fetch_concurrency": 8,
                }, handle)
            with self.assertRaises(ConfigError):
                load_config(path)
        from monitor.__main__ import build_parser
        parser = build_parser()
        options = {opt for action in parser._actions for opt in action.option_strings}
        self.assertEqual(options, {
            "-h", "--help", "--version", "--config", "--state", "--bind", "--port",
            "--timeout", "--metrics-jsonl", "--allow-external-bind",
        })
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--fetch-concurrency", "8"])


if __name__ == "__main__":
    unittest.main()
