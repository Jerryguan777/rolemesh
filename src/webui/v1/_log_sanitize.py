"""Redact secret-bearing fields from structured log payloads.

Used by handlers that accept credential bodies — the goal is that
dev consoles and shipped log files never contain a real API key,
even if the request body sails through to ``logger.info`` somewhere
by accident. Centralised here so the field-name allow-list is one
edit instead of N copies.
"""

from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"

SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "api_key",
        "apiKey",
        "secret",
        "client_secret",
        "refresh_token",
        "access_token",
        "password",
    }
)


def sanitize_for_log(payload: Any) -> Any:
    """Return a deep copy of ``payload`` with secret fields redacted.

    - dict: walk keys; any name in ``SENSITIVE_FIELDS`` (case-sensitive
      to mirror the wire schema) is replaced with ``"<redacted>"``.
    - list / tuple: recurse element-wise; the container type is
      preserved so callers can log without coercing.
    - anything else: returned as-is. We never read string values for
      "looks like a JWT" — content-based heuristics drift fast.
    """
    if isinstance(payload, dict):
        return {
            k: (REDACTED if k in SENSITIVE_FIELDS else sanitize_for_log(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [sanitize_for_log(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(sanitize_for_log(v) for v in payload)
    return payload
