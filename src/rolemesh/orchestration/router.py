"""Message formatting and outbound routing."""

from __future__ import annotations

import re
from html import escape as _html_escape
from typing import TYPE_CHECKING

from rolemesh.core.timezone import format_local_time

if TYPE_CHECKING:
    from rolemesh.core.types import Channel, NewMessage


def escape_xml(s: str) -> str:
    """Escape special XML characters."""
    if not s:
        return ""
    return _html_escape(s, quote=True)


def format_messages(messages: list[NewMessage], timezone: str) -> str:
    """Format messages as XML for the agent prompt."""
    lines = []
    for m in messages:
        display_time = format_local_time(m.timestamp, timezone)
        lines.append(
            f'<message sender="{escape_xml(m.sender_name)}" time="{escape_xml(display_time)}">'
            f"{escape_xml(m.content)}</message>"
        )

    header = f'<context timezone="{escape_xml(timezone)}" />\n'
    return f"{header}<messages>\n" + "\n".join(lines) + "\n</messages>"


_INTERNAL_TAG_RE = re.compile(r"<internal>[\s\S]*?</internal>")


def strip_internal_tags(text: str) -> str:
    """Remove <internal>...</internal> blocks from text."""
    return _INTERNAL_TAG_RE.sub("", text).strip()


def format_outbound(raw_text: str) -> str:
    """Format agent output for sending to users."""
    text = strip_internal_tags(raw_text)
    return text if text else ""


async def route_outbound(channels: list[Channel], jid: str, text: str) -> None:
    """Route an outbound message to the appropriate channel."""
    for ch in channels:
        if ch.owns_jid(jid) and ch.is_connected():
            await ch.send_message(jid, text)
            return
    raise RuntimeError(f"No channel for JID: {jid}")


def find_channel(channels: list[Channel], jid: str) -> Channel | None:
    """Find the channel that owns a JID."""
    for ch in channels:
        if ch.owns_jid(jid):
            return ch
    return None
