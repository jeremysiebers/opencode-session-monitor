# Installation

The monitor is a single Python package plus a small shell launcher. There is
nothing to compile and no third-party dependency.

## Prerequisites

- Python **3.10+** (`python3 --version`). The code uses standard-library only:
  `http.server`, `subprocess`, `json`, `dataclasses`, `concurrent.futures`.
- The **OpenCode CLI**, installed and logged in as the user that will run the
  monitor, with the `opencode api` subcommand available.
- A POSIX shell for `./ocmon` (Linux, WSL2, macOS-like environments).

Find the CLI path first; both the example config and every source entry need an
absolute path:

```sh
command -v opencode
# /home/you/.local/bin/opencode
```

## Linux

```sh
git clone <your-repository-url> opencode-session-monitor
cd opencode-session-monitor

cp config/teams.example.json config/teams.json
$EDITOR config/teams.json          # opencode_bin + worktree_dir entries

./ocmon                            # serves http://127.0.0.1:8765/
```

No root, no system service, no port below 1024. The process runs as you, reads
only its config, invokes the CLI as you and writes only:

- `config/teams.json` (your file, never written by the monitor), and
- `.runtime/last_seen.json` (created automatically; override with `--state`).

### Optional virtual environment

There are no dependencies, but a venv keeps the interpreter reproducible:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m monitor --config config/teams.json
```

`.venv/` is git-ignored.

## WSL2

Inside WSL2 the setup is identical to Linux. Two extra notes:

1. Run the monitor where the **Linux** CLI lives (`/home/you/.local/bin/opencode`)
   when your sessions are Linux/WSL2 sessions.
2. If you also run OpenCode natively on Windows, keep the second source in the
   example config and set its `opencode_bin` to the interop path of the Windows
   executable, for example
   `/mnt/c/Users/you/AppData/Roaming/npm/node_modules/@opencode/cli/bin/opencode.exe`.
   Delete that whole `sources[]` object if you do not need it; nothing else
   depends on it.

The monitor reaches the Windows executable through WSL2 interop. It does not
start a Windows service, open a second listener or install anything on Windows.

## Permissions and network exposure

- Bind address defaults to `127.0.0.1`; the HTTP server refuses a non-loopback
  bind unless `--allow-external-bind` is passed. Keep it on loopback.
- The state directory must be writable by the user running the monitor; the
  parent directory of `--state` is created if missing.
- No credentials are read or stored. The CLI handles its own authentication.
- Nothing needs `sudo`, a Docker socket or a privileged port.

## Optional: run as a systemd --user service

Create `~/.config/systemd/user/opencode-session-monitor.service`:

```ini
[Unit]
Description=OpenCode Session Monitor
After=default.target

[Service]
Type=simple
ExecStart=%h/opencode-session-monitor/ocmon --config %h/opencode-session-monitor/config/teams.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now opencode-session-monitor.service
journalctl --user -u opencode-session-monitor -f
```

This is your own user service; enabling it is optional and outside the
monitor's own runtime behaviour. Stop it with
`systemctl --user disable --now opencode-session-monitor.service`.

## Verification

```sh
./ocmon &                                   # or the systemd unit
curl -fsS http://127.0.0.1:8765/status.json | python3 -m json.tool | head
curl -fsS http://127.0.0.1:8765/ >/dev/null && echo "dashboard OK"
python3 -m unittest discover -s tests -v    # 204 tests, fully offline
```

In `status.json`, check that `teams[].source_key` matches your configured
sources and that each team has the number of `sessions` you expect.

## Troubleshooting

**`CONFIG ERROR: ... opencode_bin must be an absolute path`**
The config requires an absolute path. Use `/home/you/.local/bin/opencode`, not
`opencode` or `~/.local/bin/opencode`.

**CLI not found / `REFUSED` / every card UNKNOWN**
Run the binary by hand once as the same user:
`/home/you/.local/bin/opencode api server.info`. If that fails, the CLI is not
installed for this user or its own service is not running; fix that first. The
monitor treats a CLI failure as UNKNOWN and never invents a status.

**No sessions discovered**
Discovery matches `worktree_dir` against the session's reported directory with
a lexical comparison: same separators, same drive-letter case folding, trailing
slashes and `.`/`..` ignored, but **no symlink resolution**. Print the reported
directory with `opencode api session.list --param limit=5` and copy that exact
string. A team with zero matching sessions is legitimate; it stays empty rather
than showing a fake card.

**Port already in use**
`SERVER ERROR: [Errno 98] Address already in use` (exit code 1) means another
process owns the port. Either stop it or run with a different `--port`. Do not
run two monitors against the same state file.

**Only the Windows team is missing**
That source is optional. Check that the `/mnt/c/...` path exists from WSL2, that
WSL2 interop is enabled for your distribution, and that the Windows CLI is
logged in for the Windows user. Failure of that source is isolated; the Linux
teams keep working.

**State file problems**
`--state` selects the file (default `.runtime/last_seen.json`). An unreadable or
foreign file is ignored and rebuilt; a schema-1 file is migrated automatically.
If the monitor restarts, an already acknowledged answer is not re-raised, but a
new answer that arrived while it was down is shown as READY on the next poll.
Deleting the file is safe: it only resets which answers you have already seen.

**Dashboard shows UNKNOWN for everything after a while**
That is the stale guard: no successful update within
`stale_threshold_seconds`. Look at the red banner; the CLI may be slow or
failing. Do not silence it by raising the threshold blindly.
