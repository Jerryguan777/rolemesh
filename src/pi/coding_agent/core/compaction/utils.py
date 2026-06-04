"""Shared utilities for compaction and branch summarization.

Port of packages/coding-agent/src/core/compaction/utils.ts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pi.agent.types import AgentMessage

# ============================================================================
# File Operation Tracking
# ============================================================================


@dataclass
class FileOperations:
    """Tracks file read/write/edit operations from tool calls."""

    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


def create_file_ops() -> FileOperations:
    """Create an empty FileOperations tracker."""
    return FileOperations()


def extract_file_ops_from_message(message: AgentMessage, file_ops: FileOperations) -> None:
    """Extract file operations from tool calls in an assistant message."""
    if not hasattr(message, "role") or message.role != "assistant":
        return
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            # Handle dataclass blocks
            block_type = getattr(block, "type", None)
            if block_type != "toolCall":
                continue
            args = getattr(block, "arguments", None)
            name = getattr(block, "name", None)
        else:
            if block.get("type") != "toolCall":
                continue
            args = block.get("arguments")
            name = block.get("name")

        if not args or not name:
            continue

        path_val = args.get("path") if isinstance(args, dict) else getattr(args, "path", None)
        if not isinstance(path_val, str):
            continue

        if name == "read":
            file_ops.read.add(path_val)
        elif name == "write":
            file_ops.written.add(path_val)
        elif name == "edit":
            file_ops.edited.add(path_val)


def compute_file_lists(file_ops: FileOperations) -> tuple[list[str], list[str]]:
    """Compute final file lists from file operations.

    Returns (read_files, modified_files) where read_files contains only
    files that were read but not modified.
    """
    modified = file_ops.edited | file_ops.written
    read_only = sorted(f for f in file_ops.read if f not in modified)
    modified_files = sorted(modified)
    return read_only, modified_files


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    """Format file operations as XML tags for summary."""
    sections: list[str] = []
    if read_files:
        sections.append(f"<read-files>\n{chr(10).join(read_files)}\n</read-files>")
    if modified_files:
        sections.append(f"<modified-files>\n{chr(10).join(modified_files)}\n</modified-files>")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


# ============================================================================
# Message Serialization
# ============================================================================


def serialize_conversation(messages: list[Any]) -> str:
    """Serialize LLM messages to text for summarization.

    Prevents the model from treating it as a conversation to continue.
    Call convert_to_llm() first to handle custom message types.
    """
    parts: list[str] = []

    for msg in messages:
        role = getattr(msg, "role", None)

        if role == "user":
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    getattr(c, "text", c.get("text", "") if isinstance(c, dict) else "")
                    for c in content
                    if (getattr(c, "type", None) == "text" or (isinstance(c, dict) and c.get("type") == "text"))
                )
            else:
                text = ""
            if text:
                parts.append(f"[User]: {text}")

        elif role == "assistant":
            content = getattr(msg, "content", [])
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[str] = []

            for block in content if isinstance(content, list) else []:
                block_type = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
                if block_type == "text":
                    text_val = getattr(block, "text", None) if not isinstance(block, dict) else block.get("text", "")
                    if text_val:
                        text_parts.append(text_val)
                elif block_type == "thinking":
                    thinking_val = (
                        getattr(block, "thinking", None) if not isinstance(block, dict) else block.get("thinking", "")
                    )
                    if thinking_val:
                        thinking_parts.append(thinking_val)
                elif block_type == "toolCall":
                    name = getattr(block, "name", None) if not isinstance(block, dict) else block.get("name", "")
                    args = (
                        getattr(block, "arguments", {}) if not isinstance(block, dict) else block.get("arguments", {})
                    )
                    if isinstance(args, dict):
                        args_str = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
                    else:
                        args_str = str(args)
                    tool_calls.append(f"{name}({args_str})")

            if thinking_parts:
                parts.append(f"[Assistant thinking]: {chr(10).join(thinking_parts)}")
            if text_parts:
                parts.append(f"[Assistant]: {chr(10).join(text_parts)}")
            if tool_calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(tool_calls)}")

        elif role == "toolResult":
            content = getattr(msg, "content", [])
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    getattr(c, "text", c.get("text", "") if isinstance(c, dict) else "")
                    for c in content
                    if (getattr(c, "type", None) == "text" or (isinstance(c, dict) and c.get("type") == "text"))
                )
            else:
                text = ""
            if text:
                parts.append(f"[Tool result]: {text}")

    return "\n\n".join(parts)


# ============================================================================
# Summarization System Prompt
# ============================================================================

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI coding assistant, then produce a structured summary "
    "following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the structured summary."
)
