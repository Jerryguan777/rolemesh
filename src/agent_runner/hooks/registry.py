"""HookRegistry — the fan-out point for unified hooks.

Exception-isolation policy is asymmetric, and the asymmetry is intentional:

- Control hooks (pre_tool_use, user_prompt_submit): fail-close. If a handler
  raises, the exception propagates out of emit_*. The backend bridge MUST
  translate that into a block verdict for the agent — an audit handler that
  hit a DB outage must not silently default to "allow".

- Observational hooks (post_tool_use, post_tool_use_failure, pre_compact,
  stop): fail-safe. Each handler is try/except-wrapped so one broken
  handler cannot cascade into either a peer handler or the agent turn.

The bridge layer is responsible for translating raised exceptions from
control hooks into the backend-native block response. HookRegistry itself
does not know about SDK-specific return shapes.
"""

from __future__ import annotations

import logging
from typing import Protocol, cast

from .events import (
    CompactionEvent,
    StopEvent,
    ToolCallEvent,
    ToolCallVerdict,
    ToolResultEvent,
    ToolResultVerdict,
    UserPromptEvent,
    UserPromptVerdict,
)

_log = logging.getLogger(__name__)


class HookHandler(Protocol):
    """Protocol every handler must satisfy.

    Handlers may implement a subset — HookRegistry tolerates missing
    methods by treating them as no-ops. A handler that only cares about
    PreCompact can define only on_pre_compact.
    """

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None: ...
    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None: ...
    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None: ...
    async def on_pre_compact(self, event: CompactionEvent) -> None: ...
    async def on_user_prompt_submit(
        self, event: UserPromptEvent
    ) -> UserPromptVerdict | None: ...
    async def on_stop(self, event: StopEvent) -> None: ...


class HookRegistry:
    def __init__(self) -> None:
        self._handlers: list[object] = []

    def register(self, handler: object) -> None:
        """Register a handler.

        The parameter is typed as `object` on purpose: handlers implement
        only the subset of HookHandler methods they care about (via duck
        typing — the registry uses getattr() to tolerate missing methods).
        Typing this as `HookHandler` would force every handler to provide
        all six methods to satisfy structural subtyping, defeating the
        "handler implements only what it needs" contract.
        """
        self._handlers.append(handler)

    def __bool__(self) -> bool:
        return bool(self._handlers)

    # -- Control hooks: fail-close ------------------------------------

    async def emit_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        """Dispatch PreToolUse. Handler exceptions propagate to the caller.

        Returns:
          - block verdict on the first handler that blocks (short-circuit)
          - chained modified_input verdict if any handler modifies
          - None if no handler returned anything actionable
        """
        current_input = event.tool_input
        modified = False
        for h in self._handlers:
            fn = getattr(h, "on_pre_tool_use", None)
            if fn is None:
                continue
            probe_event = ToolCallEvent(
                tool_name=event.tool_name,
                tool_input=current_input,
                tool_call_id=event.tool_call_id,
            )
            verdict = cast("ToolCallVerdict | None", await fn(probe_event))
            if verdict is None:
                continue
            if verdict.block:
                return verdict
            if verdict.modified_input is not None:
                current_input = verdict.modified_input
                modified = True
        if modified:
            return ToolCallVerdict(block=False, modified_input=current_input)
        return None

    async def emit_user_prompt_submit(
        self, event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        """Dispatch UserPromptSubmit. Handler exceptions propagate."""
        appended: list[str] = []
        for h in self._handlers:
            fn = getattr(h, "on_user_prompt_submit", None)
            if fn is None:
                continue
            verdict = cast("UserPromptVerdict | None", await fn(event))
            if verdict is None:
                continue
            if verdict.block:
                return verdict
            if verdict.appended_context:
                appended.append(verdict.appended_context)
        if appended:
            return UserPromptVerdict(appended_context="\n\n".join(appended))
        return None

    # -- Observational hooks: fail-safe -------------------------------

    async def emit_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        appended: list[str] = []
        for h in self._handlers:
            fn = getattr(h, "on_post_tool_use", None)
            if fn is None:
                continue
            try:
                verdict = await fn(event)
                if verdict and verdict.appended_context:
                    appended.append(verdict.appended_context)
            except Exception as exc:  # noqa: BLE001 — fail-safe by design
                _log.warning("post_tool_use handler failed: %s", exc)
        if appended:
            return ToolResultVerdict(appended_context="\n\n".join(appended))
        return None

    async def emit_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        for h in self._handlers:
            fn = getattr(h, "on_post_tool_use_failure", None)
            if fn is None:
                continue
            try:
                await fn(event)
            except Exception as exc:  # noqa: BLE001 — fail-safe by design
                _log.warning("post_tool_use_failure handler failed: %s", exc)

    async def emit_pre_compact(self, event: CompactionEvent) -> None:
        for h in self._handlers:
            fn = getattr(h, "on_pre_compact", None)
            if fn is None:
                continue
            try:
                await fn(event)
            except Exception as exc:  # noqa: BLE001 — fail-safe by design
                _log.warning("pre_compact handler failed: %s", exc)

    async def emit_stop(self, event: StopEvent) -> None:
        for h in self._handlers:
            fn = getattr(h, "on_stop", None)
            if fn is None:
                continue
            try:
                await fn(event)
            except Exception as exc:  # noqa: BLE001 — fail-safe by design
                _log.warning("stop handler failed: %s", exc)
