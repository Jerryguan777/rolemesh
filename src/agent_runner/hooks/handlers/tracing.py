"""Hook handler that turns tool dispatch into OTel spans.

Plugs into ``HookRegistry`` so the Claude SDK and Pi backends both go
through one wrap point — both call ``emit_pre_tool_use`` /
``emit_post_tool_use`` /``emit_post_tool_use_failure`` on the same
registry, so this handler covers them both without backend-specific
code.

Span lifecycle:
  1. on_pre_tool_use — open a span keyed by ``tool_call_id`` (or a
     fallback per-tool counter when the backend doesn't supply one),
     attach tool name + input preview as attributes.
  2. on_post_tool_use — close the span with ``status=ok`` and the
     result preview.
  3. on_post_tool_use_failure — close the span with ``status=error``
     and the error string + ``record_exception`` semantics.

Why open/close instead of ``with ... as span``: pre/post are
separate hook callbacks that can be many seconds apart (the tool
runs between them), so we can't use a context manager. Standard
OTel pattern: ``tracer.start_span(...)`` then ``span.end()``.

Cleanup: every Open span has a finite lifetime — either post / post-
failure fires (normal path), or the run aborts. on_stop catches the
abort case and force-ends any leftover spans so we don't leak open
spans across runs.

Behaviour with OTel disabled: ``get_tracer`` returns a noop tracer,
``start_span`` returns a noop span, every method below is effectively
a dict insert + dict pop. Cost is negligible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rolemesh.observability import get_tracer

if TYPE_CHECKING:
    from ..events import (
        StopEvent,
        ToolCallEvent,
        ToolResultEvent,
    )

_INPUT_PREVIEW_MAX = 200
_RESULT_PREVIEW_MAX = 500


def _preview(value: object, limit: int) -> str:
    """Stringify and truncate. Mirrors agent_runner.tools.preview but
    inlined to avoid a cross-package import. Truncated to keep span
    attributes within OTLP's per-attribute byte budget — Langfuse +
    Phoenix render large attrs but the network cost climbs fast on
    high-frequency tool calls.
    """
    s = str(value) if not isinstance(value, str) else value
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


class TracingHookHandler:
    """Backend-agnostic span emitter for tool dispatch.

    Lives on ``HookRegistry`` alongside Approval and Safety handlers.
    Default behaviour is noop until ``install_tracer`` configures a
    real OTel provider, so registering the handler unconditionally
    is safe.
    """

    def __init__(self) -> None:
        # One tracer instance per process. ``get_tracer`` is cheap to
        # call repeatedly but caching here keeps call sites concise.
        self._tracer = get_tracer("rolemesh.agent.tools")
        # tool_call_id -> open span. We hold the span object directly
        # rather than a context manager because pre/post are two
        # callbacks separated by the actual tool execution; ``with``
        # blocks don't span those.
        self._open_spans: dict[str, Any] = {}
        # Counter used only when the backend doesn't supply a
        # tool_call_id. Both Claude SDK and Pi do supply one in
        # practice (claude_backend.py:178, pi_backend.py:147), but
        # stay defensive against future backend additions.
        self._fallback_counter: int = 0

    def _key(self, event: ToolCallEvent | ToolResultEvent) -> str:
        if event.tool_call_id:
            return event.tool_call_id
        # Synthetic key falls back to ``tool_name``. This is wrong for
        # parallel calls of the same tool but is the best we can do
        # without an id from the backend.
        self._fallback_counter += 1
        return f"_anon:{event.tool_name}:{self._fallback_counter}"

    async def on_pre_tool_use(self, event: ToolCallEvent) -> None:
        """Open a span when a tool call is about to dispatch.

        Returns None — this handler does not block and does not
        modify input. ``HookRegistry.emit_pre_tool_use`` treats
        ``None`` as "no opinion", so the verdict chain is unaffected.
        """
        key = event.tool_call_id or self._key(event)
        span = self._tracer.start_span(
            f"tool_call:{event.tool_name}",
            attributes={
                # Use the OpenInference / OTel-GenAI semantic-convention
                # keys where applicable so Langfuse / Phoenix UIs render
                # this span as a "tool call" without custom mapping.
                "tool.name": event.tool_name,
                "tool.input.preview": _preview(event.tool_input, _INPUT_PREVIEW_MAX),
                "rolemesh.tool_call_id": event.tool_call_id or key,
            },
        )
        self._open_spans[key] = span

    async def on_post_tool_use(self, event: ToolResultEvent) -> None:
        """Close a span on the normal-completion path."""
        span = self._open_spans.pop(event.tool_call_id, None)
        if span is None:
            return
        # Defer Status/StatusCode imports to keep this module importable
        # without the OTel SDK on the path. set_status accepts a string
        # via the SDK's overload, but a noop span ignores it either way.
        try:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.OK))
        except ImportError:
            pass
        span.set_attribute(
            "tool.result.preview", _preview(event.tool_result, _RESULT_PREVIEW_MAX)
        )
        span.set_attribute("tool.is_error", bool(event.is_error))
        span.end()

    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        """Close a span on the failure path. Records the failure as a
        span event so backends that surface "exceptions on a span"
        (Langfuse, Tempo, Jaeger) light it up in the UI.
        """
        span = self._open_spans.pop(event.tool_call_id, None)
        if span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(
                Status(StatusCode.ERROR, description=_preview(event.tool_result, 200))
            )
        except ImportError:
            pass
        span.set_attribute(
            "tool.result.preview", _preview(event.tool_result, _RESULT_PREVIEW_MAX)
        )
        span.set_attribute("tool.is_error", True)
        span.end()

    async def on_stop(self, event: StopEvent) -> None:
        """Force-end any leftover open spans on run termination.

        Ensures we never leak across runs if a backend unexpectedly
        skipped post_tool_use (shouldn't happen in steady state, but
        the cost of being defensive is one dict.clear()).
        """
        for span in self._open_spans.values():
            try:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(
                    Status(StatusCode.ERROR, description=f"run stopped: {event.reason}")
                )
            except ImportError:
                pass
            span.end()
        self._open_spans.clear()
