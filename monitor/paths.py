"""Pure-string path normalization for exact worktree matching.

This module deliberately performs **no filesystem access**: no path resolution,
no attribute queries, no symlink following and no dependence on the host
separator. The same function is used by config validation and by the
collector, so a worktree declared in the user config and a worktree reported
by ``session.get`` / ``session.list`` are compared by one shared rule.

It handles POSIX paths (``/home/you/projects/team-alpha``), Windows-style paths
(``C:\\Users\\you\\projects\\team-windows`` /
``C:/Users/you/projects/team-windows``) and mixed separators,
folds drive-letter case, collapses duplicate separators, drops trailing
separators and resolves ``.`` / ``..`` lexically. Two strings that denote the
same path normalize to the same string; different paths never collide.
"""
from __future__ import annotations

import re

_DRIVE_RE = re.compile(r"^([A-Za-z]):")


def normalize_path(path):
    """Return a canonical string for ``path`` or ``None`` when unusable.

    Pure string transform only — never touches the filesystem. Inputs that are
    not non-empty strings return ``None`` so callers fail closed.
    """
    if not isinstance(path, str):
        return None
    text = path.strip()
    if not text:
        return None

    # Unify Windows separators to '/'; nothing here depends on the host separator.
    text = text.replace("\\", "/")

    # Normalize a leading drive letter (C:, d:, ...) to lowercase.
    drive = ""
    match = _DRIVE_RE.match(text)
    if match:
        drive = match.group(1).lower() + ":"
        text = text[len(match.group(0)):]

    absolute = text.startswith("/")
    segments = []
    for segment in text.split("/"):
        if segment == "" or segment == ".":
            continue
        if segment == "..":
            if segments and segments[-1] != "..":
                segments.pop()
            elif not absolute:
                # A leading '..' on a relative path is meaningful.
                segments.append("..")
            # On an absolute path a leading '..' is a no-op (already at root).
            continue
        segments.append(segment)
    body = "/".join(segments)

    if drive:
        return drive + "/" + body if body else drive + "/"
    if absolute:
        return "/" + body if body else "/"
    return body if body else "."


def is_absolute(path):
    """Pure-string absolute-path check (POSIX '/' or Windows drive/UNC)."""
    if not isinstance(path, str):
        return False
    text = path.strip()
    if not text:
        return False
    if _DRIVE_RE.match(text):
        return True
    return text.startswith("/") or text.startswith("\\")


def same_path(left, right):
    """True when two paths normalize to the same canonical string."""
    a = normalize_path(left)
    b = normalize_path(right)
    return a is not None and b is not None and a == b
