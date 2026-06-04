"""Branch summarization for tree navigation.

Port of packages/coding-agent/src/core/compaction/branch-summarization.ts.

When navigating to a different point in the session tree, this generates
a summary of the branch being left so context isn't lost.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pi.agent.types import AgentMessage
from pi.ai.stream import complete_simple
from pi.ai.types import Model

from .compaction import estimate_tokens
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
class BranchSummaryResult:
    """Result from generate_branch_summary()."""

    summary: str | None = None
    read_files: list[str] | None = None
    modified_files: list[str] | None = None
    aborted: bool | None = None
    error: str | None = None


@dataclass
class BranchSummaryDetails:
    """Details stored in BranchSummaryEntry.details for file tracking."""

    read_files: list[str]
    modified_files: list[str]


@dataclass
class BranchPreparation:
    """Prepared messages and file ops for branch summarization."""

    messages: list[AgentMessage]
    file_ops: FileOperations
    total_tokens: int


@dataclass
class CollectEntriesResult:
    """Result of collecting session entries for branch summarization."""

    entries: list[Any]  # SessionEntry
    common_ancestor_id: str | None


@dataclass
class GenerateBranchSummaryOptions:
    """Options for generate_branch_summary()."""

    model: Model
    api_key: str
    signal: asyncio.Event
    custom_instructions: str | None = None
    replace_instructions: bool | None = None
    reserve_tokens: int = 16384


# ============================================================================
# Entry Collection
# ============================================================================


def collect_entries_for_branch_summary(
    session: Any,
    old_leaf_id: str | None,
    target_id: str,
) -> CollectEntriesResult:
    """Collect entries that should be summarized when navigating from one position to another.

    Walks from old_leaf_id back to the common ancestor with target_id.
    Does NOT stop at compaction boundaries.
    """
    if not old_leaf_id:
        return CollectEntriesResult(entries=[], common_ancestor_id=None)

    # Find common ancestor
    old_path_entries = session.get_branch(old_leaf_id) if hasattr(session, "get_branch") else []
    old_path: set[str] = {
        str(e.get("id") if isinstance(e, dict) else getattr(e, "id", None))
        for e in old_path_entries
        if (e.get("id") if isinstance(e, dict) else getattr(e, "id", None)) is not None
    }

    target_path = session.get_branch(target_id) if hasattr(session, "get_branch") else []

    common_ancestor_id: str | None = None
    for i in range(len(target_path) - 1, -1, -1):
        entry_id = target_path[i].get("id") if isinstance(target_path[i], dict) else getattr(target_path[i], "id", None)
        if entry_id and entry_id in old_path:
            common_ancestor_id = entry_id
            break

    # Collect entries from old leaf back to common ancestor
    entries: list[Any] = []
    current: str | None = old_leaf_id

    while current and current != common_ancestor_id:
        entry = session.get_entry(current) if hasattr(session, "get_entry") else None
        if not entry:
            break
        entries.append(entry)
        current = entry.get("parentId") if isinstance(entry, dict) else getattr(entry, "parent_id", None)

    entries.reverse()
    return CollectEntriesResult(entries=entries, common_ancestor_id=common_ancestor_id)


# ============================================================================
# Entry to Message Conversion
# ============================================================================


def _get_message_from_entry(entry: Any) -> AgentMessage | None:
    """Extract AgentMessage from a session entry.

    Similar to compaction.ts version but also handles compaction entries.
    Skips tool results - context is in assistant's tool call.
    """
    entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)

    if entry_type == "message":
        msg = entry.get("message") if isinstance(entry, dict) else getattr(entry, "message", None)
        if msg is not None and getattr(msg, "role", None) == "toolResult":
            return None
        return msg

    if entry_type == "custom_message":
        content = entry.get("content") if isinstance(entry, dict) else getattr(entry, "content", "")

        class SimpleMsg:
            role = "user"

            def __init__(self, c: str) -> None:
                self.content = c

        return SimpleMsg(content or "")  # type: ignore[return-value]

    if entry_type == "branch_summary":
        summary = entry.get("summary") if isinstance(entry, dict) else getattr(entry, "summary", "")

        class SummaryMsg:
            role = "branchSummary"

            def __init__(self, s: str) -> None:
                self.summary = s

        return SummaryMsg(summary or "")  # type: ignore[return-value]

    if entry_type == "compaction":
        summary = entry.get("summary") if isinstance(entry, dict) else getattr(entry, "summary", "")

        class CompactionMsg:
            role = "compactionSummary"

            def __init__(self, s: str) -> None:
                self.summary = s

        return CompactionMsg(summary or "")  # type: ignore[return-value]

    # thinking_level_change, model_change, custom, label - don't contribute
    return None


def prepare_branch_entries(entries: list[Any], token_budget: int = 0) -> BranchPreparation:
    """Prepare entries for summarization with token budget.

    Walks entries from NEWEST to OLDEST, adding messages until we hit the token budget.
    Also collects file operations from all entries.
    """
    messages: list[AgentMessage] = []
    file_ops = create_file_ops()
    total_tokens = 0

    # First pass: collect file ops from ALL entries
    for entry in entries:
        entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)

        if entry_type == "branch_summary":
            from_hook = entry.get("fromHook") if isinstance(entry, dict) else getattr(entry, "from_hook", False)
            details = entry.get("details") if isinstance(entry, dict) else getattr(entry, "details", None)

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

    # Second pass: walk from newest to oldest, adding messages until token budget
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        message = _get_message_from_entry(entry)
        if message is None:
            continue

        extract_file_ops_from_message(message, file_ops)

        tokens = estimate_tokens(message)

        if token_budget > 0 and total_tokens + tokens > token_budget:
            # Try to fit summary entries as important context
            entry_type = entry.get("type") if isinstance(entry, dict) else getattr(entry, "type", None)
            if entry_type in ("compaction", "branch_summary") and total_tokens < token_budget * 0.9:
                messages.insert(0, message)
                total_tokens += tokens
            break

        messages.insert(0, message)
        total_tokens += tokens

    return BranchPreparation(messages=messages, file_ops=file_ops, total_tokens=total_tokens)


# ============================================================================
# Summary Generation
# ============================================================================

_BRANCH_SUMMARY_PREAMBLE = (
    "The user explored a different conversation branch before returning here.\nSummary of that exploration:\n\n"
)

_BRANCH_SUMMARY_PROMPT = """Create a structured summary of this conversation branch for context when returning later.

Use this EXACT format:

## Goal
[What was the user trying to accomplish in this branch?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Work that was started but not finished]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [What should happen next to continue this work]

Keep each section concise. Preserve exact file paths, function names, and error messages."""


async def generate_branch_summary(
    entries: list[Any],
    options: GenerateBranchSummaryOptions,
) -> BranchSummaryResult:
    """Generate a summary of abandoned branch entries.

    Args:
        entries: Session entries to summarize (chronological order).
        options: Generation options.
    """
    model = options.model
    api_key = options.api_key
    signal = options.signal
    custom_instructions = options.custom_instructions
    replace_instructions = options.replace_instructions
    reserve_tokens = options.reserve_tokens

    context_window = getattr(model, "context_window", None) or 128000
    token_budget = context_window - reserve_tokens

    preparation = prepare_branch_entries(entries, token_budget)

    if not preparation.messages:
        return BranchSummaryResult(summary="No content to summarize")

    llm_messages = list(preparation.messages)
    conversation_text = serialize_conversation(llm_messages)

    # Build prompt
    if replace_instructions and custom_instructions:
        instructions = custom_instructions
    elif custom_instructions:
        instructions = f"{_BRANCH_SUMMARY_PROMPT}\n\nAdditional focus: {custom_instructions}"
    else:
        instructions = _BRANCH_SUMMARY_PROMPT

    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n{instructions}"

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
    opts = SimpleStreamOptions(api_key=api_key, signal=signal, max_tokens=2048)

    response = await complete_simple(model, context, opts)

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "aborted":
        return BranchSummaryResult(aborted=True)
    if stop_reason == "error":
        error_msg = getattr(response, "error_message", None) or "Summarization failed"
        return BranchSummaryResult(error=error_msg)

    content = getattr(response, "content", [])
    summary_text = "\n".join(getattr(c, "text", "") for c in content if getattr(c, "type", None) == "text")

    summary = _BRANCH_SUMMARY_PREAMBLE + summary_text

    read_files, modified_files = compute_file_lists(preparation.file_ops)
    summary += format_file_operations(read_files, modified_files)

    return BranchSummaryResult(
        summary=summary or "No summary generated",
        read_files=read_files,
        modified_files=modified_files,
    )
