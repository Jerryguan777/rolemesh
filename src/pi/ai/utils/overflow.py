"""Context overflow detection — Python port of packages/ai/src/utils/overflow.ts."""

from __future__ import annotations

import re

from pi.ai.types import AssistantMessage

OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"input is too long for requested model", re.IGNORECASE),
    re.compile(r"exceeds the context window", re.IGNORECASE),
    re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),
    re.compile(r"maximum prompt length is \d+", re.IGNORECASE),
    re.compile(r"reduce the length of the messages", re.IGNORECASE),
    re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),
    re.compile(r"exceeds the limit of \d+", re.IGNORECASE),
    re.compile(r"exceeds the available context size", re.IGNORECASE),
    re.compile(r"greater than the context length", re.IGNORECASE),
    re.compile(r"context window exceeds limit", re.IGNORECASE),
    re.compile(r"exceeded model token limit", re.IGNORECASE),
    re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"token limit exceeded", re.IGNORECASE),
]

_STATUS_CODE_PATTERN = re.compile(r"^4(00|13)\s*(status code)?\s*\(no body\)", re.IGNORECASE)


def is_context_overflow(message: AssistantMessage, context_window: int | None = None) -> bool:
    """Check if an assistant message represents a context overflow error.

    Handles two cases:
    1. Error-based overflow: error message matching known patterns.
    2. Silent overflow: usage.input exceeds context_window (e.g. z.ai).
    """
    # Case 1: Check error message patterns
    if message.stop_reason == "error" and message.error_message:
        if any(p.search(message.error_message) for p in OVERFLOW_PATTERNS):
            return True
        if _STATUS_CODE_PATTERN.search(message.error_message):
            return True

    # Case 2: Silent overflow
    if context_window and message.stop_reason == "stop":
        input_tokens = message.usage.input + message.usage.cache_read
        if input_tokens > context_window:
            return True

    return False


def get_overflow_patterns() -> list[re.Pattern[str]]:
    """Get the overflow patterns (for testing)."""
    return list(OVERFLOW_PATTERNS)
