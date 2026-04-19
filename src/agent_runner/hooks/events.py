"""Unified hook event types shared by both Claude SDK and Pi backends.

Backend-specific event/verdict shapes (SDK hook payloads, Pi
ToolCallEventResult etc.) are translated at the bridge layer — handlers
registered on HookRegistry only see these neutral dataclasses so the
same handler code runs unchanged against either backend.

PostToolUse design note: the verdict here only exposes "append context",
NOT "replace result". Claude SDK's PostToolUse hook only supports
additionalContext, while Pi's tool_result hook can replace content. Giving
handlers a replace-result capability that silently no-ops on Claude would
produce a DLP handler that looks effective in tests but lets secrets leak
in production. The asymmetry is resolved by deliberately exposing the
lowest-common-denominator surface — callers that need replacement must
either block at PreToolUse or modify tool_input there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallEvent:
    tool_name: str
    tool_input: dict[str, Any]
    tool_call_id: str = ""


@dataclass(frozen=True)
class ToolCallVerdict:
    block: bool = False
    reason: str | None = None
    modified_input: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# PostToolUse / PostToolUseFailure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResultEvent:
    tool_name: str
    tool_input: dict[str, Any]
    tool_result: str
    is_error: bool = False
    tool_call_id: str = ""


@dataclass(frozen=True)
class ToolResultVerdict:
    """Result of a PostToolUse handler.

    `appended_context=None` means "do not append anything". There is
    deliberately no modified_result field — see module docstring.
    """

    appended_context: str | None = None


# ---------------------------------------------------------------------------
# PreCompact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionEvent:
    """Union payload for PreCompact across backends.

    Claude provides a transcript_path on disk. Pi provides an in-memory
    list of AgentMessage objects from CompactionPreparation.
    messages_to_summarize. Handlers branch on which field is populated.
    Typed as Any to avoid cross-package import of pi.agent types from the
    hooks package.
    """

    transcript_path: str | None = None
    session_id: str | None = None
    messages: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserPromptEvent:
    prompt: str


@dataclass(frozen=True)
class UserPromptVerdict:
    block: bool = False
    reason: str | None = None
    appended_context: str | None = None


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopEvent:
    """Fires exactly once per run_prompt / abort cycle.

    Reason values:
      - "completed": run_prompt returned normally
      - "aborted":   abort() was awaited to completion
      - "error":     run_prompt raised an unrecoverable error
    """

    reason: str
    session_id: str | None = None
