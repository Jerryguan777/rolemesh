"""Tests for rolemesh.router."""

from rolemesh.core.types import NewMessage
from rolemesh.orchestration.router import escape_xml, format_messages, format_outbound, strip_internal_tags


def test_escape_xml_basic() -> None:
    assert escape_xml("") == ""
    assert escape_xml("hello") == "hello"
    assert escape_xml("<script>") == "&lt;script&gt;"
    assert escape_xml('a="b"') == "a=&quot;b&quot;"
    assert escape_xml("a&b") == "a&amp;b"


def test_format_messages() -> None:
    messages = [
        NewMessage(
            id="1",
            chat_jid="chat@jid",
            sender="user@jid",
            sender_name="Alice",
            content="Hello",
            timestamp="2024-01-15T14:30:00Z",
        ),
    ]
    result = format_messages(messages, "UTC")
    assert '<context timezone="UTC" />' in result
    assert "<messages>" in result
    assert 'sender="Alice"' in result
    assert "Hello" in result


def test_strip_internal_tags() -> None:
    assert strip_internal_tags("hello <internal>secret</internal> world") == "hello  world"
    assert strip_internal_tags("no tags") == "no tags"
    assert strip_internal_tags("<internal>all internal</internal>") == ""


def test_format_outbound() -> None:
    assert format_outbound("hello <internal>hidden</internal> world") == "hello  world"
    assert format_outbound("<internal>all hidden</internal>") == ""
    assert format_outbound("plain text") == "plain text"


def test_find_channel_found() -> None:
    from unittest.mock import MagicMock

    from rolemesh.orchestration.router import find_channel

    ch = MagicMock()
    ch.owns_jid.return_value = True
    result = find_channel([ch], "chat@jid")
    assert result is ch


def test_find_channel_not_found() -> None:
    from unittest.mock import MagicMock

    from rolemesh.orchestration.router import find_channel

    ch = MagicMock()
    ch.owns_jid.return_value = False
    result = find_channel([ch], "chat@jid")
    assert result is None


async def test_route_outbound() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from rolemesh.orchestration.router import route_outbound

    ch = MagicMock()
    ch.owns_jid.return_value = True
    ch.is_connected.return_value = True
    ch.send_message = AsyncMock()
    await route_outbound([ch], "chat@jid", "hello")
    ch.send_message.assert_called_once_with("chat@jid", "hello")


async def test_route_outbound_no_channel() -> None:
    import pytest

    from rolemesh.orchestration.router import route_outbound

    with pytest.raises(RuntimeError, match="No channel"):
        await route_outbound([], "chat@jid", "hello")
