# Security

## Scope and trust model

OpenCode Session Monitor is a **single-user, single-machine, local** tool. It
reads session metadata from a local OpenCode installation and serves a read-only
dashboard on the loopback interface. There is no remote mode, no account system
and no multi-user isolation, by design: the trust boundary is the local user
account plus the loopback bind, not the HTTP layer.

## Read-only contract

- The only transport is `opencode api <operationId> [--param k=v ...]`, executed
  with `shell=False`.
- Operation ids come from a frozen allowlist of GET-style reads:
  `server.info`, `session.list`, `session.active`, `session.get`,
  `session.message.list`, `session.permission.list`.
- There is a single private call choke point. No public function accepts an
  operation id, HTTP method, path, request body or arbitrary flag from config or
  input, so a configuration file cannot escalate to a mutating operation.
- The monitor never prompts, aborts, answers a permission request, edits a file
  in a worktree, or writes to OpenCode state.
- No credential is read, copied, logged or forwarded. The CLI performs its own
  authentication from its own local state.

## Network exposure

- Default bind: `127.0.0.1`. Non-loopback binds are refused unless
  `--allow-external-bind` is passed explicitly.
- Loopback is checked by resolving the requested bind host: **every** resolved
  address must be loopback before the server starts.
- There is no authentication, no TLS and no CSRF token. Do not expose the port
  to a network, and do not put it behind a proxy that terminates trust for you.
- The HTTP server does not validate the `Host` header. With a loopback bind the
  practical exposure is limited to other local processes and pages rendered in a
  local browser; those could blindly trigger the acknowledgement endpoint
  (see below). Keep the bind on loopback.

## Endpoints

| Method | Path | Effect |
| --- | --- | --- |
| `GET` | `/`, `/index.html` | Dashboard HTML. |
| `GET` | `/status.json` | The same snapshot as JSON. |
| `POST` | `/ack` | Clears local READY state for one `(team, source, session)`. |

`POST /ack` accepts only `team_key`, `source_key` and `session_id`, ignores
everything else, rejects bodies outside 1..4096 bytes, and returns a 303
redirect. Its only effect is to mark a local answer as seen. It cannot reach a
session and cannot answer a permission prompt. Because it is unauthenticated,
any local process or page that can reach the port could dismiss a READY
highlight; that is the entire impact.

## What is written to disk

- `.runtime/last_seen.json` (or `--state`): `(source_key, session_id) ->
  last_seen_message_id`, written atomically. It contains no message text and no
  credentials.
- Optional `--metrics-jsonl PATH`: one bounded JSON line per source per
  completed cycle. Unknown-outcome samples are passed through the same
  redaction and length caps as the dashboard; the file is capped at 4 MiB. Off
  by default.

Nothing else is written. The monitor does not touch OpenCode state, your home
directory, Git worktrees or production systems.

## Untrusted data handling

Everything returned by the API is treated as untrusted feed data:

- The page is HTML-escaped, including session ids, titles, reasons and scroll
  anchors.
- Message text is redacted before display: assignment-style secrets (including
  underscore compounds such as `access_token`), `Bearer` values, `sk-` keys,
  `gh[pousr]_` tokens and PEM private-key blocks.
- Sizes are capped (per message, total messages, summary length, error length)
  so a pathological response cannot exhaust the dashboard or the state file.

Redaction is best-effort pattern matching, not a guarantee: do not paste real
credentials into a session and expect the dashboard to be a secret scanner.

## Windows interop trust boundary

If you configure a native Windows `opencode.exe` through its `/mnt/c/...` path,
the monitor executes that binary with your privileges through WSL2 interop. It
passes only the same allowlisted GET arguments, starts no Windows service and
opens no new port. The Windows side is trusted to the same degree as the Windows
CLI and the Windows user account you point it at; its credentials and data stay
on that side. An attacker who can replace that executable can run code as you —
protect it like any other binary you invoke.

## Threat model

In scope (mitigated):

- A configuration file trying to inject an operation, flag, path or method —
  rejected by strict allowlists and frozen argv.
- A malicious or broken API response causing HTML injection, unbounded memory
  use or a fabricated status — escaped, capped, and failed closed to `UNKNOWN`.
- Accidental network exposure — loopback default with an explicit override.
- Credential leakage through displayed text — redaction patterns.

Out of scope:

- A local attacker who already runs code as your user (they can read the state
  file and the config, and can call the CLI themselves).
- A compromised OpenCode CLI or Windows executable.
- Browser-based attacks beyond blind acknowledgement of READY cards.
- Denial of service against your own machine.

## Reporting

Please report security issues privately: open a security advisory on the
repository (or contact the maintainer directly) instead of a public issue, and
include the version, the configuration shape (no secrets) and a reproduction
that does not involve production data.
