"""Tests for channel gateway implementations."""

from __future__ import annotations

from rolemesh.channels.slack_gateway import SlackGateway
from rolemesh.channels.telegram_gateway import TelegramGateway


def test_telegram_gateway_channel_type() -> None:
    async def noop(*args: object) -> None:
        pass

    gw = TelegramGateway(on_message=noop)  # type: ignore[arg-type]
    assert gw.channel_type == "telegram"


def test_slack_gateway_channel_type() -> None:
    async def noop(*args: object) -> None:
        pass

    gw = SlackGateway(on_message=noop)  # type: ignore[arg-type]
    assert gw.channel_type == "slack"
