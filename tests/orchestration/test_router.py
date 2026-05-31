"""Tests for rolemesh.orchestration.router.

The string-formatting helpers are pure and tested directly. For channel
routing we use a concrete FakeChannel that implements the real Channel
protocol (owns_jid / is_connected / send_message) instead of MagicMock —
a MagicMock returns a truthy stub for *any* attribute access, so it would
pass even if route_outbound called a method that doesn't exist or checked
a property that was renamed. The fake pins the real interface and records
what was actually sent.
"""

from __future__ import annotations

import pytest

from rolemesh.core.types import NewMessage
from rolemesh.orchestration.router import (
    escape_xml,
    find_channel,
    format_messages,
    format_outbound,
    route_outbound,
    strip_internal_tags,
)


class FakeChannel:
    def __init__(self, owns: set[str] | None = None, connected: bool = True) -> None:
        self._owns = owns or set()
        self._connected = connected
        self.sent: list[tuple[str, str]] = []

    def owns_jid(self, jid: str) -> bool:
        return jid in self._owns

    def is_connected(self) -> bool:
        return self._connected

    async def send_message(self, jid: str, text: str) -> None:
        self.sent.append((jid, text))


# --- pure string helpers -----------------------------------------------------


def test_escape_xml_handles_all_special_chars_and_empty() -> None:
    assert escape_xml("") == ""
    assert escape_xml("hello") == "hello"
    assert escape_xml("<script>") == "&lt;script&gt;"
    assert escape_xml('a="b"') == "a=&quot;b&quot;"
    assert escape_xml("a&b") == "a&amp;b"


def test_format_messages_escapes_injected_markup_in_content() -> None:
    """A user whose name/content contains XML must not break out of the
    <message> envelope — every interpolated field is escaped."""
    messages = [
        NewMessage(
            id="1", chat_jid="chat@jid", sender="user@jid",
            sender_name='Alice"/><inject>', content="<script>x</script>",
            timestamp="2024-01-15T14:30:00Z",
        ),
    ]
    result = format_messages(messages, "UTC")
    assert '<context timezone="UTC" />' in result
    assert "<inject>" not in result  # the raw tag must be escaped
    assert "&lt;script&gt;" in result
    assert "<script>x</script>" not in result


def test_strip_internal_tags_removes_blocks_and_trims() -> None:
    assert strip_internal_tags("hello <internal>secret</internal> world") == "hello  world"
    assert strip_internal_tags("no tags") == "no tags"
    assert strip_internal_tags("<internal>all internal</internal>") == ""


def test_strip_internal_tags_handles_multiline_block() -> None:
    """The block can span newlines ([\\s\\S]*?). A naive '.' regex would
    leak the inner content."""
    text = "before <internal>line1\nline2</internal> after"
    assert strip_internal_tags(text) == "before  after"


def test_format_outbound_strips_internal() -> None:
    assert format_outbound("hello <internal>hidden</internal> world") == "hello  world"
    assert format_outbound("<internal>all hidden</internal>") == ""
    assert format_outbound("plain text") == "plain text"


# --- channel routing ---------------------------------------------------------


def test_find_channel_selects_the_owning_channel_not_the_first() -> None:
    a = FakeChannel(owns={"a@jid"})
    b = FakeChannel(owns={"b@jid"})
    assert find_channel([a, b], "b@jid") is b


def test_find_channel_returns_none_when_unowned() -> None:
    assert find_channel([FakeChannel(owns={"a@jid"})], "z@jid") is None


async def test_route_outbound_delivers_to_owning_connected_channel() -> None:
    owner = FakeChannel(owns={"chat@jid"}, connected=True)
    other = FakeChannel(owns={"else@jid"}, connected=True)
    await route_outbound([other, owner], "chat@jid", "hello")
    assert owner.sent == [("chat@jid", "hello")]
    assert other.sent == []  # non-owner must not receive the message


async def test_route_outbound_skips_disconnected_owner() -> None:
    """A channel that owns the JID but is disconnected must be skipped, not
    used. This path (owns_jid True, is_connected False) was previously
    untested — a regression dropping the is_connected guard would deliver
    to a dead channel."""
    dead = FakeChannel(owns={"chat@jid"}, connected=False)
    with pytest.raises(RuntimeError, match="No channel"):
        await route_outbound([dead], "chat@jid", "hi")
    assert dead.sent == []


async def test_route_outbound_fails_over_to_connected_owner() -> None:
    dead = FakeChannel(owns={"chat@jid"}, connected=False)
    live = FakeChannel(owns={"chat@jid"}, connected=True)
    await route_outbound([dead, live], "chat@jid", "hi")
    assert live.sent == [("chat@jid", "hi")]
    assert dead.sent == []


async def test_route_outbound_raises_when_no_channel_matches() -> None:
    with pytest.raises(RuntimeError, match="No channel"):
        await route_outbound([], "chat@jid", "hello")
