"""Secret redaction and size caps for untrusted API text.

All text that originates from an untrusted API response or a secret-bearing
field is passed through :func:`redact` and the applicable caps before it can
reach the dashboard or a log/state record.
"""
from __future__ import annotations

import re

from .messages import extract_text

# Caps (mirror the bridge contract).
MAX_MESSAGES = 20
MAX_CHARS_PER_MESSAGE = 4096
MAX_TOTAL_CHARS = 32768
MAX_ERROR_CHARS = 200
DISPLAY_SUMMARY_CHARS = 240

# Compound forms (access_token, secret_key, auth_token, db_access_token, ...)
# must be covered too: a plain \b boundary does not fire between "_" and a
# keyword letter, so such keys previously escaped redaction.
_SECRET_KEY_ATOM = r"(?:password|passwd|secret|token|api[_-]?key|apikey|authorization)"
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:[A-Za-z0-9]+_)*" + _SECRET_KEY_ATOM + r"(?:_[A-Za-z0-9]+)*\b)"
    r"(\s*[:=]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{4,}")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{8,}")
_GH_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.S,
)


def redact(text):
    """Redact secret-looking material from ``text``.

    Covers assignment-style secrets (including underscore-compound keys such
    as ``access_token`` and ``secret_key``), Bearer values, OpenAI-style
    ``sk-`` keys, GitHub-style ``gh[pousr]_`` tokens and PEM private-key blocks.
    Redaction is applied even if a value would otherwise be truncated.
    """
    if not isinstance(text, str):
        return ""
    out = _PEM_RE.sub("[REDACTED_PEM]", text)
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _ASSIGNMENT_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", out)
    out = _SK_RE.sub("[REDACTED_SK]", out)
    out = _GH_RE.sub("[REDACTED_GH]", out)
    return out


def cap_text(text, limit):
    """Truncate ``text`` to at most ``limit`` characters (single ellipsis)."""
    if not isinstance(text, str) or limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "\u2026"
    return text[: limit - 1] + "\u2026"


def summarize_messages(
    messages,
    max_messages=MAX_MESSAGES,
    per_message=MAX_CHARS_PER_MESSAGE,
    total=MAX_TOTAL_CHARS,
):
    """Return a bounded, redacted view of message text.

    ``messages`` is read in the order supplied (the collector supplies newest
    first). The result reports how many messages were kept, the accumulated
    character count and whether anything was truncated.
    """
    if not isinstance(messages, list):
        messages = []
    kept = []
    used = 0
    truncated = len(messages) > max_messages
    for message in messages[:max_messages]:
        safe = redact(extract_text(message))
        if len(safe) > per_message:
            safe = cap_text(safe, per_message)
            truncated = True
        remaining = total - used
        if remaining <= 0:
            truncated = True
            break
        if len(safe) > remaining:
            safe = cap_text(safe, remaining)
            truncated = True
        used += len(safe)
        kept.append(safe)
    return {
        "texts": kept,
        "count": len(kept),
        "total_chars": used,
        "truncated": truncated,
    }


def safe_error(text):
    """Bounded, redacted form of an error string for display/logging."""
    return cap_text(redact(text if isinstance(text, str) else str(text)), MAX_ERROR_CHARS)
