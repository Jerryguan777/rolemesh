"""Adversarial tests for the rolemesh.observability framework.

Each test corresponds to a T-N scenario in FR-12 of the brief. The
goal is to catch real bug classes the noop-falls-back-on-everything
design might hide — NOT to mirror the implementation back at itself.

T-7 (mutation testing) is described in tests/observability/MUTATION.md
— it's an honesty check on the assertions here. If you weaken an
assertion below to silence a test, run the mutation pass to see how
many mutations your weakened suite still catches.

The ``T-N`` naming convention (uppercase T) deliberately mirrors the
brief so test failures map back to documented scenarios in one
search. PEP 8's lowercase function-name rule (N802) is intentionally
suppressed for this file; matches the existing ``test_A1_...`` /
``test_A2_...`` convention in tests/container/.
"""

# ruff: noqa: N802

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_runner.backend import ResultEvent, UsageSnapshot
from agent_runner.hooks.events import (
    StopEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_runner.hooks.observability_handler import OtelHookHandler
from agent_runner.main import _emit_claude_message_span
from rolemesh.ipc.protocol import AgentInitData
from rolemesh.observability import (
    attach_parent_context,
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    install_tracer,
    is_installed,
    shutdown_tracer,
)

# ---------------------------------------------------------------------------
# T-1: ResultEvent before any tool span — start_time fallback
# ---------------------------------------------------------------------------


def test_T1_claude_message_span_emits_with_positive_duration_when_no_prior_tool(
    in_memory_tracer: Any,
) -> None:
    """Claude can reply without using any tool; the synthetic span
    must still appear with start <= end and a non-zero duration so
    Langfuse renders it as a finite bar rather than a zero-width
    point."""
    event = ResultEvent(
        text="Hello world",
        is_final=True,
        usage=UsageSnapshot(
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=5,
            cache_write_tokens=2,
            model_id="claude-3-5-sonnet-20241022",
        ),
    )
    _emit_claude_message_span(event)
    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) == 1, "expected exactly one claude.message span"
    span = spans[0]
    assert span.name == "claude.message"
    assert span.start_time > 0
    assert span.end_time > 0
    assert span.start_time <= span.end_time, "negative-duration span"
    assert span.end_time - span.start_time > 0, "zero-width span"
    # T-7 mutation #2 guard: input/output token attributes are
    # carried under their gen_ai.* names and *not* swapped. Asserting
    # both with their distinct values catches a swap mutation.
    assert span.attributes["gen_ai.usage.input_tokens"] == 10
    assert span.attributes["gen_ai.usage.output_tokens"] == 20
    assert span.attributes["gen_ai.usage.cache_read_input_tokens"] == 5
    assert span.attributes["gen_ai.usage.cache_creation_input_tokens"] == 2
    assert span.attributes["gen_ai.system"] == "anthropic"
    assert span.attributes["gen_ai.request.model"] == "claude-3-5-sonnet-20241022"


def test_T1b_claude_message_span_handles_missing_usage(
    in_memory_tracer: Any,
) -> None:
    """Some Claude SDK error paths emit ResultEvent without usage.
    The span should still appear (so the call is visible) without
    raising on missing token fields."""
    event = ResultEvent(text="degraded", usage=None, is_final=True)
    _emit_claude_message_span(event)
    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) == 1
    # Optional attrs should simply be absent, not 0/None which would
    # make Langfuse compute cost as if usage was reported.
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes
    assert "gen_ai.usage.output_tokens" not in spans[0].attributes


# ---------------------------------------------------------------------------
# T-2: on_stop with in-flight spans must close them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T2_on_stop_closes_inflight_tool_spans(
    in_memory_tracer: Any,
) -> None:
    """User abort / unexpected runner exit leaves pre_tool_use spans
    without their post counterpart. on_stop must close them so they
    don't get dropped by BatchSpanProcessor on flush."""
    handler = OtelHookHandler()
    for tcid, name in (("t1", "bash"), ("t2", "read"), ("t3", "write")):
        await handler.on_pre_tool_use(
            ToolCallEvent(tool_name=name, tool_input={}, tool_call_id=tcid)
        )
    assert len(in_memory_tracer.get_finished_spans()) == 0, (
        "spans closed prematurely; the test is no longer testing on_stop"
    )

    await handler.on_stop(StopEvent(reason="aborted"))

    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) == 3, f"expected 3 stopped spans, got {len(spans)}"
    for span in spans:
        assert (
            span.attributes.get("rolemesh.span_ended_by") == "stop_no_post"
        ), "stop-closed span missing the rolemesh.span_ended_by tag"
    assert handler._spans == {}, "handler leaked references after on_stop"


# ---------------------------------------------------------------------------
# T-3: OTLP endpoint unreachable must not block install or span emit
# ---------------------------------------------------------------------------


def test_T3_install_tracer_with_unreachable_endpoint_doesnt_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator forgets to start Langfuse / typoes the endpoint —
    install_tracer must not stall the agent main loop, and span emit
    must not block on the failed export. BatchSpanProcessor exports
    on a worker thread, so the user thread should be untouched."""
    from rolemesh.observability import tracer as _tracer_mod

    _tracer_mod._installed = False
    _tracer_mod._provider = None

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    start = time.monotonic()
    install_tracer("test-service")
    assert time.monotonic() - start < 0.5, "install_tracer blocked on endpoint"
    assert is_installed(), "endpoint set, SDK present — install_tracer should activate"

    tracer = get_tracer("test")
    span_start = time.monotonic()
    with tracer.start_as_current_span("hi") as span:
        span.set_attribute("k", "v")
    assert time.monotonic() - span_start < 0.5, (
        "span emit blocked; BatchSpanProcessor must export off-thread"
    )

    # Tear down without calling shutdown_tracer (which would block on
    # OTLP retries to the unreachable endpoint).
    _tracer_mod._installed = False
    _tracer_mod._provider = None


# ---------------------------------------------------------------------------
# T-3b: inject_trace_context inside a span must produce a real W3C carrier
# (T-7 mutation #3 guard: catches inject returning empty {} unconditionally)
# ---------------------------------------------------------------------------


def test_T3b_inject_inside_span_returns_w3c_carrier(in_memory_tracer: Any) -> None:
    tracer = get_tracer("test.inject")
    with tracer.start_as_current_span("parent"):
        carrier = inject_trace_context()
    assert "traceparent" in carrier, (
        f"W3C carrier missing 'traceparent' key; inject is broken or returning {{}} "
        f"unconditionally. Got: {carrier}"
    )
    # The traceparent value is "00-<trace_id_32hex>-<span_id_16hex>-<flags>"
    parts = carrier["traceparent"].split("-")
    assert len(parts) == 4, f"malformed traceparent: {carrier['traceparent']}"
    assert len(parts[1]) == 32, "trace_id should be 32 hex chars"
    assert len(parts[2]) == 16, "span_id should be 16 hex chars"


# ---------------------------------------------------------------------------
# T-4: trace_context=None round-trip and attach_parent_context safety
# ---------------------------------------------------------------------------


def test_T4_init_data_round_trip_with_none_trace_context() -> None:
    """Orchestrator without observability extra installed produces
    None trace_context. Container must deserialize cleanly and
    attach_parent_context must not raise."""
    init = AgentInitData(
        prompt="hi",
        group_folder="x",
        chat_jid="y",
        trace_context=None,
    )
    decoded = AgentInitData.deserialize(init.serialize())
    assert decoded.trace_context is None
    attach_parent_context(None)
    attach_parent_context({})  # empty dict variant


def test_T4b_init_data_round_trip_preserves_real_w3c_carrier() -> None:
    """If the orchestrator IS tracing, the carrier must survive the
    NATS KV serialize→deserialize hop unchanged."""
    carrier = {
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate": "rojo=00f067aa0ba902b7",
    }
    init = AgentInitData(
        prompt="hi",
        group_folder="x",
        chat_jid="y",
        trace_context=carrier,
    )
    decoded = AgentInitData.deserialize(init.serialize())
    assert decoded.trace_context == carrier
    # And the carrier is parseable by the framework
    ctx = extract_trace_context(decoded.trace_context)
    assert ctx is not None


# ---------------------------------------------------------------------------
# T-4c: shutdown_tracer must drain BatchSpanProcessor (T-7 mutation #4 guard)
# ---------------------------------------------------------------------------


def test_T4c_shutdown_tracer_flushes_batched_spans() -> None:
    """BatchSpanProcessor buffers spans until its 5s flush interval
    expires. shutdown_tracer must drain that buffer or the tail of
    every short-lived agent run is lost on container exit."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from rolemesh.observability import tracer as _tracer_mod

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "t"}))
    provider.add_span_processor(BatchSpanProcessor(exporter))

    prev_global = otel_trace._TRACER_PROVIDER
    otel_trace._TRACER_PROVIDER = provider
    prev_p = _tracer_mod._provider
    prev_i = _tracer_mod._installed
    _tracer_mod._provider = provider
    _tracer_mod._installed = True

    try:
        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("buffered"):
            pass
        # Pre-flush: BatchSpanProcessor hasn't dispatched yet.
        assert len(exporter.get_finished_spans()) == 0, (
            "BatchSpanProcessor flushed eagerly; the test premise is broken"
        )
        shutdown_tracer()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1, (
            "shutdown_tracer didn't flush; tail spans would be lost on exit. "
            "If shutdown_tracer was reduced to a noop this test fails."
        )
    finally:
        _tracer_mod._provider = prev_p
        _tracer_mod._installed = prev_i
        otel_trace._TRACER_PROVIDER = prev_global


# ---------------------------------------------------------------------------
# T-5: duplicate pre_tool_use with the same tool_call_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T5_duplicate_pre_tool_use_doesnt_leak_spans(
    in_memory_tracer: Any,
) -> None:
    """HookRegistry doesn't deduplicate. If the backend (or a retry
    upstream) double-fires pre_tool_use with the same tool_call_id,
    the handler must end the orphaned first span rather than overwrite
    its dict entry and lose the reference."""
    handler = OtelHookHandler()
    await handler.on_pre_tool_use(
        ToolCallEvent(
            tool_name="bash", tool_input={"cmd": "ls"}, tool_call_id="dup-1"
        )
    )
    await handler.on_pre_tool_use(
        ToolCallEvent(
            tool_name="bash", tool_input={"cmd": "pwd"}, tool_call_id="dup-1"
        )
    )

    finished = in_memory_tracer.get_finished_spans()
    assert len(finished) == 1, (
        "duplicate pre should have ended the first span; "
        f"got {len(finished)} finished, {len(handler._spans)} in-flight"
    )
    assert (
        finished[0].attributes.get("rolemesh.span_ended_by")
        == "duplicate_pre_tool_use"
    )
    assert len(handler._spans) == 1, "exactly one span should remain in-flight"

    # Closing the second span normally completes the pair
    await handler.on_post_tool_use(
        ToolResultEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_result="/",
            tool_call_id="dup-1",
        )
    )
    assert len(in_memory_tracer.get_finished_spans()) == 2
    assert handler._spans == {}


# ---------------------------------------------------------------------------
# T-6: orphan post_tool_use_failure / post_tool_use without matching pre
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_T6_orphan_post_failure_creates_no_span(
    in_memory_tracer: Any,
) -> None:
    """A post for an unknown id must NOT manufacture a fake span
    (which would render in Langfuse as a tool that mysteriously
    materialised at end time with no start) and must NOT raise."""
    handler = OtelHookHandler()
    await handler.on_post_tool_use_failure(
        ToolResultEvent(
            tool_name="bash",
            tool_input={"cmd": "ls"},
            tool_result="error",
            is_error=True,
            tool_call_id="ghost-id",
        )
    )
    assert len(in_memory_tracer.get_finished_spans()) == 0
    assert handler._spans == {}


@pytest.mark.asyncio
async def test_T6b_orphan_post_success_creates_no_span(
    in_memory_tracer: Any,
) -> None:
    """Same invariant on the success path."""
    handler = OtelHookHandler()
    await handler.on_post_tool_use(
        ToolResultEvent(
            tool_name="bash",
            tool_input={"cmd": "ls"},
            tool_result="ok",
            tool_call_id="ghost-success",
        )
    )
    assert len(in_memory_tracer.get_finished_spans()) == 0
    assert handler._spans == {}


# ---------------------------------------------------------------------------
# Noop-mode integrity: with no SDK / no endpoint, every helper must
# be silent and side-effect-free. This guards FR-1's zero-impact
# promise — without it, an opt-out user could see installation-only
# regressions that mirror tests would never catch.
# ---------------------------------------------------------------------------


def test_noop_mode_handlers_dont_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tracer installed: hook handler + claude.message helper
    must be no-ops. Critical because the brief permits unconditional
    registration of OtelHookHandler in production."""
    from rolemesh.observability import tracer as _tracer_mod

    _tracer_mod._installed = False
    _tracer_mod._provider = None

    handler = OtelHookHandler()
    import asyncio

    async def _drive() -> None:
        await handler.on_pre_tool_use(
            ToolCallEvent(tool_name="x", tool_input={}, tool_call_id="a")
        )
        await handler.on_post_tool_use(
            ToolResultEvent(
                tool_name="x", tool_input={}, tool_result="ok", tool_call_id="a"
            )
        )
        await handler.on_stop(StopEvent(reason="completed"))

    asyncio.run(_drive())
    # And the claude.message synthetic span helper
    _emit_claude_message_span(
        ResultEvent(text="hi", usage=None, is_final=True)
    )
