"""Helpers for interpreting read-only OpenCode message payloads.

The exact v2 message envelope is not fully documented, so these helpers
tolerate the common flat and wrapped shapes without ever inventing a value.
They only *read* data already returned by a GET.
"""
from __future__ import annotations


def _info(message):
    """Return the inner info object if present, else an empty mapping."""
    if isinstance(message, dict) and isinstance(message.get("info"), dict):
        return message["info"]
    return {}


def message_id(message):
    """Return the message id string or None."""
    info = _info(message)
    if isinstance(info.get("id"), str) and info["id"]:
        return info["id"]
    if isinstance(message, dict) and isinstance(message.get("id"), str) and message["id"]:
        return message["id"]
    return None


def role_of(message):
    """Return a lower-cased role/type string or '' if absent."""
    info = _info(message)
    candidates = (
        info.get("role"),
        message.get("role") if isinstance(message, dict) else None,
        info.get("type"),
        message.get("type") if isinstance(message, dict) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return ""


def is_assistant(message):
    """True only for a message whose role is an assistant variant."""
    return role_of(message) in ("assistant", "assistant_message", "assistantmessage")


def message_time_ms(message):
    """Return an epoch-millisecond timestamp from a message, or None."""
    info = _info(message)
    timing = info.get("time")
    if not isinstance(timing, dict):
        timing = message.get("time") if isinstance(message, dict) else None
    if isinstance(timing, dict):
        for key in ("updated", "created"):
            value = timing.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def extract_text(message):
    """Return the concatenated human-readable text of a message's parts.

    Text parts are expected as {"type": "text", "text": "..."} inside a
    ``content`` (or ``parts``) list. Plain string parts are also accepted.
    Reasoning/tool parts that carry no string ``text`` field are ignored.
    """
    if not isinstance(message, dict):
        return ""
    info = _info(message)
    container = message.get("content")
    if container is None:
        container = message.get("parts")
    if container is None:
        container = info.get("parts")
    parts = container if isinstance(container, list) else []
    collected = []
    for part in parts:
        if isinstance(part, str):
            collected.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                collected.append(text)
    return "\n".join(collected)
