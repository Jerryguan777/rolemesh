"""Context compaction for long sessions.

Port of packages/coding-agent/src/core/compaction/compaction.ts.

Pure functions for compaction logic. The session manager handles I/O,
and after compaction the session is reloaded.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pi.agent.types import AgentMessage
from pi.ai.stream import complete_simple
from pi.ai.types import Model, Usage

from .utils import (
    SUMMARIZATION_SYSTEM_PROMPT,
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)

# ============================================================================
# Types
# ============================================================================


@dataclass
class CompactionDetails:
    """Details stored in CompactionEntry.details for file tracking."""

    read_files: list[str]
    modified_files: list[str]


@dataclass
class CompactionResult:
    """Result from compact() - SessionManager adds uuid/parentUuid when saving."""

    summary: str
    first_kept_entry_id: str
    tokens_before: int
    details: Any | None = None


@dataclass
class CompactionSettings:
    """Settings controlling when and how compaction occurs."""

    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


@dataclass
class ContextUsageEstimate:
    """Estimated context usage for a list of messages."""

    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None


@dataclass
class CutPointResult:
    """Result of finding a cut point in session entries."""

    first_kept_entry_index: int
    turn_start_index: int
    is_split_turn: bool


@dataclass
class CompactionPreparation:
    """Pre-calculated data for compaction, computed by prepare_compaction()."""

    first_kept_entry_id: str
    messages_to_summarize: list[AgentMessage]
    turn_prefix_messages: list[AgentMessage]
    is_split_turn: bool
    tokens_before: int
    file_ops: FileOperations
    settings: CompactionSettings
    previous_summary: str | None = None


# ============================================================================
# Token calculation
# ============================================================================


def calculate_context_tokens(usage: Usage) -> int:
    """Calculate total context tokens from usage.

    Uses the native total_tokens field when available, falls back to computing
    from components.
    """
    total = getattr(usage, "total_tokens", 0)
    if total:
        return total
    return (
        getattr(usage, "input", 0)
        + getattr(usage, "output", 0)
        + getattr(usage, "cache_read", 0)
        + getattr(usage, "cache_write", 0)
    )


def _get_assistant_usage(msg: AgentMessage) -> Usage | None:
    """Get usage from an assistant message if available.

    Skips aborted and error messages as they don't have valid usage data.
    """
    if not hasattr(msg, "role") or msg.role != "assistant":
        return None
    stop_reason = getattr(msg, "stop_reason", None)
    if stop_reason in ("aborted", "error"):
        return None
    usage = getattr(msg, "usage", None)
    return usage if usage is not None else None


def get_last_assistant_usage(entries: list[Any]) -> Usage | None:
    """Find the last non-aborted assistant message usage from session entries."""
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if isinstance(entry, dict) and entry.get("type") == "message":
            msg = entry.get("message")
        else:
            msg = getattr(entry, "message", None) if getattr(entry, "type", None) == "message" else None

        if msg is not None:
            usage = _get_assistant_usage(msg)
            if usage:
                return usage
    return None


def _get_last_assistant_usage_info(
    messages: list[AgentMessage],
) -> tuple[Usage, int] | None:
    """Get last assistant usage and its index from a message list."""
    for i in range(len(messages) - 1, -1, -1):
        usage = _get_assistant_usage(messages[i])
        if usage:
            return usage, i
    return None


def estimate_context_tokens(messages: list[AgentMessage]) -> ContextUsageEstimate:
    """Estimate context tokens from messages, using the last assistant usage when available.

    If there are messages after the last usage, estimate their tokens with estimate_tokens.
    """
    usage_info = _get_last_assistant_usage_info(messages)

    if not usage_info:
        estimated = sum(estimate_tokens(m) for m in messages)
        return ContextUsageEstimate(
            tokens=estimated,
            usage_tokens=0,
            trailing_tokens=estimated,
            last_usage_index=None,
        )

    usage, idx = usage_info
    usage_tokens = calculate_context_tokens(usage)
    trailing_tokens = sum(estimate_tokens(messages[i]) for i in range(idx + 1, len(messages)))
    return ContextUsageEstimate(
        tokens=usage_tokens + trailing_tokens,
        usage_tokens=usage_tokens,
        trailing_tokens=trailing_tokens,
        last_usage_index=idx,
    )


def should_compact(
    context_tokens: int,
    context_window: int,
    settings: CompactionSettings,
) -> bool:
    """Check if compaction should trigger based on context usage."""
    if not settings.enabled:
        return False
    return context_tokens > context_window - settings.reserve_tokens


# ============================================================================
# Cut point detection
# ============================================================================


def estimate_tokens(message: AgentMessage) -> int:
    """Estimate token count for a message using chars/4 heuristic.

    This is conservative (overestimates tokens).
    """
    chars = 0
    role = getattr(message, "role", "")

    if role == "user":
        content = getattr(message, "content", "")
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            for block in content:
                block_type = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
                if block_type == "text":
                    text = getattr(block, "text", "") if not isinstance(block, dict) else block.get("text", "")
                    chars += len(text or "")
        return (chars + 3) // 4

    elif role == "assistant":
        content = getattr(message, "content", [])
        for block in content if isinstance(content, list) else []:
            block_type = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
            if block_type == "text":
                text = getattr(block, "text", "") if not isinstance(block, dict) else block.get("text", "")
                chars += len(text or "")
            elif block_type == "thinking":
                thinking = getattr(block, "thinking", "") if not isinstance(block, dict) else block.get("thinking", "")
                chars += len(thinking or "")
            elif block_type == "toolCall":
                name = getattr(block, "name", "") if not isinstance(block, dict) else block.get("name", "")
                args = getattr(block, "arguments", {}) if not isinstance(block, dict) else block.get("arguments", {})
                chars += len(name or "") + len(str(args))
        return (chars + 3) // 4

    elif role in ("custom", "toolResult"):
        content = getattr(message, "content", [])
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            for block in content:
                block_type = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
                if block_type == "text":
                    text = getattr(block, "text", "") if not isinstance(block, dict) else block.get("text", "")
                    chars += len(text or "")
                elif block_type == "image":
                    chars += 4800  # Estimate images as ~1200 tokens
        return (chars + 3) // 4

    elif role == "bash_execution":
        chars = len(getattr(message, "command", "")) + len(getattr(message, "stdout", ""))
        return (chars + 3) // 4

    elif role in ("branch_summary", "compaction_summary"):
        chars = len(getattr(message, "summary", ""))
        return (chars + 3) // 4

    return 0


def _find_valid_cut_points(entries: list[Any], start_index: int, end_index: int) -> list[int]:
    """Find indices that are valid cut points (user/assistant/custom/bash, not tool results)."""
    cut_points: list[int] = []
    for i in range(start_index, end_index):
        entry = entries[i]
        entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)

        if entry_type == "message":
            msg = entry.get("message") if isinstance(entry, dict) else getattr(entry, "message", None)
            if msg is not None:
                msg_role = getattr(msg, "role", None)
                if msg_role in ("bash_execution", "custom", "branch_summary", "compaction_summary", "user", "assistant"):
                    cut_points.append(i)
                # toolResult: not a valid cut point

        elif entry_type in ("branch_summary", "custom_message"):
            cut_points.append(i)

    return cut_points


def find_turn_start_index(entries: list[Any], entry_index: int, start_index: int) -> int:
    """Find the user message (or bash_execution) that starts the turn containing the given entry index.

    Returns -1 if no turn start found before the index.
    """
    for i in range(entry_index, start_index - 1, -1):
        entry = entries[i]
        entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)

        if entry_type in ("branch_summary", "custom_message"):
            return i

        if entry_type == "message":
            msg = entry.get("message") if isinstance(entry, dict) else getattr(entry, "message", None)
            if msg is not None:
                role = getattr(msg, "role", None)
                if role in ("user", "bash_execution"):
                    return i

    return -1


def find_cut_point(
    entries: list[Any],
    start_index: int,
    end_index: int,
    keep_recent_tokens: int,
) -> CutPointResult:
    """Find the cut point in session entries that keeps approximately keep_recent_tokens.

    Algorithm: Walk backwards from newest, accumulating estimated message sizes.
    Stop when we've accumulated >= keep_recent_tokens. Cut at that point.
    """
    cut_points = _find_valid_cut_points(entries, start_index, end_index)

    if not cut_points:
        return CutPointResult(
            first_kept_entry_index=start_index,
            turn_start_index=-1,
            is_split_turn=False,
        )

    accumulated_tokens = 0
    cut_index = cut_points[0]  # Default: keep from first message

    for i in range(end_index - 1, start_index - 1, -1):
        entry = entries[i]
        entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)

        if entry_type != "message":
            continue

        msg = entry.get("message") if isinstance(entry, dict) else getattr(entry, "message", None)
        if msg is None:
            continue

        message_tokens = estimate_tokens(msg)
        accumulated_tokens += message_tokens

        if accumulated_tokens >= keep_recent_tokens:
            for c in range(len(cut_points)):
                if cut_points[c] >= i:
                    cut_index = cut_points[c]
                    break
            break

    # Scan backwards from cut_index to include non-message entries
    while cut_index > start_index:
        prev_entry = entries[cut_index - 1]
        prev_type = prev_entry.get("type") if isinstance(prev_entry, dict) else getattr(prev_entry, "type", None)

        if prev_type == "compaction":
            break
        if prev_type == "message":
            break
        cut_index -= 1

    # Determine if this is a split turn
    cut_entry = entries[cut_index]
    cut_type = cut_entry.get("type") if isinstance(cut_entry, dict) else getattr(cut_entry, "type", None)

    is_user_message = False
    if cut_type == "message":
        cut_msg = cut_entry.get("message") if isinstance(cut_entry, dict) else getattr(cut_entry, "message", None)
        if cut_msg is not None:
            is_user_message = getattr(cut_msg, "role", None) == "user"

    turn_start_index = -1
    if not is_user_message:
        turn_start_index = find_turn_start_index(entries, cut_index, start_index)

    return CutPointResult(
        first_kept_entry_index=cut_index,
        turn_start_index=turn_start_index,
        is_split_turn=not is_user_message and turn_start_index != -1,
    )


# ============================================================================
# Message extraction from session entries
# ============================================================================


def _get_message_from_entry(entry: Any) -> AgentMessage | None:
    """Extract AgentMessage from a session entry if it produces one."""
    entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)

    if entry_type == "message":
        return entry.get("message") if isinstance(entry, dict) else getattr(entry, "message", None)

    # For custom message types, we return a simplified representation
    # The full implementation requires messages.py which is not yet ported
    if entry_type == "custom_message":
        # Return a simple user message with the content
        content = entry.get("content") if isinstance(entry, dict) else getattr(entry, "content", "")
        return _make_simple_message("user", content or "")  # type: ignore[no-any-return]

    if entry_type == "branch_summary":
        summary = entry.get("summary") if isinstance(entry, dict) else getattr(entry, "summary", "")
        return _make_summary_message("branch_summary", summary or "")  # type: ignore[no-any-return]

    if entry_type == "compaction":
        summary = entry.get("summary") if isinstance(entry, dict) else getattr(entry, "summary", "")
        return _make_summary_message("compaction_summary", summary or "")  # type: ignore[no-any-return]

    return None


def _make_simple_message(role: str, content: str) -> Any:
    """Create a simple message with string content."""

    class SimpleMsg:
        def __init__(self, r: str, c: str) -> None:
            self.role = r
            self.content = c

    return SimpleMsg(role, content)


def _make_summary_message(role: str, summary: str) -> Any:
    """Create a summary-style message."""

    class SummaryMsg:
        def __init__(self, r: str, s: str) -> None:
            self.role = r
            self.summary = s
            self.content = s

    return SummaryMsg(role, summary)


# ============================================================================
# Summarization
# ============================================================================

_SUMMARIZATION_PROMPT = (
    "The messages above are a conversation to summarize."
    " Create a structured context checkpoint summary that another LLM will use to continue the work."
    """

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""
)

_UPDATE_SUMMARIZATION_PROMPT = (
    "The messages above are NEW conversation messages to incorporate into"
    " the existing summary provided in <previous-summary> tags."
    """

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""
)

_TURN_PREFIX_SUMMARIZATION_PROMPT = (
    "This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent work) is retained."
    """

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""
)


def _convert_to_llm(messages: list[AgentMessage]) -> list[Any]:
    """Convert agent messages to LLM-compatible messages.

    For now, pass through as-is (custom types handled in serialize_conversation).
    """
    return list(messages)


async def generate_summary(
    current_messages: list[AgentMessage],
    model: Model,
    reserve_tokens: int,
    api_key: str,
    signal: asyncio.Event | None = None,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
) -> str:
    """Generate a summary of the conversation using the LLM.

    If previous_summary is provided, uses the update prompt to merge.
    """
    max_tokens = int(0.8 * reserve_tokens)

    base_prompt = _UPDATE_SUMMARIZATION_PROMPT if previous_summary else _SUMMARIZATION_PROMPT
    if custom_instructions:
        base_prompt = f"{base_prompt}\n\nAdditional focus: {custom_instructions}"

    llm_messages = _convert_to_llm(current_messages)
    conversation_text = serialize_conversation(llm_messages)

    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n"
    if previous_summary:
        prompt_text += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    prompt_text += base_prompt

    summarization_messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}],
            "timestamp": int(time.time() * 1000),
        }
    ]

    from pi.ai.types import Context

    context = Context(
        system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
        messages=summarization_messages,  # type: ignore[arg-type]
    )

    from pi.ai.types import SimpleStreamOptions

    options = SimpleStreamOptions(
        max_tokens=max_tokens,
        signal=signal,
        api_key=api_key,
        reasoning="high",
    )

    response = await complete_simple(model, context, options)

    if getattr(response, "stop_reason", None) == "error":
        error_msg = getattr(response, "error_message", None) or "Unknown error"
        raise RuntimeError(f"Summarization failed: {error_msg}")

    content = getattr(response, "content", [])
    return "\n".join(getattr(c, "text", "") for c in content if getattr(c, "type", None) == "text")


async def _generate_turn_prefix_summary(
    messages: list[AgentMessage],
    model: Model,
    reserve_tokens: int,
    api_key: str,
    signal: asyncio.Event | None = None,
) -> str:
    """Generate a summary for a turn prefix (when splitting a turn)."""
    max_tokens = int(0.5 * reserve_tokens)
    llm_messages = _convert_to_llm(messages)
    conversation_text = serialize_conversation(llm_messages)
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n{_TURN_PREFIX_SUMMARIZATION_PROMPT}"

    summarization_messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}],
            "timestamp": int(time.time() * 1000),
        }
    ]

    from pi.ai.types import Context, SimpleStreamOptions

    context = Context(
        system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
        messages=summarization_messages,  # type: ignore[arg-type]
    )
    options = SimpleStreamOptions(max_tokens=max_tokens, signal=signal, api_key=api_key)

    response = await complete_simple(model, context, options)

    if getattr(response, "stop_reason", None) == "error":
        error_msg = getattr(response, "error_message", None) or "Unknown error"
        raise RuntimeError(f"Turn prefix summarization failed: {error_msg}")

    content = getattr(response, "content", [])
    return "\n".join(getattr(c, "text", "") for c in content if getattr(c, "type", None) == "text")


# ============================================================================
# Compaction Preparation
# ============================================================================


def _extract_file_operations(
    messages: list[AgentMessage],
    entries: list[Any],
    prev_compaction_index: int,
) -> FileOperations:
    """Extract file operations from messages and previous compaction entries."""
    file_ops = create_file_ops()

    if prev_compaction_index >= 0:
        prev_compaction = entries[prev_compaction_index]
        from_hook = (
            prev_compaction.get("fromHook")
            if isinstance(prev_compaction, dict)
            else getattr(prev_compaction, "from_hook", False)
        )
        details = (
            prev_compaction.get("details")
            if isinstance(prev_compaction, dict)
            else getattr(prev_compaction, "details", None)
        )

        if not from_hook and details is not None:
            read_files = (
                details.get("readFiles", []) if isinstance(details, dict) else getattr(details, "read_files", [])
            )
            modified_files = (
                details.get("modifiedFiles", [])
                if isinstance(details, dict)
                else getattr(details, "modified_files", [])
            )
            for f in read_files or []:
                file_ops.read.add(f)
            for f in modified_files or []:
                file_ops.edited.add(f)

    for msg in messages:
        extract_file_ops_from_message(msg, file_ops)

    return file_ops


def prepare_compaction(
    path_entries: list[Any],
    settings: CompactionSettings,
) -> CompactionPreparation | None:
    """Prepare compaction data from session entries.

    Returns None if the last entry is already a compaction or if the session
    can't be compacted (missing UUIDs).
    """
    if not path_entries:
        return None

    last_entry = path_entries[-1]
    last_type = last_entry.get("type") if isinstance(last_entry, dict) else getattr(last_entry, "type", None)
    if last_type == "compaction":
        return None

    prev_compaction_index = -1
    for i in range(len(path_entries) - 1, -1, -1):
        entry = path_entries[i]
        entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)
        if entry_type == "compaction":
            prev_compaction_index = i
            break

    boundary_start = prev_compaction_index + 1
    boundary_end = len(path_entries)

    usage_start = max(prev_compaction_index, 0)
    usage_messages: list[AgentMessage] = []
    for i in range(usage_start, boundary_end):
        msg = _get_message_from_entry(path_entries[i])
        if msg is not None:
            usage_messages.append(msg)

    tokens_before = estimate_context_tokens(usage_messages).tokens

    cut_point = find_cut_point(path_entries, boundary_start, boundary_end, settings.keep_recent_tokens)

    first_kept_entry = path_entries[cut_point.first_kept_entry_index]
    entry_id = (
        first_kept_entry.get("id") if isinstance(first_kept_entry, dict) else getattr(first_kept_entry, "id", None)
    )
    if not entry_id:
        return None

    first_kept_entry_id = entry_id
    history_end = cut_point.turn_start_index if cut_point.is_split_turn else cut_point.first_kept_entry_index

    messages_to_summarize: list[AgentMessage] = []
    for i in range(boundary_start, history_end):
        msg = _get_message_from_entry(path_entries[i])
        if msg is not None:
            messages_to_summarize.append(msg)

    turn_prefix_messages: list[AgentMessage] = []
    if cut_point.is_split_turn:
        for i in range(cut_point.turn_start_index, cut_point.first_kept_entry_index):
            msg = _get_message_from_entry(path_entries[i])
            if msg is not None:
                turn_prefix_messages.append(msg)

    previous_summary: str | None = None
    if prev_compaction_index >= 0:
        prev_compaction = path_entries[prev_compaction_index]
        previous_summary = (
            prev_compaction.get("summary")
            if isinstance(prev_compaction, dict)
            else getattr(prev_compaction, "summary", None)
        )

    file_ops = _extract_file_operations(messages_to_summarize, path_entries, prev_compaction_index)

    if cut_point.is_split_turn:
        for msg in turn_prefix_messages:
            extract_file_ops_from_message(msg, file_ops)

    return CompactionPreparation(
        first_kept_entry_id=first_kept_entry_id,
        messages_to_summarize=messages_to_summarize,
        turn_prefix_messages=turn_prefix_messages,
        is_split_turn=cut_point.is_split_turn,
        tokens_before=tokens_before,
        previous_summary=previous_summary,
        file_ops=file_ops,
        settings=settings,
    )


# ============================================================================
# Main compaction function
# ============================================================================


async def compact(
    preparation: CompactionPreparation,
    model: Model,
    api_key: str,
    custom_instructions: str | None = None,
    signal: asyncio.Event | None = None,
) -> CompactionResult:
    """Generate summaries for compaction using prepared data.

    Returns CompactionResult - SessionManager adds uuid/parentUuid when saving.
    """
    first_kept_entry_id = preparation.first_kept_entry_id
    messages_to_summarize = preparation.messages_to_summarize
    turn_prefix_messages = preparation.turn_prefix_messages
    is_split_turn = preparation.is_split_turn
    tokens_before = preparation.tokens_before
    previous_summary = preparation.previous_summary
    file_ops = preparation.file_ops
    settings = preparation.settings

    if is_split_turn and turn_prefix_messages:
        # Generate both summaries in parallel
        tasks = []
        if messages_to_summarize:
            tasks.append(
                generate_summary(
                    messages_to_summarize,
                    model,
                    settings.reserve_tokens,
                    api_key,
                    signal,
                    custom_instructions,
                    previous_summary,
                )
            )
        else:

            async def _no_history() -> str:
                return "No prior history."

            tasks.append(_no_history())

        tasks.append(
            _generate_turn_prefix_summary(
                turn_prefix_messages,
                model,
                settings.reserve_tokens,
                api_key,
                signal,
            )
        )

        results = await asyncio.gather(*tasks)
        history_result, turn_prefix_result = results
        summary = f"{history_result}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_prefix_result}"
    else:
        summary = await generate_summary(
            messages_to_summarize,
            model,
            settings.reserve_tokens,
            api_key,
            signal,
            custom_instructions,
            previous_summary,
        )

    read_files, modified_files = compute_file_lists(file_ops)
    summary += format_file_operations(read_files, modified_files)

    if not first_kept_entry_id:
        raise RuntimeError("First kept entry has no UUID - session may need migration")

    return CompactionResult(
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=tokens_before,
        details=CompactionDetails(read_files=read_files, modified_files=modified_files),
    )
