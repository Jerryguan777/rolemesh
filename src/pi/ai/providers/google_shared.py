"""Shared utilities for Google Generative AI, Gemini CLI, and Vertex providers.

Ported from packages/ai/src/providers/google-shared.ts.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from typing import Any

from pi.ai.providers.transform_messages import transform_messages
from pi.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    StopReason,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
)
from pi.ai.utils.sanitize_unicode import sanitize_surrogates

# Type alias for Google API types
GoogleApiType = str  # "google-generative-ai" | "google-gemini-cli" | "google-vertex"

# Atomic counter for generating unique tool call IDs (thread/coroutine-safe)
_tool_call_counter = itertools.count(1)


def generate_tool_call_id(name: str) -> str:
    """Generate a unique tool call ID using name, timestamp, and atomic counter."""
    return f"{name}_{int(time.time() * 1000)}_{next(_tool_call_counter)}"


# Base64 signature pattern for thought signatures
_BASE64_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9+/]+=*$")


def is_thinking_part(part: dict[str, Any]) -> bool:
    """Determine whether a streamed Gemini Part should be treated as thinking."""
    return part.get("thought") is True


def retain_thought_signature(existing: str | None, incoming: str | None) -> str | None:
    """Retain thought signatures during streaming.

    Preserves the last non-empty signature for the current block.
    """
    if isinstance(incoming, str) and len(incoming) > 0:
        return incoming
    return existing


def _is_valid_thought_signature(signature: str | None) -> bool:
    if not signature:
        return False
    if len(signature) % 4 != 0:
        return False
    return bool(_BASE64_SIGNATURE_PATTERN.match(signature))


def _resolve_thought_signature(is_same_provider_and_model: bool, signature: str | None) -> str | None:
    if is_same_provider_and_model and _is_valid_thought_signature(signature):
        return signature
    return None


def requires_tool_call_id(model_id: str) -> bool:
    """Models via Google APIs that require explicit tool call IDs."""
    return model_id.startswith("claude-") or model_id.startswith("gpt-oss-")


def convert_messages(model: Model, context: Context) -> list[dict[str, Any]]:
    """Convert internal messages to Gemini Content[] format."""
    contents: list[dict[str, Any]] = []

    def normalize_tool_call_id(id_: str) -> str:
        if not requires_tool_call_id(model.id):
            return id_
        return re.sub(r"[^a-zA-Z0-9_-]", "_", id_)[:64]

    transformed_messages = transform_messages(
        context.messages, model, lambda tc_id, _m, _a: normalize_tool_call_id(tc_id)
    )

    for msg in transformed_messages:
        if msg.role == "user":
            assert not isinstance(msg, (AssistantMessage, ToolCall))
            from pi.ai.types import UserMessage

            assert isinstance(msg, UserMessage)
            if isinstance(msg.content, str):
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": sanitize_surrogates(msg.content)}],
                    }
                )
            else:
                parts: list[dict[str, Any]] = []
                for item in msg.content:
                    if isinstance(item, TextContent):
                        parts.append({"text": sanitize_surrogates(item.text)})
                    elif isinstance(item, ImageContent):
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": item.mime_type,
                                    "data": item.data,
                                },
                            }
                        )
                filtered = parts if "image" in model.input else [p for p in parts if "text" in p]
                if not filtered:
                    continue
                contents.append({"role": "user", "parts": filtered})

        elif msg.role == "assistant":
            assert isinstance(msg, AssistantMessage)
            parts_list: list[dict[str, Any]] = []
            is_same = msg.provider == model.provider and msg.model == model.id

            for block in msg.content:
                if isinstance(block, TextContent):
                    if not block.text or block.text.strip() == "":
                        continue
                    sig = _resolve_thought_signature(is_same, block.text_signature)
                    part: dict[str, Any] = {"text": sanitize_surrogates(block.text)}
                    if sig:
                        part["thoughtSignature"] = sig
                    parts_list.append(part)

                elif isinstance(block, ThinkingContent):
                    if not block.thinking or block.thinking.strip() == "":
                        continue
                    if is_same:
                        sig = _resolve_thought_signature(is_same, block.thinking_signature)
                        part = {"thought": True, "text": sanitize_surrogates(block.thinking)}
                        if sig:
                            part["thoughtSignature"] = sig
                        parts_list.append(part)
                    else:
                        parts_list.append({"text": sanitize_surrogates(block.thinking)})

                elif isinstance(block, ToolCall):
                    sig = _resolve_thought_signature(is_same, block.thought_signature)
                    is_gemini_3 = "gemini-3" in model.id.lower()
                    if is_gemini_3 and not sig:
                        args_str = json.dumps(block.arguments or {}, indent=2)
                        parts_list.append(
                            {
                                "text": (
                                    f'[Historical context: a different model called tool "{block.name}" '
                                    f"with arguments: {args_str}. "
                                    f"Do not mimic this format - use proper function calling.]"
                                ),
                            }
                        )
                    else:
                        fc: dict[str, Any] = {
                            "name": block.name,
                            "args": block.arguments or {},
                        }
                        if requires_tool_call_id(model.id):
                            fc["id"] = block.id
                        part = {"functionCall": fc}
                        if sig:
                            part["thoughtSignature"] = sig
                        parts_list.append(part)

            if not parts_list:
                continue
            contents.append({"role": "model", "parts": parts_list})

        elif msg.role == "toolResult":
            from pi.ai.types import ToolResultMessage

            assert isinstance(msg, ToolResultMessage)
            text_parts = [c for c in msg.content if isinstance(c, TextContent)]
            text_result = "\n".join(c.text for c in text_parts)
            image_parts = [c for c in msg.content if isinstance(c, ImageContent)] if "image" in model.input else []

            has_text = len(text_result) > 0
            has_images = len(image_parts) > 0
            supports_multimodal_fr = "gemini-3" in model.id

            response_value = (
                sanitize_surrogates(text_result) if has_text else "(see attached image)" if has_images else ""
            )

            image_part_dicts: list[dict[str, Any]] = [
                {"inlineData": {"mimeType": img.mime_type, "data": img.data}} for img in image_parts
            ]

            include_id = requires_tool_call_id(model.id)
            fr: dict[str, Any] = {
                "name": msg.tool_name,
                "response": {"error": response_value} if msg.is_error else {"output": response_value},
            }
            if has_images and supports_multimodal_fr:
                fr["parts"] = image_part_dicts
            if include_id:
                fr["id"] = msg.tool_call_id

            function_response_part: dict[str, Any] = {"functionResponse": fr}

            # Merge consecutive tool results into single user turn
            if contents and contents[-1].get("role") == "user":
                last_parts = contents[-1].get("parts", [])
                if any("functionResponse" in p for p in last_parts):
                    last_parts.append(function_response_part)
                else:
                    contents.append({"role": "user", "parts": [function_response_part]})
            else:
                contents.append({"role": "user", "parts": [function_response_part]})

            # For older models, add images in a separate user message
            if has_images and not supports_multimodal_fr:
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": "Tool result image:"}, *image_part_dicts],
                    }
                )

    return contents


def convert_tools(
    tools: list[Tool],
    use_parameters: bool = False,
) -> list[dict[str, Any]] | None:
    """Convert tools to Gemini function declarations format."""
    if not tools:
        return None
    return [
        {
            "functionDeclarations": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    **(
                        {"parameters": tool.parameters} if use_parameters else {"parametersJsonSchema": tool.parameters}
                    ),
                }
                for tool in tools
            ],
        },
    ]


def map_tool_choice(choice: str) -> str:
    """Map tool choice string to Gemini FunctionCallingConfigMode."""
    mapping = {
        "auto": "AUTO",
        "none": "NONE",
        "any": "ANY",
    }
    return mapping.get(choice, "AUTO")


def map_stop_reason(reason: str) -> StopReason:
    """Map Gemini FinishReason enum to our StopReason."""
    if reason == "STOP":
        return "stop"
    if reason == "MAX_TOKENS":
        return "length"
    return "error"


def map_stop_reason_string(reason: str) -> StopReason:
    """Map string finish reason to our StopReason (for raw API responses)."""
    if reason == "STOP":
        return "stop"
    if reason == "MAX_TOKENS":
        return "length"
    return "error"
