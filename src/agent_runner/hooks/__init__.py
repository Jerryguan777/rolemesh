"""Unified hook system for agent backends.

Provides a backend-neutral HookRegistry that both Claude SDK and Pi
backends bridge into. Supported events:

  - PreToolUse: control — can block a tool call or modify its input
    (Pi degrades modified_input to a warning; modified_input only fully
    works on Claude SDK)
  - PostToolUse: observation + append — can append context to a tool
    result but cannot replace the result text (see events.py docstring)
  - PostToolUseFailure: observation only; cannot unblock a failed call
  - PreCompact: side effect before the backend compacts transcripts
  - UserPromptSubmit: control — can block or append context to a prompt
  - Stop: notification emitted once per run_prompt/abort end; does not
    map to Claude SDK's Stop hook (whose semantics are different) and
    does not map to Pi's agent_end event

Exception policy:
  - Control hooks (PreToolUse, UserPromptSubmit): fail-close. Handler
    exceptions propagate and bridges turn them into block verdicts.
  - Observational hooks: fail-safe. Exceptions are logged and swallowed.

Handlers implement a subset of the HookHandler protocol — undefined
methods are treated as no-ops by the registry.
"""

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
from .registry import HookHandler, HookRegistry

__all__ = [
    "CompactionEvent",
    "HookHandler",
    "HookRegistry",
    "StopEvent",
    "ToolCallEvent",
    "ToolCallVerdict",
    "ToolResultEvent",
    "ToolResultVerdict",
    "UserPromptEvent",
    "UserPromptVerdict",
]
