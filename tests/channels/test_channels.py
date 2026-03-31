"""Tests for Telegram and Slack channel implementations."""

from __future__ import annotations

from rolemesh.channels.registry import get_channel_factory, get_registered_channel_names


def test_telegram_registered() -> None:
    """Telegram channel is self-registered on import."""
    assert "telegram" in get_registered_channel_names()
    factory = get_channel_factory("telegram")
    assert factory is not None


def test_slack_registered() -> None:
    """Slack channel is self-registered on import."""
    assert "slack" in get_registered_channel_names()
    factory = get_channel_factory("slack")
    assert factory is not None


def test_telegram_factory_returns_none_without_token(monkeypatch: object) -> None:
    """Telegram factory returns None when TELEGRAM_BOT_TOKEN is not set."""
    import pytest

    mp = pytest.MonkeyPatch()
    mp.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    factory = get_channel_factory("telegram")
    assert factory is not None
    from unittest.mock import MagicMock, patch

    opts = MagicMock()
    # Mock read_env_file to return empty (no .env token)
    with (
        patch("rolemesh.channels.telegram.read_env_file", return_value={}),
        patch("rolemesh.channels.telegram.os.environ.get", return_value=None),
    ):
        result = factory(opts)
    assert result is None
    mp.undo()


def test_slack_factory_returns_none_without_tokens() -> None:
    """Slack factory returns None when tokens are not set."""
    factory = get_channel_factory("slack")
    assert factory is not None
    from unittest.mock import MagicMock

    opts = MagicMock()
    result = factory(opts)
    assert result is None


def test_telegram_owns_jid() -> None:
    """TelegramChannel.owns_jid matches tg: prefix."""
    from unittest.mock import MagicMock

    from rolemesh.channels.telegram import TelegramChannel

    ch = TelegramChannel("fake-token", MagicMock())
    assert ch.owns_jid("tg:123456") is True
    assert ch.owns_jid("slack:C123") is False
    assert ch.owns_jid("123456") is False


def test_slack_channel_owns_jid() -> None:
    """SlackChannel.owns_jid matches slack: prefix."""
    from unittest.mock import MagicMock, patch

    with patch(
        "rolemesh.channels.slack.read_env_file",
        return_value={
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
        },
    ):
        from rolemesh.channels.slack import SlackChannel

        ch = SlackChannel(MagicMock())
    assert ch.owns_jid("slack:C0123456789") is True
    assert ch.owns_jid("tg:123") is False
