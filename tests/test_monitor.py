"""Fully offline tests for the OpenCode Session Monitor.

No network, no real OpenCode binary: a recording fake CLI supplies canned API
responses. Run with::

    cd opencode-session-monitor && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor.api import (  # noqa: E402
    ALLOWED_OPERATIONS, MalformedApiResponse, MonitorApiError, OpencodeApi,
)
from monitor.collector import (  # noqa: E402
    BUSY, ERROR, IDLE, READY, UNKNOWN, WAITING_PERMISSION, Monitor, effective_status,
    MESSAGE_FALLBACK_LIMIT, MESSAGE_LIMIT,
)
from monitor.config import ConfigError, load_config  # noqa: E402
from monitor.redaction import (  # noqa: E402
    MAX_CHARS_PER_MESSAGE, MAX_MESSAGES, MAX_TOTAL_CHARS,
    cap_text, redact, summarize_messages,
)
from monitor.state import ReadyPending, StateStore  # noqa: E402
from monitor.web import (  # noqa: E402
    RefusedBind, handle_ack, is_loopback, render_dashboard, serve, status_json,
)

from fake_cli import FakeCli  # noqa: E402

SID = "ses_aaa"
SID_B = "ses_bbb"


def stub_getaddrinfo(host, port, *args, **kwargs):
    """Offline resolution stub: only real loopback literals/names resolve local."""
    if host == "127.0.0.1.evil.com":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.7", 0))]
    if host == "mixed.example":
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", 0)),
        ]
    if host == "localhost":
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]
    if host == "::1":
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]
    if host == "127.0.0.1":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    raise socket.gaierror("name resolution failed")


def session_info(sid, directory="/w/t1", outcome="succeeded", updated=1000, title=""):
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


def team(key="t1", label="Team One", directory="/w/t1", sid=None):
    entry = {"key": key, "label": label, "worktree_dir": directory}
    if sid is not None:
        entry["session_id"] = sid
    return entry


def big_history(count=20, first_text_chars=300000):
    """Synthetic long history whose first (newest) message carries ~300 KB text."""
    messages = [assistant_msg("msg_big", text="x" * first_text_chars)]
    messages += [assistant_msg("msg_%d" % i, text="y" * 200)
                 for i in range(1, count)]
    return messages


def truncated_json_stdout(messages, cut_char=257600):
    """Return raw rc-0 stdout cut mid-string, reproducing the INC027 §11 signature."""
    raw = json.dumps({"cursor": "cursor-abc", "data": messages})
    if len(raw) <= cut_char:
        raise AssertionError("payload too small to cut at %d (len %d)" % (cut_char, len(raw)))
    cut = raw[:cut_char]
    try:
        json.loads(cut)
    except ValueError:
        return cut  # unterminated JSON string -> malformed, as observed in V3
    raise AssertionError("cut did not produce malformed JSON")


class LimitAwareCli(FakeCli):
    """FakeCli that can return a distinct raw stdout or raise per message-list limit."""

    def __init__(self):
        super().__init__()
        self.by_limit = {}
        self.errors_by_limit = {}

    def set_limit(self, sid, limit, value):
        self.by_limit[(sid, str(limit))] = value
        return self

    def error_limit(self, sid, limit, exception):
        """Make the message.list call for this sid+limit raise ``exception``."""
        self.errors_by_limit[(sid, str(limit))] = exception
        return self

    def __call__(self, argv, timeout=None):
        operation, params = self._parse(argv)
        sid = params.get("sessionID")
        lookup = (sid, params.get("limit"))
        if operation == "session.message.list" and lookup in self.errors_by_limit:
            self.calls.append(list(argv))
            raise self.errors_by_limit[lookup]
        if operation == "session.message.list" and lookup in self.by_limit:
            self.calls.append(list(argv))
            value = self.by_limit[lookup]
            stdout = value if isinstance(value, str) else json.dumps(value)
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        return super().__call__(argv, timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeCli()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")

    def make_monitor(self, teams, **overrides):
        config = {
            "schema": 1,
            "opencode_bin": "/fake/opencode",
            "server_url": overrides.get("server_url"),
            "poll_interval_seconds": overrides.get("poll", 3),
            "stale_threshold_seconds": overrides.get("stale", 15),
            "bind": "127.0.0.1",
            "port": 8765,
            "teams": teams,
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        cfg = load_config(self.config_path)
        api = OpencodeApi(cfg.opencode_bin, server_url=cfg.server_url, runner=self.fake)
        store = StateStore(self.state_path)
        ready = ReadyPending()
        return Monitor(cfg, api, store, ready), store, ready

    def scenario(self, mon, sid=SID, *, key="t1", outcome="succeeded", perms=None,
                 active=(), messages=None, directory="/w/t1", title=""):
        self.fake.set_active(active)
        self.fake.set_session(sid, session_info(sid, directory, outcome, title=title))
        self.fake.set_permissions(sid, perms or [])
        self.fake.set_messages(sid, messages or [])
        mon.poll_once()
        return mon.snapshot(key)


# ---------------------------------------------------------------------------
# AC1 — no mutating OpenCode call is reachable
# ---------------------------------------------------------------------------
class TestApiGetOnly(Base):
    def test_every_called_argv_is_a_frozen_get_operation(self):
        api = OpencodeApi("/fake/opencode", runner=self.fake)
        api.server_info()
        api.session_list()
        api.session_active()
        api.session_get(SID)
        api.session_message_list(SID, limit=20, order="desc")
        api.session_permission_list(SID)

        self.assertEqual(len(self.fake.calls), 6)
        forbidden_tokens = {
            "-d", "--data", "--header", "-H", "-X", "--request", "--method",
            "POST", "PUT", "DELETE", "PATCH",
            "session.prompt", "experimental.session.wait", "permission.reply",
            "session.delete", "session.fork", "session.import",
        }
        for argv in self.fake.calls:
            self.assertEqual(argv[0], "/fake/opencode")
            self.assertEqual(argv[1], "api")
            operations = [token for token in argv if token in ALLOWED_OPERATIONS]
            self.assertEqual(len(operations), 1, argv)
            self.assertIn(operations[0], ALLOWED_OPERATIONS)
            for token in argv:
                self.assertNotIn(token, forbidden_tokens, argv)
                self.assertFalse(token.startswith("/api/"), argv)

    def test_source_contains_no_mutating_operation_id(self):
        with open(os.path.join(BASE, "monitor", "api.py"), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in (
            "session.prompt", "experimental.session.wait", "permission.reply",
            "session.delete", "session.fork", "session.import", "session.send",
        ):
            self.assertNotIn(forbidden, source)

    def test_no_public_operation_or_flag_parameter(self):
        api = OpencodeApi("/fake/opencode", runner=self.fake)
        self.assertFalse(hasattr(api, "call"))
        self.assertFalse(hasattr(api, "operation"))
        with self.assertRaises(MonitorApiError):
            api.session_message_list(SID, limit=21, order="desc")
        with self.assertRaises(MonitorApiError):
            api.session_message_list(SID, limit=20, order="sideways")
        with self.assertRaises(MonitorApiError):
            api.session_get("-d")  # not a valid session id; cannot smuggle a flag

    def test_malformed_stdout_uses_dedicated_exception(self):
        # Truncated/partial JSON is a distinct, fail-closed signal, still a
        # MonitorApiError so existing UNKNOWN mapping is unchanged.
        self.assertTrue(issubclass(MalformedApiResponse, MonitorApiError))
        self.fake.set(("session.get", SID), "cut mid string")
        api = OpencodeApi("/fake/opencode", runner=self.fake)
        with self.assertRaises(MalformedApiResponse):
            api.session_get(SID)

    def test_server_url_only_adds_a_validated_server_flag(self):
        api = OpencodeApi("/fake/opencode", server_url="http://127.0.0.1:4000", runner=self.fake)
        api.session_active()
        argv = self.fake.calls[0]
        self.assertIn("--server", argv)
        self.assertIn("http://127.0.0.1:4000", argv)
        self.assertNotIn("-d", argv)
        self.assertIn("session.active", argv)

    def test_full_collector_cycle_only_emits_get_operations(self):
        mon, _, _ = self.make_monitor([team(directory="/w/t1")])
        self.fake.set_active([])
        self.fake.set_session_list([SID, SID_B])
        self.fake.set_session(SID, session_info(SID, "/w/t1"))
        self.fake.set_session(SID_B, session_info(SID_B, "/w/other"))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [assistant_msg("msg_1")])
        mon.poll_once()
        self.assertGreater(len(self.fake.calls), 0)
        for argv in self.fake.calls:
            self.assertEqual(argv[0], "/fake/opencode")
            self.assertEqual(argv[1], "api")
            self.assertEqual(len([t for t in argv if t in ALLOWED_OPERATIONS]), 1, argv)
            self.assertNotIn("-d", argv)
            self.assertNotIn("POST", argv)
            self.assertFalse(any(t.startswith("/api/") for t in argv), argv)


# ---------------------------------------------------------------------------
# AC3 — statuses and precedence
# ---------------------------------------------------------------------------
class TestStatusPrecedence(Base):
    def setUp(self):
        super().setUp()
        self.monitor, self.store, self.ready = self.make_monitor([team(sid=SID)])

    def test_first_observation_initialises_baseline_without_ready(self):
        snap = self.scenario(self.monitor, messages=[assistant_msg("msg_1")])
        self.assertEqual(snap.status, IDLE)
        self.assertEqual(self.store.last_seen(SID), "msg_1")
        self.assertFalse(self.ready.has(SID))

    def test_new_answer_raises_ready(self):
        self.scenario(self.monitor, messages=[assistant_msg("msg_1")])
        snap = self.scenario(self.monitor, messages=[assistant_msg("msg_2"), assistant_msg("msg_1")])
        self.assertEqual(snap.status, READY)
        self.assertTrue(snap.awaiting)
        self.assertEqual(self.store.last_seen(SID), "msg_2")
        self.assertTrue(self.ready.has(SID))

    def test_busy_and_outcomes_and_permissions(self):
        self.scenario(self.monitor, messages=[assistant_msg("msg_1")])

        busy = self.scenario(self.monitor, active=[SID], messages=[assistant_msg("msg_2")])
        self.assertEqual(busy.status, BUSY)

        waiting = self.scenario(
            self.monitor, perms=[{"id": "req_1", "type": "bash"}],
            messages=[assistant_msg("msg_2")],
        )
        self.assertEqual(waiting.status, WAITING_PERMISSION)
        self.assertTrue(waiting.awaiting)

        for bad in ("failed", "interrupted"):
            err = self.scenario(self.monitor, outcome=bad, messages=[assistant_msg("msg_2")])
            self.assertEqual(err.status, ERROR)

    def test_precedence_first_match_wins(self):
        self.scenario(self.monitor, messages=[assistant_msg("msg_1")])

        # INC027i: a pending permission is a CURRENT state and outranks the
        # previous turn's failed outcome.
        snap = self.scenario(
            self.monitor, outcome="failed", perms=[{"id": "req_1"}], active=[SID],
            messages=[assistant_msg("msg_2")],
        )
        self.assertEqual(snap.status, WAITING_PERMISSION)

        # drop permission -> BUSY (still a current state, still outranks ERROR)
        snap = self.scenario(
            self.monitor, outcome="failed", active=[SID],
            messages=[assistant_msg("msg_2")],
        )
        self.assertEqual(snap.status, BUSY)

        # drop busy -> ERROR: a terminal failure with no current activity.
        snap = self.scenario(
            self.monitor, outcome="failed", messages=[assistant_msg("msg_2")],
        )
        self.assertEqual(snap.status, ERROR)
        self.assertEqual(snap.reason, "outcome=failed")

        # drop ERROR -> READY (new answer still pending)
        snap = self.scenario(self.monitor, messages=[assistant_msg("msg_2")])
        self.assertEqual(snap.status, READY)

        # acknowledge -> IDLE
        self.assertTrue(handle_ack(self.monitor, "t1"))
        snap = self.scenario(self.monitor, messages=[assistant_msg("msg_2")])
        self.assertEqual(snap.status, IDLE)

    def test_unknown_on_transport_timeout_and_malformed_json(self):
        self.scenario(self.monitor, messages=[assistant_msg("msg_1")])

        self.fake.error(("session.get", SID), OSError("service gone"))
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)
        self.fake.errors.clear()

        self.fake.error(("session.message.list", SID),
                        subprocess.TimeoutExpired(cmd=["opencode"], timeout=1))
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)
        self.fake.errors.clear()

        self.fake.set(("session.get", SID), "not json at all")
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)
        self.fake.errors.clear()

    def test_service_loss_makes_all_teams_unknown(self):
        self.monitor, _, _ = self.make_monitor([team(sid=SID), team("t2", "Team Two", "/w/t2", SID_B)])
        self.fake.error("session.active", OSError("down"))
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)
        self.assertEqual(self.monitor.snapshot("t2").status, UNKNOWN)

    def test_missing_or_ambiguous_data_is_unknown(self):
        self.fake.set_active({})
        self.fake.set_session(SID, session_info(SID))
        self.fake.set(("session.permission.list", SID), {"unexpected": True})  # not a list
        self.fake.set_messages(SID, [])
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)

    def test_missing_or_unrecognized_outcome_is_unknown(self):
        self.fake.set_active([])
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [])

        missing = session_info(SID)
        del missing["outcome"]
        self.fake.set_session(SID, missing)
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)

        self.fake.set_session(SID, session_info(SID, outcome="still-working-on-it"))
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, UNKNOWN)

        # A recognised successful outcome is not an error/ambiguity.
        self.fake.set_session(SID, session_info(SID, outcome="succeeded"))
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot("t1").status, IDLE)

    def test_stale_snapshot_is_unknown(self):
        self.scenario(self.monitor, messages=[assistant_msg("msg_1")])
        snap = self.monitor.snapshot("t1")
        self.assertEqual(effective_status(snap, 15), IDLE)
        stale_now = snap.updated_at + 100
        self.assertEqual(effective_status(snap, 15, now=stale_now), UNKNOWN)


# ---------------------------------------------------------------------------
# AC8 — concurrent sessions and per-team failure isolation
# ---------------------------------------------------------------------------
class TestConcurrencyAndIsolation(Base):
    def test_multiple_concurrent_sessions_for_one_team(self):
        mon, _, _ = self.make_monitor([team(directory="/w/team")])
        self.fake.set_active([])
        self.fake.set_session_list([SID, SID_B])
        self.fake.set_session(SID, session_info(SID, directory="/w/team"))
        self.fake.set_session(SID_B, session_info(SID_B, directory="/w/team"))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [])
        self.fake.set_permissions(SID_B, [{"id": "req_1"}])
        self.fake.set_messages(SID_B, [])
        mon.poll_once()

        snap = mon.snapshot("t1")
        self.assertEqual(set(snap.session_ids), {SID, SID_B})
        self.assertEqual(snap.status, WAITING_PERMISSION)
        self.assertEqual(mon.await_count(15), 1)

    def test_non_matching_worktree_is_excluded(self):
        mon, _, _ = self.make_monitor([team(directory="/w/team")])
        self.fake.set_active([])
        self.fake.set_session_list([SID, SID_B])
        self.fake.set_session(SID, session_info(SID, directory="/w/team"))
        self.fake.set_session(SID_B, session_info(SID_B, directory="/w/other"))
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").session_ids, (SID,))
        self.assertEqual(mon.snapshot("t1").status, IDLE)

    def test_one_team_failure_does_not_crash_the_collector(self):
        mon, _, _ = self.make_monitor([
            team(sid=SID), team("t2", "Team Two", "/w/t2", SID_B),
        ])
        self.fake.set_active([])
        self.fake.set_session(SID, session_info(SID, "/w/t1"))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [assistant_msg("msg_1")])
        # first observation -> baseline
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, IDLE)

        # Now t1 fails hard, t2 raises a fresh READY.
        self.fake.error(("session.get", SID), OSError("t1 down"))
        self.fake.set_session(SID_B, session_info(SID_B, "/w/t2"))
        self.fake.set_permissions(SID_B, [])
        self.fake.set_messages(SID_B, [assistant_msg("msg_1")])
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, UNKNOWN)
        self.assertEqual(mon.snapshot("t2").status, READY)
        self.assertEqual(mon.await_count(15), 1)

    def test_unknown_session_is_isolated_to_its_own_card(self):
        """INC027d AC-d2: one session's UNKNOWN must not disable its sibling.

        Supersedes the INC027 'any UNKNOWN -> whole team UNKNOWN' collapse: each
        primary is now its own stacked card with an independent status.
        """
        mon, _, _ = self.make_monitor([team(directory="/w/team")])
        self.fake.set_active([])
        self.fake.set_session_list([SID, SID_B])
        self.fake.set_session(SID, session_info(SID, "/w/team"))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [assistant_msg("msg_1")])
        self.fake.set_session(SID_B, session_info(SID_B, "/w/team"))
        self.fake.set_permissions(SID_B, [])
        self.fake.error(("session.message.list", SID_B), OSError("partial loss"))
        mon.poll_once()
        snap = mon.snapshot("t1")
        by_id = {s.session_id: s for s in snap.sessions}
        self.assertEqual(len(snap.sessions), 2)          # both remain separate cards
        self.assertEqual(by_id[SID].status, IDLE)        # healthy sibling NOT disabled
        self.assertEqual(by_id[SID_B].status, UNKNOWN)   # failure isolated to its card
        self.assertEqual(snap.status, IDLE)              # no team-wide UNKNOWN collapse


# ---------------------------------------------------------------------------
# AC4 — restart / dedup / busy guard
# ---------------------------------------------------------------------------
class TestDedupAndRestart(Base):
    def test_restart_does_not_raise_duplicate_ready(self):
        mon, _, _ = self.make_monitor([team(sid=SID)])
        self.scenario(mon, messages=[assistant_msg("msg_1")])
        snap = self.scenario(mon, messages=[assistant_msg("msg_2"), assistant_msg("msg_1")])
        self.assertEqual(snap.status, READY)

        calls_before = len(self.fake.calls)
        # Fresh process: new state store from the same file, empty ready_pending.
        restarted = StateStore(self.state_path)
        ready2 = ReadyPending()
        api2 = OpencodeApi("/fake/opencode", runner=self.fake)
        mon2 = Monitor(mon.config, api2, restarted, ready2)
        self.fake.set_active([])
        self.fake.set_session(SID, session_info(SID))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [assistant_msg("msg_2"), assistant_msg("msg_1")])
        mon2.poll_once()
        self.assertEqual(mon2.snapshot("t1").status, IDLE)
        self.assertFalse(ready2.has(SID))
        self.assertEqual(restarted.last_seen(SID), "msg_2")
        self.assertGreater(len(self.fake.calls), calls_before)  # reads happened

    def test_busy_partial_does_not_update_baseline(self):
        mon, store, _ = self.make_monitor([team(sid=SID)])
        self.scenario(mon, messages=[assistant_msg("msg_1")])
        self.scenario(mon, messages=[assistant_msg("msg_2")])  # READY, baseline msg_2
        self.assertEqual(store.last_seen(SID), "msg_2")
        handle_ack(mon, "t1")

        # A new (streaming) message while busy must not advance the baseline.
        busy = self.scenario(mon, active=[SID], messages=[assistant_msg("msg_3")])
        self.assertEqual(busy.status, BUSY)
        self.assertEqual(store.last_seen(SID), "msg_2")

        # Once idle, the same completed message raises READY exactly once.
        idle = self.scenario(mon, messages=[assistant_msg("msg_3")])
        self.assertEqual(idle.status, READY)
        self.assertEqual(store.last_seen(SID), "msg_3")
        again = self.scenario(mon, messages=[assistant_msg("msg_3")])
        self.assertEqual(again.status, READY)  # pending until Gezien (not a duplicate raise)

    def test_corrupt_state_cannot_duplicate_ready(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")
        mon, store, ready = self.make_monitor([team(sid=SID)])
        self.assertIsNone(store.last_seen(SID))
        snap = self.scenario(mon, messages=[assistant_msg("msg_1")])
        self.assertEqual(snap.status, IDLE)
        self.assertFalse(ready.has(SID))


# ---------------------------------------------------------------------------
# AC6 — redaction and caps
# ---------------------------------------------------------------------------
class TestRedactionAndCaps(Base):
    def test_secret_redaction(self):
        sample = (
            'password: "EXAMPLEPASSWORD" api_key=EXAMPLEKEY token: EXAMPLETOKEN '
            'Authorization: Bearer sk-test-EXAMPLE000000001 '
            "ghp_" + "EXAMPLEGHTOKEN000000" + " "
            "-----BEGIN EXAMPLE PRIVATE KEY-----\nEXAMPLEPEMBODY\n"
            "-----END EXAMPLE PRIVATE KEY-----"
        )
        safe = redact(sample)
        for secret in ("EXAMPLEPASSWORD", "EXAMPLEKEY", "EXAMPLETOKEN",
                       "sk-test-EXAMPLE", "EXAMPLEGHTOKEN000000", "EXAMPLEPEMBODY"):
            self.assertNotIn(secret, safe)
        self.assertIn("[REDACTED", safe)

    def test_compound_secret_keys_are_redacted(self):
        sample = (
            "access_token=ACCESSSECRET123 secret_key='SECRETKEY456' "
            "auth_token: AUTH789 db_access_token=DBTOKEN000 "
            "api_key=APKEY111 api-key=APIKEY222"
        )
        safe = redact(sample)
        for secret in (
            "ACCESSSECRET123", "SECRETKEY456", "AUTH789",
            "DBTOKEN000", "APKEY111", "APIKEY222",
        ):
            self.assertNotIn(secret, safe)
        for key in (
            "access_token", "secret_key", "auth_token", "db_access_token", "api_key",
        ):
            self.assertIn(key, safe)
        self.assertIn("[REDACTED", safe)

    def test_caps(self):
        long_text = "x" * (MAX_CHARS_PER_MESSAGE + 500)
        messages = [assistant_msg("msg_%d" % i, long_text) for i in range(MAX_MESSAGES + 10)]
        result = summarize_messages(messages)
        self.assertLessEqual(result["count"], MAX_MESSAGES)
        self.assertLessEqual(result["total_chars"], MAX_TOTAL_CHARS)
        for text in result["texts"]:
            self.assertLessEqual(len(text), MAX_CHARS_PER_MESSAGE)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(cap_text("abcdef", 3)), 3)

    def test_dashboard_escapes_untrusted_text(self):
        mon, _, _ = self.make_monitor([team(sid=SID)])
        self.scenario(mon, messages=[assistant_msg("msg_1", text="<script>alert(1)</script>")])
        page = render_dashboard(mon, 15, 5)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


# ---------------------------------------------------------------------------
# AC5 / AC7 / config — web, ack, bind, config strictness
# ---------------------------------------------------------------------------
class TestWebAndConfig(Base):
    def test_dashboard_contains_required_elements(self):
        mon, _, _ = self.make_monitor([
            team(sid=SID), team("t2", "Team Two", "/w/t2", SID_B),
        ])
        self.fake.set_active([])
        self.fake.set_session(SID, session_info(SID, "/w/t1", title="ALPHA answer"))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [assistant_msg("msg_1", text="hello from ALPHA")])
        self.fake.set_session(SID_B, session_info(SID_B, "/w/t2"))
        self.fake.error(("session.get", SID_B), OSError("no service"))
        mon.poll_once()  # t1 first observation -> baseline (IDLE)
        self.fake.set_messages(SID, [
            assistant_msg("msg_2", text="hello from ALPHA"),
            assistant_msg("msg_1", text="older ALPHA answer"),
        ])
        mon.poll_once()  # t1 -> READY; t2 stays UNKNOWN

        page = render_dashboard(mon, 15, 5)
        self.assertIn("Team One", page)
        self.assertIn("Team Two", page)
        self.assertIn('http-equiv="refresh"', page)
        self.assertIn('method="post"', page)
        self.assertIn('action="/ack"', page)
        self.assertIn("Connection lost", page)
        self.assertIn("Awaiting operator", page)
        payload = status_json(mon, 15)
        self.assertEqual(payload["teams"][0]["status"], READY)
        self.assertEqual(payload["teams"][1]["status"], UNKNOWN)

    def test_gezien_button_only_for_ready_cards(self):
        mon, _, _ = self.make_monitor([
            team(sid=SID), team("t2", "Team Two", "/w/t2", SID_B),
        ])
        self.fake.set_active([])
        self.fake.set_session(SID, session_info(SID, "/w/t1"))
        self.fake.set_permissions(SID, [])
        self.fake.set_messages(SID, [assistant_msg("msg_1")])
        self.fake.set_session(SID_B, session_info(SID_B, "/w/t2"))
        self.fake.set_permissions(SID_B, [{"id": "req_1"}])
        self.fake.set_messages(SID_B, [])
        mon.poll_once()  # t1 baseline (IDLE)
        self.fake.set_messages(SID, [assistant_msg("msg_2"), assistant_msg("msg_1")])
        mon.poll_once()  # t1 -> READY, t2 -> WAITING_PERMISSION

        self.assertEqual(mon.snapshot("t1").status, READY)
        self.assertEqual(mon.snapshot("t2").status, WAITING_PERMISSION)
        page = render_dashboard(mon, 15, 5)
        self.assertEqual(page.count('action="/ack"'), 1)
        self.assertIn("Gezien", page)
        self.assertIn("Gezien does not apply", page)
        # Both still count as awaiting operator (AC7) even though only READY
        # offers a local acknowledgement.
        self.assertEqual(status_json(mon, 15)["awaiting_count"], 2)

    def test_gezien_never_calls_opencode(self):
        mon, _, ready = self.make_monitor([team(sid=SID)])
        self.scenario(mon, messages=[assistant_msg("msg_1")])
        self.scenario(mon, messages=[assistant_msg("msg_2")])
        self.assertEqual(self.monitor_status(mon), READY)

        before = list(self.fake.calls)
        self.assertTrue(handle_ack(mon, "t1"))
        self.assertEqual(self.fake.calls, before)  # no new CLI call at all
        self.assertFalse(ready.has(SID))
        self.assertEqual(self.monitor_status(mon), IDLE)
        self.assertFalse(handle_ack(mon, "does-not-exist"))

    @staticmethod
    def monitor_status(mon):
        return mon.snapshot("t1").status

    def test_loopback_bind_enforcement(self):
        # Real loopback literals are accepted by the shared resolution check.
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("localhost"))
        self.assertTrue(is_loopback("::1"))
        self.assertFalse(is_loopback("0.0.0.0"))
        self.assertFalse(is_loopback("::"))
        mon, _, _ = self.make_monitor([team(sid=SID)])
        with self.assertRaises(RefusedBind):
            serve(mon, "0.0.0.0", 8765, allow_external=False)

    def test_loopback_check_uses_resolution_not_string_prefix(self):
        """A host that merely looks loopback must be refused (offline stub)."""

        mon, _, _ = self.make_monitor([team(sid=SID)])
        with patch("monitor.web.socket.getaddrinfo", side_effect=stub_getaddrinfo):
            self.assertTrue(is_loopback("127.0.0.1"))
            self.assertTrue(is_loopback("::1"))
            self.assertTrue(is_loopback("localhost"))
            self.assertFalse(is_loopback("127.0.0.1.evil.com"))
            self.assertFalse(is_loopback("mixed.example"))
            self.assertFalse(is_loopback("no-such-host.invalid"))
            # Both startup validation and serve()'s guard share this check.
            with self.assertRaises(RefusedBind):
                serve(mon, "127.0.0.1.evil.com", 8765, allow_external=False)
            with self.assertRaises(RefusedBind):
                serve(mon, "mixed.example", 8765, allow_external=False)

    def test_startup_validation_uses_resolution_check(self):
        """main() refuses a look-alike host before starting any thread/socket."""
        from monitor.__main__ import main

        self.make_monitor([team(sid=SID)])  # writes self.config_path
        with patch("monitor.web.socket.getaddrinfo", side_effect=stub_getaddrinfo), \
                patch("builtins.print"):
            code = main(["--config", self.config_path, "--bind", "127.0.0.1.evil.com"])
        self.assertEqual(code, 2)

    def test_config_rejects_injected_operation_and_bad_values(self):
        base = {
            "schema": 1, "opencode_bin": "/fake/opencode",
            "teams": [team(sid=SID)],
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(base, handle)
        cfg = load_config(self.config_path)
        self.assertEqual(cfg.poll_interval, 3.0)
        self.assertEqual(cfg.stale_threshold, 15.0)

        base_bad = dict(base)
        base_bad["operation"] = "session.prompt"  # must be rejected
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(base_bad, handle)
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

        for bad_url in ("http://user:pw@127.0.0.1:4000", "ftp://x:1", "http://127.0.0.1:port"):
            cfg_bad = dict(base)
            cfg_bad["server_url"] = bad_url
            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump(cfg_bad, handle)
            with self.assertRaises(ConfigError):
                load_config(self.config_path)

    def test_config_template_has_no_environment_facts(self):
        with open(os.path.join(BASE, "config", "teams.example.json"), encoding="utf-8") as handle:
            template = handle.read()
        self.assertNotIn("/home/opencode", template)
        self.assertNotIn("/opt/", template)
        self.assertIn("/home/you/projects/team-alpha", template)
        self.assertIn("/home/you/.local/bin/opencode", template)
        # Every absolute path shipped in the template is a documented generic
        # placeholder, never a machine-specific location.
        for path in re.findall(r'"(/[^"]*)"', template):
            self.assertTrue(path.startswith(("/home/you/", "/mnt/c/")), path)


# ---------------------------------------------------------------------------
# INC027 §12 AC12.1-AC12.7 — oversized / truncated message.list corrective
# ---------------------------------------------------------------------------
class TestTruncationCorrective(Base):
    def _setup(self):
        self.fake = LimitAwareCli()
        mon, store, ready = self.make_monitor([team(sid=SID)])
        self.fake.set_active([])
        self.fake.set_session(SID, session_info(SID))
        self.fake.set_permissions(SID, [])
        return mon, store, ready

    @staticmethod
    def _message_calls(fake):
        return [c for c in fake.calls if "session.message.list" in c]

    def test_truncated_first_page_retries_and_finds_newest_id(self):
        mon, store, ready = self._setup()
        truncated = truncated_json_stdout(big_history(), cut_char=257600)
        self.assertGreaterEqual(len(truncated), 257000)  # ~258 KB, as in §11
        self.fake.set_limit(SID, MESSAGE_LIMIT, truncated)
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT,
                            {"data": [assistant_msg("msg_new")]})

        mon.poll_once()  # first eligible observation: silent baseline
        snap = mon.snapshot("t1")
        self.assertEqual(snap.status, IDLE)
        self.assertEqual(store.last_seen(SID), "msg_new")
        self.assertFalse(ready.has(SID))

        calls = self._message_calls(self.fake)
        self.assertEqual(len(calls), 2)  # bounded: one retry only
        self.assertIn("limit=%d" % MESSAGE_LIMIT, calls[0])
        self.assertIn("limit=%d" % MESSAGE_FALLBACK_LIMIT, calls[1])
        for call in calls:
            self.assertEqual(call[1], "api")
            self.assertIn("session.message.list", call)
            self.assertIn("order=desc", call)
            self.assertFalse(any(t in ("POST", "PUT", "DELETE", "PATCH", "-d", "--data")
                                 for t in call))

        # A genuinely newer answer on the fallback page raises READY once.
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT, {
            "data": [assistant_msg("msg_newer"), assistant_msg("msg_new")],
        })
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, READY)
        self.assertEqual(store.last_seen(SID), "msg_newer")
        self.assertTrue(ready.has(SID))

    def test_both_pages_truncated_fails_closed_unknown(self):
        mon, store, ready = self._setup()
        truncated = truncated_json_stdout(big_history(), cut_char=257600)
        self.fake.set_limit(SID, MESSAGE_LIMIT, truncated)
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT, truncated)
        store.record(SID, "msg_old")  # a false READY would be detectable

        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertEqual(snap.status, UNKNOWN)
        self.assertIn("malformed", snap.reason.lower())
        self.assertEqual(store.last_seen(SID), "msg_old")  # baseline untouched
        self.assertFalse(ready.has(SID))
        self.assertEqual(len(self._message_calls(self.fake)), 2)  # bounded

    def test_truncated_poll_does_not_consume_a_ready(self):
        mon, store, ready = self._setup()
        valid = {"data": [assistant_msg("msg_1")]}
        self.fake.set_limit(SID, MESSAGE_LIMIT, valid)
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT, valid)
        mon.poll_once()  # baseline msg_1, IDLE
        self.assertEqual(mon.snapshot("t1").status, IDLE)

        truncated = truncated_json_stdout(big_history(), cut_char=257600)
        self.fake.set_limit(SID, MESSAGE_LIMIT, truncated)
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT, truncated)
        mon.poll_once()  # truncated -> UNKNOWN, no state consumption
        self.assertEqual(mon.snapshot("t1").status, UNKNOWN)
        self.assertEqual(store.last_seen(SID), "msg_1")
        self.assertFalse(ready.has(SID))

        # Recovery with a valid page raises exactly one READY for the new id.
        self.fake.set_limit(SID, MESSAGE_LIMIT, {
            "data": [assistant_msg("msg_2"), assistant_msg("msg_1")],
        })
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, READY)
        self.assertEqual(store.last_seen(SID), "msg_2")

    def test_missing_assistant_never_raises_ready(self):
        mon, store, ready = self._setup()
        user_only = {"role": "user", "id": "usr_1",
                     "content": [{"type": "text", "text": "hi"}]}
        self.fake.set_limit(SID, MESSAGE_LIMIT, {"data": [user_only]})
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT, {"data": [user_only]})
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, IDLE)
        self.assertEqual(store.last_seen(SID), "")
        self.assertFalse(ready.has(SID))

    def test_busy_guard_holds_across_retry(self):
        mon, store, ready = self._setup()
        valid = {"data": [assistant_msg("msg_1")]}
        self.fake.set_limit(SID, MESSAGE_LIMIT, valid)
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT, valid)
        mon.poll_once()  # baseline msg_1

        self.fake.set_active([SID])
        truncated = truncated_json_stdout(big_history(), cut_char=257600)
        self.fake.set_limit(SID, MESSAGE_LIMIT, truncated)
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT,
                            {"data": [assistant_msg("msg_2")]})
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, BUSY)
        self.assertEqual(store.last_seen(SID), "msg_1")  # streaming not counted
        self.assertFalse(ready.has(SID))

    def test_response_sizes_and_partial_history(self):
        mon, store, ready = self._setup()
        # Small valid page, newest assistant first (order=desc).
        self.fake.set_limit(SID, MESSAGE_LIMIT,
                            {"data": [assistant_msg("msg_a")]})
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT,
                            {"data": [assistant_msg("msg_a")]})
        mon.poll_once()
        self.assertEqual(store.last_seen(SID), "msg_a")
        # Large-but-valid page with several messages: newest id still found.
        large = [assistant_msg("msg_z", text="q" * 50000)] + [
            assistant_msg("msg_%d" % i) for i in range(MESSAGE_LIMIT - 1)
        ]
        self.fake.set_limit(SID, MESSAGE_LIMIT, {"data": large})
        mon.poll_once()
        self.assertEqual(mon.snapshot("t1").status, READY)
        self.assertEqual(store.last_seen(SID), "msg_z")

    def test_generic_monitor_error_does_not_trigger_limit_one_retry(self):
        """A non-malformed auth/transport error must NOT trigger the truncation retry.

        Mutant C broadens ``except MalformedApiResponse`` to
        ``except MonitorApiError`` in collector._fetch_message_data. Under that
        mutant the wrapped transport error below is caught, the fallback page is
        fetched and its valid data recovers the team to IDLE. The correct code
        lets the plain MonitorApiError propagate to the per-session UNKNOWN path.
        This test must FAIL under the mutant (status, call count and limit all
        change) and PASS on the current implementation.
        """
        mon, store, ready = self._setup()
        # First (limit=MESSAGE_LIMIT) call raises a wrapped transport error.
        # api._call converts OSError -> plain MonitorApiError, NOT MalformedApiResponse.
        self.fake.error_limit(SID, MESSAGE_LIMIT, OSError("auth/transport gone"))
        # The fallback page WOULD return valid data if the retry were (wrongly)
        # attempted; this makes the status assertion non-vacuous.
        self.fake.set_limit(SID, MESSAGE_FALLBACK_LIMIT,
                            {"data": [assistant_msg("msg_fallback")]})

        mon.poll_once()
        snap = mon.snapshot("t1")
        self.assertEqual(snap.status, UNKNOWN)

        calls = self._message_calls(self.fake)
        self.assertEqual(len(calls), 1)  # exactly one message.list call, no retry
        self.assertIn("limit=%d" % MESSAGE_LIMIT, calls[0])
        self.assertNotIn("limit=%d" % MESSAGE_FALLBACK_LIMIT, calls[0])


if __name__ == "__main__":
    unittest.main()
