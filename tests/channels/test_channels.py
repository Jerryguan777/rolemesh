"""Tests for channel gateway helpers.

The gateways themselves are network-bound (Telegram Bot / Slack App), so
the previous tests only read back the constant ``channel_type`` property —
a tautology. Instead we test the pure logic that actually has bugs to
find: Slack timestamp parsing and the per-bot binding registry.
"""

from __future__ import annotations

from rolemesh.channels.slack_gateway import _slack_ts_to_iso
from rolemesh.channels.telegram_gateway import _BotInstance


async def _noop(*args: object) -> None:
    pass


# --- _slack_ts_to_iso --------------------------------------------------------


def test_slack_ts_parses_fractional_seconds_to_utc() -> None:
    # 1700000000.000500 = 2023-11-14T22:13:20.000500+00:00
    out = _slack_ts_to_iso("1700000000.000500")
    assert out.startswith("2023-11-14T22:13:20")
    assert out.endswith("+00:00")  # always normalized to UTC


def test_slack_ts_epoch_zero() -> None:
    assert _slack_ts_to_iso("0").startswith("1970-01-01T00:00:00")


def test_slack_ts_invalid_returns_empty_string() -> None:
    """Malformed timestamps (Slack quirks, truncated payloads) must not
    raise — they degrade to '' so the caller can drop the field."""
    assert _slack_ts_to_iso("not-a-number") == ""
    assert _slack_ts_to_iso("") == ""


def test_slack_ts_out_of_range_returns_empty_string() -> None:
    # CAUGHT A REAL BUG: a many-digit ts raises OverflowError ("timestamp
    # out of range for platform time_t"), which is NOT a ValueError/OSError,
    # so the helper's except clause let it propagate. Fixed by adding
    # OverflowError to the caught set in slack_gateway._slack_ts_to_iso.
    assert _slack_ts_to_iso("9" * 40) == ""


# --- _BotInstance binding registry -------------------------------------------


def test_bot_instance_starts_with_no_bindings() -> None:
    bot = _BotInstance(token="t", on_message=_noop)  # type: ignore[arg-type]
    assert bot.has_bindings is False


def test_add_binding_makes_has_bindings_true() -> None:
    bot = _BotInstance(token="t", on_message=_noop)  # type: ignore[arg-type]
    bot.add_binding_id("b1", "Alice")
    assert bot.has_bindings is True
    assert bot._display_names["b1"] == "Alice"


def test_add_binding_is_idempotent() -> None:
    """Re-adding the same binding (reconnect, duplicate event) must not
    create a second entry — otherwise removal would leave a dangling id and
    the bot would never shut down."""
    bot = _BotInstance(token="t", on_message=_noop)  # type: ignore[arg-type]
    bot.add_binding_id("b1")
    bot.add_binding_id("b1")
    assert bot._binding_ids == ["b1"]


def test_remove_last_binding_clears_has_bindings() -> None:
    bot = _BotInstance(token="t", on_message=_noop)  # type: ignore[arg-type]
    bot.add_binding_id("b1")
    bot.remove_binding_id("b1")
    assert bot.has_bindings is False


def test_remove_unknown_binding_is_a_noop() -> None:
    bot = _BotInstance(token="t", on_message=_noop)  # type: ignore[arg-type]
    bot.add_binding_id("b1")
    bot.remove_binding_id("does-not-exist")  # must not raise
    assert bot._binding_ids == ["b1"]


def test_add_binding_without_display_name_omits_it() -> None:
    bot = _BotInstance(token="t", on_message=_noop)  # type: ignore[arg-type]
    bot.add_binding_id("b1")
    assert "b1" not in bot._display_names
