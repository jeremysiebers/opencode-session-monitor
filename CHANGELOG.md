# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-18

First public release.

### Added

- Read-only local dashboard for OpenCode sessions, grouped into six team
  containers in the example configuration.
- Multiple primary sessions per worktree, each with its own status, activity,
  baseline and acknowledgement state; child sessions stay advisory and never
  become cards.
- One poll source for WSL2/Linux, plus an optional native-Windows source
  reached through WSL2 interop (no Windows service or extra port).
- `READY` detection with the local **Gezien** acknowledgement.
- Compact, collapsible team containers with status badges.
- Team status priority and activity-based session ordering inside a team.
- `--version`, a top-level `version` key in `/status.json` and a version label
  in the dashboard header.

### Security

- Read-only, GET-only access through the OpenCode CLI with a frozen argv.
- Loopback-only dashboard bind by default; no credentials are read or stored.
