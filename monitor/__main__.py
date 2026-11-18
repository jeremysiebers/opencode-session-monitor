"""Entrypoint: ``python3 -m monitor`` (run from the monitor directory)."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from .api import OpencodeApi
from .collector import Monitor, MultiSourceMonitor, SnapshotRegistry
from .config import ConfigError, load_config
from .metrics import JsonlMetricsWriter, MetricsRecorder
from .state import ReadyPending, StateStore
from .version import __version__
from .web import RefusedBind, is_loopback, serve

# The monitor directory (parent of this package). Runtime state lives only here.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE_PATH = os.path.join(BASE_DIR, ".runtime", "last_seen.json")
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config", "teams.json")
DEFAULT_TIMEOUT = 15.0


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only local OpenCode Session Monitor")
    parser.add_argument(
        "--version",
        action="version",
        version="OpenCode Session Monitor %s" % __version__,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="user config JSON path")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="monitor-local state file path")
    parser.add_argument("--bind", default=None, help="bind host (default 127.0.0.1 or config)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8765 or config)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-CLI-call timeout (s)")
    parser.add_argument(
        "--metrics-jsonl",
        default=None,
        metavar="PATH",
        help="opt-in: append one metrics JSON line per source per completed cycle "
             "(fixed 4 MiB cap; default off)",
    )
    parser.add_argument(
        "--allow-external-bind",
        action="store_true",
        help="explicit override permitting a non-loopback bind (use with care)",
    )
    return parser


def _poll_loop(monitor, interval, writer=None):
    while True:
        try:
            monitor.poll_once()
        except Exception:  # noqa: BLE001 - keep the collector thread alive
            pass
        # JSONL is written strictly AFTER end_cycle (inside poll_once), so file
        # I/O can never inflate the measured cycle wall time.
        if writer is not None:
            try:
                snapshot = monitor.metrics_snapshot()
                if snapshot is not None:
                    writer.write(monitor.source_key, snapshot)
            except Exception:  # noqa: BLE001 - metrics must never kill the poll thread
                pass
        time.sleep(interval)


def build_source_monitors(config, store, ready, registry, timeout, runners=None,
                          recorders=None):
    """Create one independent GET-only Monitor per configured source.

    All sources share the SAME StateStore, ReadyPending and SnapshotRegistry;
    only the binary/server URL/teams/state-key differ. A native Windows
    ``opencode.exe`` is invoked through the very same ``OpencodeApi`` with
    ``shell=False`` — no shell wrapper of any kind and no new operation.

    ``runners`` may inject a per-source test double keyed by source key; when
    absent, the real subprocess runner is used (the production path).
    ``recorders`` may inject a per-source :class:`MetricsRecorder`; otherwise one
    bounded, in-memory recorder is created per source (JSONL stays opt-in).
    """
    runners = runners or {}
    recorders = recorders or {}
    monitors = []
    for source in config.active_sources():
        recorder = recorders.get(source.key)
        if recorder is None:
            recorder = MetricsRecorder(
                source_key=source.key,
                poll_interval=getattr(config, "poll_interval", 3.0),
                stale_threshold=getattr(config, "stale_threshold", 15.0),
            )
        api = OpencodeApi(
            source.opencode_bin, server_url=source.server_url, timeout=timeout,
            runner=runners.get(source.key), metrics=recorder,
        )
        monitors.append(
            Monitor(source, api, store, ready, registry=registry, metrics=recorder)
        )
    return monitors


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("CONFIG ERROR: %s" % exc, file=sys.stderr)
        return 2

    bind = args.bind if args.bind is not None else config.bind
    port = args.port if args.port is not None else config.port
    # Same resolution-based loopback check used by web.serve()'s guard.
    if not is_loopback(bind) and not args.allow_external_bind:
        print(
            "REFUSED: non-loopback bind %r (pass --allow-external-bind to override)" % bind,
            file=sys.stderr,
        )
        return 2

    store = StateStore(args.state)
    ready = ReadyPending()
    registry = SnapshotRegistry()
    source_monitors = build_source_monitors(config, store, ready, registry, args.timeout)
    hub = MultiSourceMonitor(source_monitors, store, ready, registry)
    # JSONL metrics are strictly opt-in (default None) and bounded to 4 MiB.
    metrics_writer = (
        JsonlMetricsWriter(args.metrics_jsonl) if args.metrics_jsonl else None
    )

    # One independent poll thread per source; the wall time of a cycle is the
    # maximum, not the sum, of the per-source cycles. A failing/slow source is
    # contained in its own thread and marks only its own cards UNKNOWN.
    for index, source_monitor in enumerate(source_monitors):
        worker = threading.Thread(
            target=_poll_loop, args=(source_monitor, config.poll_interval, metrics_writer),
            name="monitor-poll-%s-%d" % (source_monitor.source_key, index), daemon=True,
        )
        worker.start()

    try:
        serve(
            hub,
            bind,
            port,
            allow_external=args.allow_external_bind,
            stale_threshold=config.stale_threshold,
            refresh_seconds=int(max(3, config.poll_interval + 1)),
        )
    except RefusedBind as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("SERVER ERROR: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
