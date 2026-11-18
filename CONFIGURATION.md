# Configuration

Configuration is one JSON file, passed with `--config` (default
`config/teams.json`). It is validated strictly: unknown keys are rejected, so a
config file cannot inject an operation, method, body, command or extra flag.

Copy `config/teams.example.json` to `config/teams.json` and edit it. The example
is accepted by the real loader as-is; every value in it is an example, not a
detected fact.

Keys starting with `_` are ignored everywhere and exist only for comments.

## Schema 1 and schema 2

Both are accepted; the loader reports the schema it read.

| | Schema 1 | Schema 2 |
| --- | --- | --- |
| Top-level `opencode_bin` | required | required |
| Top-level `teams` | required, non-empty | required, non-empty (back-compat view) |
| `sources[]` | not available | optional; when non-empty it **replaces** the single implicit source |
| Multi-installation | no | yes, one entry per installation |
| `deny_session_ids` | optional | optional, per file and per source |

A schema-2 file with an empty `sources[]` behaves exactly like schema 1: one
implicit source built from the top-level fields, namespaced by the top-level
`source_key` (default `"default"`).

## Top-level keys

```json
{
  "schema": 2,
  "opencode_bin": "/home/you/.local/bin/opencode",
  "server_url": "",
  "poll_interval_seconds": 3,
  "stale_threshold_seconds": 15,
  "bind": "127.0.0.1",
  "port": 8765,
  "deny_session_ids": [],
  "source_key": "default",
  "sources": [],
  "teams": []
}
```

- `opencode_bin` — absolute path to the CLI. Relative paths, empty values and
  values containing NUL are rejected.
- `server_url` — optional. Empty string means "let the CLI use its own
  background service". A URL must be `http(s)`, have a host and a numeric port
  (1..65535), and must not embed credentials.
- `poll_interval_seconds` — delay between polls (default 3). One poll per
  source runs at a time; the next poll starts after the previous one finished.
- `stale_threshold_seconds` — a card with no successful update for longer than
  this is displayed as `UNKNOWN` (default 15).
- `bind` / `port` — dashboard listener; defaults `127.0.0.1` / `8765`.
  A non-loopback bind is refused unless `--allow-external-bind` is passed.
- `deny_session_ids` — exact `ses_*` ids only, never titles or substrings.
- `source_key` — state namespace for the single implicit source (default
  `"default"`); each `sources[]` entry uses its own `key` instead.

## Sources

```json
"sources": [
  {
    "key": "wsl2",
    "opencode_bin": "/home/you/.local/bin/opencode",
    "server_url": "",
    "deny_session_ids": [],
    "teams": [ ... ]
  }
]
```

- `key` — short identifier, unique; doubles as the state namespace and the
  `source` badge on cards.
- `opencode_bin` — absolute path for **this** installation. A native Windows
  executable is addressed by its `/mnt/c/...` interop path and invoked directly
  with `shell=False`; no shell wrapper, no new service, no new port.
- `deny_session_ids`, `teams` — per source.
- Team keys must be unique across all sources.

Each source gets its own poll thread, its own API wrapper and its own metrics
recorder. A failing or overflowing source marks only its own cards `UNKNOWN`;
other sources and sibling teams are unaffected.

## Teams and worktrees

```json
{
  "key": "team-alpha",
  "label": "Team Alpha",
  "worktree_dir": "/home/you/projects/team-alpha",
  "session_id": null,
  "child_session_ids": []
}
```

- `key` — short identifier, unique within its source (and across sources).
- `label` — the human name shown on the dashboard.
- `worktree_dir` — absolute path, matched **exactly** against the directory the
  API reports for a session.
- `session_id` — `null`/absent for discovery, or an exact `ses_*` id to pin one
  session (migration/compat anchor). Pinned sessions skip discovery for that
  team.
- `child_session_ids` — optional exact ids of known advisory child sessions.
  Children never become cards; they may only mark an otherwise-IDLE team BUSY.

### Exact matching

Paths are compared by a pure-string normalizer, never by touching the
filesystem: backslashes and forward slashes are unified, a drive letter is
case-folded, duplicate separators are collapsed, trailing separators are
dropped and `.`/`..` are resolved lexically. Symlinks are **not** resolved.
`/home/you/projects/team-alpha` and `/home/you/projects/team-alpha/` match;
`/home/you/projects/team-alpha` and `/home/you/projects/team-alpha/other` do
not.

### Discovery versus pinning

With `session_id: null` the collector walks `session.list` page by page and
admits every item that is:
- a primary session (no `parentID` string) — an explicit `null` or a non-string
  `parentID` is treated as UNKNOWN and never guessed;
- not archived;
- not in the exact deny list;
- reporting the configured worktree directory.

Several primaries in one worktree become separate stacked cards with
independent status, baseline and Gezien state. A session that disappears is
retained as GRACE for two complete discovery cycles and then dropped, while its
already-seen baseline is kept, so a returning id cannot re-raise READY.

Discovery must be **complete** (terminal cursor, no cycle, well-formed items)
before new primaries are admitted. An incomplete walk fails closed: existing
cards stay visible as `UNKNOWN` and a yellow banner explains why.

## Optional Windows interop

The example config contains a second source, `win-windows`, whose
`opencode_bin` is a `/mnt/c/...` path and whose comment says it may be deleted.
Keep it only if you actually run OpenCode natively on Windows; otherwise delete
the whole object. No other setting refers to it, and removing it cannot change
the Linux teams.

The Windows source is trustworthy to the same degree as the Windows CLI you
point at: same user privileges, same credentials, same data. See `SECURITY.md`.

## Polling, staleness and caps

- Poll interval and stale threshold are wall-clock seconds; they are not
  auto-tuned. If a poll plus the interval approaches the stale threshold, lower
  the number of teams or accept UNKNOWN cards; do not silently inflate the
  threshold.
- The per-session fan-out is three read-only GETs (`session.get`,
  `permission.list`, `message.list`). Per-session fetches run with bounded
  concurrency; discovery and all state mutations stay on the poll thread.
- Code constants (not configurable, deliberately):
  - `PAGE_LIMIT = 50`, `MAX_PAGES = 64`, `MAX_UNIQUE = 3200`
  - `MAX_PRIMARIES_PER_TEAM = 32` — beyond that the team fails closed to
    `UNKNOWN` with a "primary overflow" banner; never a partial winner
  - `MAX_CHILDREN = 16`, `GRACE_LIMIT = 2`
  - `FETCH_CONCURRENCY = 8` parallel session fetches
  - `MESSAGE_LIMIT = 5` messages per session, newest first

## State file

`--state` selects the state file (default `.runtime/last_seen.json`). It stores
only the mapping `(source_key, session_id) -> last_seen_message_id`, in nested
schema-2 form, written atomically (temp file + fsync + replace). A schema-1 flat
file is migrated on read. Losing the file only resets which answers you have
already acknowledged; it never touches OpenCode state or your worktrees.

`--metrics-jsonl PATH` optionally appends one JSON line per source per completed
cycle (call counts, latencies, outcome buckets, redacted samples), capped at
4 MiB. It is off by default and never changes collection behaviour.

## Gezien semantics

`Gezien` ("seen") is a local, in-memory acknowledgement for a READY card. It is
offered only when the card is READY:

- it clears the ready-pending flag for that `(source_key, session_id)` and the
  card returns to IDLE;
- it never issues a CLI/API call and cannot reach a session;
- it cannot clear `WAITING_PERMISSION`, which requires answering the permission
  prompt in the OpenCode session itself;
- it is not persisted; a restart re-baselines from the state file, so an
  acknowledged answer stays acknowledged, but the in-memory "awaiting" count
  starts fresh for anything not yet in the state file.

The awaiting count includes READY and WAITING_PERMISSION cards, because both
need a human; only READY offers the button.
