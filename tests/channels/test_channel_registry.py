"""Tests for rolemesh.channels.registry."""

from rolemesh.channels.registry import (
    get_channel_factory,
    get_registered_channel_names,
    register_channel,
)


def test_register_and_get_channel() -> None:
    def dummy_factory(opts: object) -> None:
        return None

    register_channel("test", dummy_factory)  # type: ignore[arg-type]
    assert get_channel_factory("test") is dummy_factory  # type: ignore[comparison-overlap]
    assert "test" in get_registered_channel_names()


def test_get_unknown_channel() -> None:
    assert get_channel_factory("nonexistent") is None
