"""Message transformation for API submission — Python port of providers/transform-messages.ts."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from pi.ai.types import (
    AssistantContentBlock,
    AssistantMessage,
    Message,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
)


def transform_messages(
    messages: list[Message],
    model: Model,
    normalize_tool_call_id: Callable[[str, Model, AssistantMessage], str] | None = None,
) -> list[Message]:
    """Prepare messages for API submission.

    - Normalizes tool call IDs via optional callback.
    - Converts thinking blocks to text for different model sources.
    - Removes signatures from cross-model calls.
    - Inserts synthetic empty tool results for orphaned tool calls.
    - Skips errored/aborted assistant messages.
    """
    # Build a map of original tool call IDs to normalized IDs
    tool_call_id_map: dict[str, str] = {}

    # First pass: transform messages
    transformed: list[Message] = []
    for msg in messages:
        if msg.role == "user":
            transformed.append(msg)
            continue

        if msg.role == "toolResult":
            if not isinstance(msg, ToolResultMessage):
                raise TypeError(f"Expected ToolResultMessage, got {type(msg)}")
            normalized_id = tool_call_id_map.get(msg.tool_call_id)
            if normalized_id and normalized_id != msg.tool_call_id:
                transformed.append(replace(msg, tool_call_id=normalized_id))
            else:
                transformed.append(msg)
            continue

        if msg.role == "assistant":
            if not isinstance(msg, AssistantMessage):
                raise TypeError(f"Expected AssistantMessage, got {type(msg)}")
            is_same_model = msg.provider == model.provider and msg.api == model.api and msg.model == model.id

            transformed_content: list[AssistantContentBlock] = []
            for block in msg.content:
                if isinstance(block, ThinkingContent):
                    # Same model with signature: keep (needed for replay)
                    if is_same_model and block.thinking_signature:
                        transformed_content.append(block)
                    elif not block.thinking or block.thinking.strip() == "":
                        # Skip empty thinking blocks
                        pass
                    elif is_same_model:
                        transformed_content.append(block)
                    else:
                        # Convert to text for different models
                        transformed_content.append(TextContent(text=block.thinking))
                elif isinstance(block, TextContent):
                    if is_same_model:
                        transformed_content.append(block)
                    else:
                        # Strip signature for cross-model
                        transformed_content.append(TextContent(text=block.text))
                elif isinstance(block, ToolCall):
                    normalized_tool_call = block

                    if not is_same_model and block.thought_signature:
                        normalized_tool_call = replace(block, thought_signature=None)

                    if not is_same_model and normalize_tool_call_id:
                        normalized_id = normalize_tool_call_id(block.id, model, msg)
                        if normalized_id != block.id:
                            tool_call_id_map[block.id] = normalized_id
                            normalized_tool_call = replace(normalized_tool_call, id=normalized_id)

                    transformed_content.append(normalized_tool_call)
                else:
                    transformed_content.append(block)

            transformed.append(replace(msg, content=transformed_content))
            continue

        transformed.append(msg)

    # Second pass: insert synthetic tool results for orphaned tool calls
    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()

    def _flush_orphaned_tool_calls() -> None:
        nonlocal pending_tool_calls, existing_tool_result_ids
        for tc in pending_tool_calls:
            if tc.id not in existing_tool_result_ids:
                result.append(
                    ToolResultMessage(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        content=[TextContent(text="No result provided")],
                        is_error=True,
                        timestamp=time.time() * 1000,
                    )
                )
        pending_tool_calls = []
        existing_tool_result_ids = set()

    for t_msg in transformed:
        if t_msg.role == "assistant":
            if not isinstance(t_msg, AssistantMessage):
                raise TypeError(f"Expected AssistantMessage, got {type(t_msg)}")

            # Flush orphaned tool calls from previous assistant
            if pending_tool_calls:
                _flush_orphaned_tool_calls()

            # Skip errored/aborted messages
            if t_msg.stop_reason in ("error", "aborted"):
                continue

            # Track tool calls
            tool_calls = [b for b in t_msg.content if isinstance(b, ToolCall)]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_tool_result_ids = set()

            result.append(t_msg)

        elif t_msg.role == "toolResult":
            if not isinstance(t_msg, ToolResultMessage):
                raise TypeError(f"Expected ToolResultMessage, got {type(t_msg)}")
            existing_tool_result_ids.add(t_msg.tool_call_id)
            result.append(t_msg)

        elif t_msg.role == "user":
            if pending_tool_calls:
                _flush_orphaned_tool_calls()
            result.append(t_msg)

        else:
            result.append(t_msg)

    return result
