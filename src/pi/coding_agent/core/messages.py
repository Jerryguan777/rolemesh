"""Message types — Python port of packages/coding-agent/src/core/messages.ts.

Custom message types that extend the core AgentMessage union. These types
carry additional metadata that is not part of the standard LLM message set.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pi.agent.types import AgentMessage
from pi.ai.types import (
    ImageContent,
    Message,
    TextContent,
    UserMessage,
)

# ---------------------------------------------------------------------------
# Constants for compaction / branch summary delimiters
# ---------------------------------------------------------------------------

COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"

BRANCH_SUMMARY_PREFIX = "The following is a summary of a branch that this conversation came back from:\n\n<summary>\n"
BRANCH_SUMMARY_SUFFIX = "</summary>"

# snake_case aliases for parity with TS camelCase exports
branch_summary_prefix = BRANCH_SUMMARY_PREFIX
branch_summary_suffix = BRANCH_SUMMARY_SUFFIX
compaction_summary_prefix = COMPACTION_SUMMARY_PREFIX
compaction_summary_suffix = COMPACTION_SUMMARY_SUFFIX


# ---------------------------------------------------------------------------
# Custom message dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BashExecutionMessage:
    """Records a bash command execution result in the session.

    Maps to TS BashExecutionMessage but uses separate stdout/stderr fields
    instead of a combined output field, as required by the shared interface
    contract (issue #26).
    """

    role: Literal["bash_execution"] = "bash_execution"
    command: str = ""
    # stdout contains the combined output (stdout+stderr) from the bash
    # executor, to match the TS 'output' field semantics.
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    timestamp: float = field(default_factory=lambda: time.time() * 1000)
    exclude_from_context: bool | None = None


@dataclass
class CustomMessage:
    """Extension-injected message that participates in LLM context."""

    role: Literal["custom"] = "custom"
    custom_type: str = ""
    content: str | list[TextContent | ImageContent] = ""
    display: bool = True
    details: Any = None
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


@dataclass
class BranchSummaryMessage:
    """Summary injected when returning from a conversation branch."""

    role: Literal["branch_summary"] = "branch_summary"
    summary: str = ""
    from_id: str = ""
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


@dataclass
class CompactionSummaryMessage:
    """Summary injected at compaction boundary."""

    role: Literal["compaction_summary"] = "compaction_summary"
    summary: str = ""
    tokens_before: int = 0
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


# ---------------------------------------------------------------------------
# Extended message type alias (includes all custom types)
# ---------------------------------------------------------------------------

# SessionMessage is a superset of AgentMessage that also includes the custom
# message types used by the coding agent (bash execution, compaction, etc.).
SessionMessage = AgentMessage | BashExecutionMessage | CustomMessage | BranchSummaryMessage | CompactionSummaryMessage


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def bash_execution_to_text(msg: BashExecutionMessage) -> str:
    """Format a bash execution message as human-readable text."""
    lines: list[str] = [f"$ {msg.command}"]
    if msg.stdout:
        lines.append(msg.stdout)
    if msg.stderr:
        lines.append(msg.stderr)
    if msg.exit_code is not None:
        lines.append(f"[exit {msg.exit_code}]")
    if msg.cancelled:
        lines.append("[cancelled]")
    if msg.truncated:
        lines.append("[output truncated]")
    return "\n".join(lines)


def create_branch_summary_message(
    summary: str,
    from_id: str,
    timestamp: str,
) -> BranchSummaryMessage:
    """Create a BranchSummaryMessage from serialized data."""
    ts = _parse_timestamp(timestamp)
    return BranchSummaryMessage(summary=summary, from_id=from_id, timestamp=ts)


def create_compaction_summary_message(
    summary: str,
    tokens_before: int,
    timestamp: str,
) -> CompactionSummaryMessage:
    """Create a CompactionSummaryMessage from serialized data."""
    ts = _parse_timestamp(timestamp)
    return CompactionSummaryMessage(summary=summary, tokens_before=tokens_before, timestamp=ts)


def create_custom_message(
    custom_type: str,
    content: str | list[TextContent | ImageContent],
    display: bool,
    details: Any,
    timestamp: str,
) -> CustomMessage:
    """Create a CustomMessage from serialized data."""
    ts = _parse_timestamp(timestamp)
    return CustomMessage(
        custom_type=custom_type,
        content=content,
        display=display,
        details=details,
        timestamp=ts,
    )


def _parse_timestamp(timestamp: str) -> float:
    """Parse an ISO timestamp string to milliseconds since epoch."""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.timestamp() * 1000
    except (ValueError, AttributeError):
        return time.time() * 1000


# ---------------------------------------------------------------------------
# LLM conversion
# ---------------------------------------------------------------------------


def convert_to_llm(messages: list[SessionMessage]) -> list[Message]:
    """Convert agent messages to LLM-compatible messages.

    Filters out non-LLM messages (BashExecutionMessage, CustomMessage that
    have display=False etc.) and injects compaction/branch summaries as
    user messages so the LLM sees them.
    """
    result: list[Message] = []

    for msg in messages:
        if isinstance(msg, CompactionSummaryMessage):
            # Inject as user message: prefix + summary + suffix
            text = f"{COMPACTION_SUMMARY_PREFIX}{msg.summary}{COMPACTION_SUMMARY_SUFFIX}"
            result.append(UserMessage(content=text, timestamp=msg.timestamp))
        elif isinstance(msg, BranchSummaryMessage):
            # Inject as user message: prefix + summary + suffix
            text = f"{BRANCH_SUMMARY_PREFIX}{msg.summary}{BRANCH_SUMMARY_SUFFIX}"
            result.append(UserMessage(content=text, timestamp=msg.timestamp))
        elif isinstance(msg, CustomMessage):
            # Custom messages go to the LLM as user messages
            result.append(UserMessage(content=msg.content, timestamp=msg.timestamp))
        elif isinstance(msg, BashExecutionMessage):
            # Skip messages excluded from context (!! prefix); include the rest
            if msg.exclude_from_context:
                continue
            text = bash_execution_to_text(msg)
            result.append(UserMessage(content=text, timestamp=msg.timestamp))
        else:
            # Standard LLM messages (user, assistant, toolResult)
            result.append(msg)

    return result
