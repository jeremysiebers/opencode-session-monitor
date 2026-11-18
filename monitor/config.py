"""Strict, user-driven configuration.

No operational fact (binary path, service URL, session ids or team/worktree
mapping) is hardcoded. Every value comes from the user-supplied JSON file;
this module validates it and refuses unknown keys so a config file can never
inject an operation, method or path into the API layer.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_STALE_THRESHOLD = 15.0
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765

# INC027d: schema 2 is the current format. Schema 1 remains readable so the
# existing user config (and offline tests) keep working without migration.
CONFIG_SCHEMA = 2
LEGACY_SCHEMA = 1
DEFAULT_SOURCE_KEY = "default"

_SESSION_RE = re.compile(r"^ses_[A-Za-z0-9_-]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

ALLOWED_TOP_KEYS = frozenset({
    "schema",
    "opencode_bin",
    "server_url",
    "poll_interval_seconds",
    "stale_threshold_seconds",
    "bind",
    "port",
    "teams",
    # INC027d (schema-2, sources[]-ready):
    "deny_session_ids",   # exact session-ID deny-list (never title/name based)
    "source_key",         # state namespacing key for this single source
    "sources",            # reserved for INC027e; accepted, not acted on in 027d
})
ALLOWED_TEAM_KEYS = frozenset({
    "key",
    "label",
    "worktree_dir",
    "session_id",
    # INC027e: exact pinned child ids (advisory only, never cards). Optional.
    "child_session_ids",
})

# INC027e: strict allowlist for each object in the schema-2 ``sources[]`` list.
# No operation, method, body or arbitrary command key is accepted.
SOURCE_ALLOWED_KEYS = frozenset({
    "key",
    "opencode_bin",
    "server_url",
    "teams",
    "deny_session_ids",
})


class ConfigError(Exception):
    """Raised when the user config is missing or invalid."""


@dataclass(frozen=True)
class TeamConfig:
    key: str
    label: str
    worktree_dir: str
    session_id: str | None = None
    child_session_ids: tuple = ()


@dataclass(frozen=True)
class SourceConfig:
    """One INC027e source: its own binary/auth/teams/state namespace.

    ``key`` doubles as the state namespacing ``source_key``. Exactly the same
    GET-only API wrapper is used for every source; only the binary path and the
    validated optional server URL differ. No credentials or shell wrapper are
    ever part of a source.
    """

    key: str
    opencode_bin: str
    server_url: str | None
    teams: tuple
    deny_session_ids: tuple = ()

    @property
    def source_key(self):
        """Alias used by the collector's unchanged INC027d namespacing logic."""
        return self.key


@dataclass(frozen=True)
class Config:
    opencode_bin: str
    server_url: str | None
    poll_interval: float
    stale_threshold: float
    bind: str
    port: int
    teams: tuple
    # INC027d additions (defaults keep schema-1 callers simple):
    schema: int = CONFIG_SCHEMA
    deny_session_ids: tuple = ()
    source_key: str = DEFAULT_SOURCE_KEY
    sources: tuple = ()  # INC027e: tuple of SourceConfig (empty => synthesized single source)

    def active_sources(self):
        """Return the sources to poll, in config order.

        A non-empty ``sources[]`` (schema 2) replaces the single implicit source.
        Schema 1 and schema 2 configs without ``sources[]`` keep the INC027d
        single-source behaviour, synthesized from the top-level fields.
        """
        if self.sources:
            return self.sources
        return (SourceConfig(
            key=self.source_key,
            opencode_bin=self.opencode_bin,
            server_url=self.server_url,
            teams=self.teams,
            deny_session_ids=self.deny_session_ids,
        ),)


def _positive_number(value, name, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s must be a positive number" % name)
    number = float(value)
    if number <= 0:
        raise ConfigError("%s must be a positive number" % name)
    return number


def _validate_server_url(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or any(c.isspace() for c in value) or "\x00" in value:
        raise ConfigError("server_url must be a plain URL string")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("server_url must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ConfigError("server_url must not embed credentials")
    try:
        port = parsed.port
    except ValueError:
        raise ConfigError("server_url has an invalid port")
    if port is None or not (1 <= port <= 65535):
        raise ConfigError("server_url must declare a numeric port 1..65535")
    return value


def _validate_team(raw, seen_keys):
    if not isinstance(raw, dict):
        raise ConfigError("each team must be an object")
    unknown = {k for k in raw if not k.startswith("_") and k not in ALLOWED_TEAM_KEYS}
    if unknown:
        raise ConfigError("unknown team keys: %s" % ", ".join(sorted(unknown)))
    key = raw.get("key")
    label = raw.get("label")
    worktree_dir = raw.get("worktree_dir")
    session_id = raw.get("session_id")

    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ConfigError("team key must be a short identifier")
    if key in seen_keys:
        raise ConfigError("duplicate team key: %s" % key)
    seen_keys.add(key)
    if not isinstance(label, str) or not label.strip() or "\x00" in label:
        raise ConfigError("team label must be a non-empty string")
    from .paths import is_absolute
    if (not isinstance(worktree_dir, str) or not worktree_dir
            or not is_absolute(worktree_dir) or "\x00" in worktree_dir):
        raise ConfigError("worktree_dir must be an absolute path")
    if session_id in (None, ""):
        session_id = None
    elif not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
        raise ConfigError("session_id must match ses_... or be empty")
    child_ids = raw.get("child_session_ids")
    if child_ids in (None, "", ()):
        child_ids = ()
    else:
        if not isinstance(child_ids, list):
            raise ConfigError("child_session_ids must be a list of exact ses_* ids")
        child_ids = tuple(_validate_deny_ids(child_ids))
    # Worktrees are compared with the shared pure-string normalizer at runtime;
    # we still normalize here for config-level display/compat. The runtime match
    # uses monitor.paths.same_path so Windows-style strings compare identically.
    from .paths import normalize_path
    normalized = normalize_path(worktree_dir)
    if normalized is None:
        raise ConfigError("worktree_dir must be an absolute path")
    return TeamConfig(
        key=key, label=label, worktree_dir=normalized,
        session_id=session_id, child_session_ids=child_ids,
    )


def _validate_deny_ids(value):
    """Exact session-ID deny-list only; never titles, names or substrings."""
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError("deny_session_ids must be a list")
    result = []
    for item in value:
        if not isinstance(item, str) or not _SESSION_RE.match(item):
            raise ConfigError("deny_session_ids entries must be exact ses_* ids")
        if item not in result:
            result.append(item)
    return tuple(result)


def _validate_source_key(value):
    if value in (None, ""):
        return DEFAULT_SOURCE_KEY
    if not isinstance(value, str) or not _KEY_RE.match(value):
        raise ConfigError("source_key must be a short identifier")
    return value


def _validate_sources(value):
    """INC027e: validate ``sources[]`` into immutable :class:`SourceConfig`s.

    Each source has its own absolute executable, validated optional server URL,
    teams and exact-ID deny list. No operation/method/body/shell key is accepted.
    An empty/absent list keeps the single-source INC027d default.
    """
    if value in (None, "", []):
        return ()
    if not isinstance(value, list):
        raise ConfigError("sources must be a list")

    result = []
    source_keys = set()
    team_keys = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ConfigError("each source must be an object")
        unknown = {k for k in raw if not k.startswith("_") and k not in SOURCE_ALLOWED_KEYS}
        if unknown:
            raise ConfigError("unknown source keys: %s" % ", ".join(sorted(unknown)))

        key = _validate_source_key(raw.get("key"))
        if key in source_keys:
            raise ConfigError("duplicate source key: %s" % key)
        source_keys.add(key)

        binary = raw.get("opencode_bin")
        # Native Windows exe is invoked from WSL2 through its absolute
        # /mnt/c/... interop path, which is POSIX-absolute to the collector.
        if (not isinstance(binary, str) or not binary
                or not os.path.isabs(binary) or "\x00" in binary):
            raise ConfigError("source opencode_bin must be an absolute path string")
        url = _validate_server_url(raw.get("server_url"))

        teams_data = raw.get("teams")
        if not isinstance(teams_data, list) or not teams_data:
            raise ConfigError("source teams must be a non-empty list")
        seen = set()
        teams = tuple(_validate_team(item, seen) for item in teams_data)
        for team in teams:
            if team.key in team_keys:
                raise ConfigError("team key must be unique across sources: %s" % team.key)
            team_keys.add(team.key)

        result.append(SourceConfig(
            key=key,
            opencode_bin=binary,
            server_url=url,
            teams=teams,
            deny_session_ids=_validate_deny_ids(raw.get("deny_session_ids")),
        ))
    return tuple(result)


def load_config(path):
    """Load and validate an user-supplied config file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigError("cannot read config: %s" % exc)
    except ValueError as exc:
        raise ConfigError("config is not valid JSON: %s" % exc)

    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")
    unknown = {k for k in data if not k.startswith("_") and k not in ALLOWED_TOP_KEYS}
    if unknown:
        raise ConfigError("unknown config keys: %s" % ", ".join(sorted(unknown)))
    schema = data.get("schema")
    if schema not in (LEGACY_SCHEMA, CONFIG_SCHEMA):
        raise ConfigError("schema must be 1 or 2")

    binary = data.get("opencode_bin")
    if (not isinstance(binary, str) or not binary
            or not os.path.isabs(binary) or "\x00" in binary):
        raise ConfigError("opencode_bin must be an absolute path string")

    server_url = _validate_server_url(data.get("server_url"))
    poll_interval = _positive_number(
        data.get("poll_interval_seconds"), "poll_interval_seconds", DEFAULT_POLL_INTERVAL
    )
    stale_threshold = _positive_number(
        data.get("stale_threshold_seconds"), "stale_threshold_seconds", DEFAULT_STALE_THRESHOLD
    )

    bind = data.get("bind", DEFAULT_BIND)
    if not isinstance(bind, str) or not bind.strip() or any(c.isspace() for c in bind):
        raise ConfigError("bind must be a host string")

    port = data.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("port must be an integer 1..65535")

    teams_data = data.get("teams")
    if not isinstance(teams_data, list) or not teams_data:
        raise ConfigError("teams must be a non-empty list")
    seen = set()
    teams = tuple(_validate_team(raw, seen) for raw in teams_data)

    return Config(
        opencode_bin=binary,
        server_url=server_url,
        poll_interval=poll_interval,
        stale_threshold=stale_threshold,
        bind=bind,
        port=port,
        teams=teams,
        schema=schema,
        deny_session_ids=_validate_deny_ids(data.get("deny_session_ids")),
        source_key=_validate_source_key(data.get("source_key")),
        sources=_validate_sources(data.get("sources")),
    )
