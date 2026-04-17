"""GitHub Copilot request headers — ported from packages/ai/src/providers/github-copilot-headers.ts."""

from __future__ import annotations

from collections.abc import Sequence

from pi.ai.types import Message


def infer_copilot_initiator(messages: Sequence[Message]) -> str:
    """Infer X-Initiator header value from message history.

    Copilot expects "user" for user-initiated requests and "agent" for
    follow-up requests that come after assistant or tool messages.

    Args:
        messages: Current conversation messages.

    Returns:
        "user" or "agent".
    """
    if not messages:
        return "user"
    last = messages[-1]
    return "agent" if last.role != "user" else "user"


def has_copilot_vision_input(messages: Sequence[Message]) -> bool:
    """Check whether any message in the conversation contains image content.

    Copilot requires the Copilot-Vision-Request header when sending images.

    Args:
        messages: Conversation messages to inspect.

    Returns:
        True if any user or toolResult message contains image content.
    """
    for msg in messages:
        if msg.role == "user" and isinstance(msg.content, list) and any(c.type == "image" for c in msg.content):
            return True
        if msg.role == "toolResult" and isinstance(msg.content, list) and any(c.type == "image" for c in msg.content):
            return True
    return False


def build_copilot_dynamic_headers(
    messages: Sequence[Message],
    has_images: bool,
) -> dict[str, str]:
    """Build dynamic Copilot request headers.

    Args:
        messages: Current conversation messages (used to infer initiator).
        has_images: Whether the request contains image content.

    Returns:
        Dict of header name → value to merge with provider defaults.
    """
    headers: dict[str, str] = {
        "X-Initiator": infer_copilot_initiator(messages),
        "Openai-Intent": "conversation-edits",
    }
    if has_images:
        headers["Copilot-Vision-Request"] = "true"
    return headers
