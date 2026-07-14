"""Late-stream failures must not discard an already-complete turn.

Production bug: both Bedrock and Anthropic-direct returned the full
reply text, yet pi recorded ``stop_reason="error"`` with an EMPTY
``error_message`` and (for Bedrock) usage input=0/output=0. Mechanism:

* The provider ``except`` blocks unconditionally overwrote
  ``stop_reason`` — even when the wire's terminal marker
  (``messageStop`` / ``message_delta.stop_reason``) had already been
  processed and the turn was complete. A transport hiccup while
  draining the stream tail (before Bedrock's final ``metadata`` usage
  event) failed the whole turn.
* ``error_message = str(exc)`` — timeout-family exceptions
  (``TimeoutError``, ``socket.timeout``) stringify to ``""``, leaving
  an undebuggable empty diagnostic. Bedrock additionally flattened
  the exception to a string inside ``_iter_events``, losing the type.

The same stop-reason overwrite exists in the upstream TS project
(reported there); the empty-diagnostic half was a port regression.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

# pi.ai providers depend on extras (boto3 etc.). If the dev env
# doesn't have them installed, skip cleanly rather than blowing up
# at import time. The container that actually runs Pi backend has
# boto3 baked in.
boto3 = pytest.importorskip("boto3")

from pi.ai.providers.amazon_bedrock import BedrockOptions, stream_bedrock  # noqa: E402
from pi.ai.providers.anthropic import AnthropicOptions, stream_anthropic  # noqa: E402
from pi.ai.types import (  # noqa: E402
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    TextContent,
    UserMessage,
)


def _text(msg: Any) -> str:
    return "".join(getattr(b, "text", "") for b in msg.content)


async def _final_event(stream: Any) -> DoneEvent | ErrorEvent:
    async for ev in stream:
        if isinstance(ev, (DoneEvent, ErrorEvent)):
            return ev
    raise AssertionError("stream ended without DoneEvent/ErrorEvent")


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------

_BEDROCK_BODY = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hello, "}}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "world!"}}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
]
_BEDROCK_STOP = {"messageStop": {"stopReason": "end_turn"}}


class _FakeConverseStream:
    """Yields the given events, then raises ``tail_exc`` (if set)."""

    def __init__(self, events: list[dict[str, Any]], tail_exc: Exception | None) -> None:
        self._events = events
        self._tail_exc = tail_exc

    def __iter__(self) -> Any:
        yield from self._events
        if self._tail_exc is not None:
            raise self._tail_exc


async def _bedrock_final(
    events: list[dict[str, Any]], tail_exc: Exception | None
) -> DoneEvent | ErrorEvent:
    """Run stream_bedrock against a fake converse_stream and return the
    terminal event. The boto3 patch must stay active while the stream is
    CONSUMED — client construction happens inside the async task, not at
    call time."""
    fake_client = SimpleNamespace(
        converse_stream=lambda **kwargs: {"stream": _FakeConverseStream(events, tail_exc)}
    )
    model = Model(id="anthropic.claude-x", api="bedrock-converse-stream", provider="amazon-bedrock")
    ctx = Context(messages=[UserMessage(content=[TextContent(text="hi")])])
    with patch("pi.ai.providers.amazon_bedrock.boto3") as fake_boto3:
        fake_boto3.client.return_value = fake_client
        return await _final_event(stream_bedrock(model, ctx, BedrockOptions()))


@pytest.mark.asyncio
async def test_bedrock_tail_error_after_message_stop_keeps_completed_turn() -> None:
    """THE production repro: full reply + messageStop(end_turn) arrive,
    then the transport dies before the final ``metadata`` (usage) event
    with a TimeoutError (str() == ""). Must now finish as Done with the
    real stop reason — not stop_reason='error', error_message=''."""
    final = await _bedrock_final([*_BEDROCK_BODY, _BEDROCK_STOP], TimeoutError())
    assert isinstance(final, DoneEvent)
    assert final.message.stop_reason == "stop"
    assert _text(final.message) == "Hello, world!"
    assert final.message.error_message is None
    # Usage may legitimately be missing (metadata never arrived);
    # missing usage beats discarding a delivered response.
    assert final.message.usage.input == 0


@pytest.mark.asyncio
async def test_bedrock_midstream_error_still_fails_with_typed_diagnostic() -> None:
    """A failure BEFORE messageStop is a real failure — and the recorded
    diagnostic must never be empty (TimeoutError stringifies to "")."""
    final = await _bedrock_final(list(_BEDROCK_BODY), TimeoutError())
    assert isinstance(final, ErrorEvent)
    assert final.error.stop_reason == "error"
    assert final.error.error_message  # non-empty
    assert "TimeoutError" in final.error.error_message


@pytest.mark.asyncio
async def test_bedrock_midstream_error_message_preserved_verbatim() -> None:
    """Exceptions that DO carry a message keep it as-is (the repr
    fallback only kicks in for empty-str exceptions)."""
    final = await _bedrock_final(list(_BEDROCK_BODY), ValueError("throttled by proxy"))
    assert isinstance(final, ErrorEvent)
    assert final.error.error_message == "throttled by proxy"


@pytest.mark.asyncio
async def test_bedrock_error_mapped_stop_reason_is_not_rescued() -> None:
    """Guard rail: a messageStop whose wire reason maps to 'error'
    (refusal/guardrail/unknown) must stay on the error path even when
    the tail also fails — the degrade branch only covers turns that
    ended with a NORMAL terminal reason."""
    stop = {"messageStop": {"stopReason": "guardrail_intervened"}}
    final = await _bedrock_final([*_BEDROCK_BODY, stop], TimeoutError())
    assert isinstance(final, ErrorEvent)
    assert final.error.stop_reason == "error"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class _FakeSDKStream:
    """Mimics the anthropic SDK's ``client.messages.stream`` context
    manager + async iterator: yields ``events``, then raises
    ``tail_exc`` (if set) instead of a clean end."""

    def __init__(self, events: list[Any], tail_exc: Exception | None) -> None:
        self._events = list(events)
        self._tail_exc = tail_exc
        self._i = 0

    async def __aenter__(self) -> _FakeSDKStream:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self) -> _FakeSDKStream:
        return self

    async def __anext__(self) -> Any:
        if self._i < len(self._events):
            ev = self._events[self._i]
            self._i += 1
            return ev
        if self._tail_exc is not None:
            raise self._tail_exc
        raise StopAsyncIteration


def _anthropic_sdk_events(*, with_terminal: bool) -> list[Any]:
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    events: list[Any] = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=usage)),
        SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text")),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="Hello, world!"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
    ]
    if with_terminal:
        events.append(
            SimpleNamespace(
                type="message_delta",
                delta=SimpleNamespace(stop_reason="end_turn"),
                usage=usage,
            )
        )
    return events


async def _anthropic_final(
    events: list[Any], tail_exc: Exception | None
) -> DoneEvent | ErrorEvent:
    """Run stream_anthropic against a fake SDK stream and return the
    terminal event. The patch must stay active while the generator is
    consumed — _create_client runs on first iteration, not at call time."""
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **kwargs: _FakeSDKStream(events, tail_exc))
    )
    model = Model(id="claude-test", api="anthropic-messages", provider="anthropic")
    ctx = Context(messages=[UserMessage(content=[TextContent(text="hi")])])
    with patch(
        "pi.ai.providers.anthropic._create_client", return_value=(fake_client, False)
    ):
        return await _final_event(
            stream_anthropic(model, ctx, AnthropicOptions(api_key="test-key"))
        )


@pytest.mark.asyncio
async def test_anthropic_tail_error_after_terminal_keeps_completed_turn() -> None:
    """Anthropic twin of the Bedrock repro. Usage arrives with
    message_delta (before the failure), so it must survive too."""
    final = await _anthropic_final(_anthropic_sdk_events(with_terminal=True), TimeoutError())
    assert isinstance(final, DoneEvent)
    assert final.message.stop_reason == "stop"
    assert _text(final.message) == "Hello, world!"
    assert final.message.error_message is None
    assert final.message.usage.input == 10
    assert final.message.usage.output == 5


@pytest.mark.asyncio
async def test_anthropic_midstream_error_still_fails_with_typed_diagnostic() -> None:
    final = await _anthropic_final(_anthropic_sdk_events(with_terminal=False), TimeoutError())
    assert isinstance(final, ErrorEvent)
    assert final.error.stop_reason == "error"
    assert final.error.error_message
    assert "TimeoutError" in final.error.error_message
