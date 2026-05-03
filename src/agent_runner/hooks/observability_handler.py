"""OTel-backed hook handler — emits one span per tool call.

Registered on HookRegistry so both Claude SDK and Pi backends fan
out to it through the same neutral ``ToolCallEvent`` /
``ToolResultEvent`` types. When ``rolemesh.observability`` is in
noop mode (extra not installed or endpoint not set), every span
operation here is a no-op, so the handler is safe to register
unconditionally.

The handler must never raise from ``on_pre_tool_use``: that is a
fail-CLOSE hook in HookRegistry, so a thrown exception here would
block the agent's tool call. All operations are wrapped in
try/except for that reason.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger
from rolemesh.observability import get_tracer

if TYPE_CHECKING:
    from .events import (
        StopEvent,
        ToolCallEvent,
        ToolCallVerdict,
        ToolResultEvent,
        ToolResultVerdict,
    )

logger = get_logger()

# Truncation limits keep span attributes small (Langfuse / OTel
# backends choke on multi-MB attributes). Inputs are usually a
# small dict; results can be a wall of file content. The asymmetry
# (200 vs 500) reflects that.
_INPUT_PREVIEW_MAX = 200
_RESULT_PREVIEW_MAX = 500


def _truncate(value: object, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            s = repr(value)
    if len(s) > limit:
        return s[:limit] + "...(truncated)"
    return s


class OtelHookHandler:
    """Emits one OTel span per tool call.

    Span lifecycle is keyed on ``tool_call_id``:

    - ``on_pre_tool_use`` opens a span and stashes it in ``_spans``
    - ``on_post_tool_use`` / ``on_post_tool_use_failure`` close it
    - ``on_stop`` closes any span still in flight (fault path —
      e.g. user-aborted turn or unexpected runner exit)

    Tool calls without a ``tool_call_id`` cannot be correlated to
    their post event, so the pre handler closes them immediately
    rather than leak. That trades visibility (we lose the result)
    for non-leakiness — and most backends do populate the id.
    """

    def __init__(self) -> None:
        self._tracer = get_tracer("rolemesh.agent_runner")
        self._spans: dict[str, Any] = {}

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        try:
            self._open_span(event)
        except Exception:
            # pre_tool_use is fail-CLOSE in HookRegistry; if we propagate
            # we'd block the agent's tool call. Log and swallow.
            logger.exception(
                "OtelHookHandler.on_pre_tool_use failed; tool span dropped"
            )
        return None

    def _open_span(self, event: ToolCallEvent) -> None:
        tcid = event.tool_call_id or ""
        # Duplicate pre with the same id (T-5): end the orphaned
        # span so we don't leak. Tag the cause so the operator can
        # spot the double-fire pattern in the trace tree.
        if tcid and tcid in self._spans:
            old = self._spans.pop(tcid)
            try:
                old.set_attribute(
                    "rolemesh.span_ended_by", "duplicate_pre_tool_use"
                )
                old.end()
            except Exception:  # noqa: BLE001 — fail-safe by design
                pass
        span = self._tracer.start_span(f"tool_call:{event.tool_name}")
        try:
            span.set_attribute("rolemesh.tool_name", event.tool_name)
            span.set_attribute("rolemesh.tool_call_id", tcid)
            span.set_attribute(
                "rolemesh.tool_input_preview",
                _truncate(event.tool_input, _INPUT_PREVIEW_MAX),
            )
        except Exception:
            # Attribute set failures are non-fatal — the span itself is
            # still valid. Log so we notice systematic SDK regressions.
            logger.exception("OtelHookHandler set_attribute failed")
        if tcid:
            self._spans[tcid] = span
        else:
            try:
                span.set_attribute("rolemesh.span_ended_by", "no_tool_call_id")
                span.end()
            except Exception:  # noqa: BLE001 — fail-safe by design
                pass

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        self._end_span(event, is_error=event.is_error)
        return None

    async def on_post_tool_use_failure(
        self, event: ToolResultEvent
    ) -> None:
        self._end_span(event, is_error=True)

    async def on_stop(self, event: StopEvent) -> None:
        # Close any leftover spans so BatchSpanProcessor flushes them
        # alongside the rest of the trace. Without this, an aborted
        # turn leaves spans dangling in memory until the process
        # exits, and the tail is dropped.
        for _tcid, span in list(self._spans.items()):
            try:
                span.set_attribute("rolemesh.span_ended_by", "stop_no_post")
                span.end()
            except Exception:  # noqa: BLE001 — fail-safe by design
                pass
        self._spans.clear()

    def _end_span(self, event: ToolResultEvent, is_error: bool) -> None:
        tcid = event.tool_call_id or ""
        if not tcid or tcid not in self._spans:
            # Orphan post (T-6): backend dispatched a post without a
            # matching pre, or with an id we never saw. Log at debug
            # so the common Pi/Claude divergence doesn't drown the
            # operator in warnings.
            logger.debug(
                "OtelHookHandler: post for unknown tool_call_id",
                tool_call_id=tcid,
                tool_name=event.tool_name,
            )
            return
        span = self._spans.pop(tcid)
        try:
            span.set_attribute("rolemesh.is_error", bool(is_error))
            span.set_attribute(
                "rolemesh.tool_result_preview",
                _truncate(event.tool_result, _RESULT_PREVIEW_MAX),
            )
            span.end()
        except Exception:
            logger.exception(
                "OtelHookHandler._end_span failed; tool span may be incomplete"
            )
