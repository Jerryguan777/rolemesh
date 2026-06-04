"""Shared OpenAI Responses API helpers — ported from packages/ai/src/providers/openai-responses-shared.ts."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from pi.ai.models import calculate_cost
from pi.ai.providers.transform_messages import transform_messages
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ImageContent,
    Model,
    StopReason,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    Tool,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
)
from pi.ai.utils.json_parse import parse_streaming_json
from pi.ai.utils.sanitize_unicode import sanitize_surrogates


def _short_hash(s: str) -> str:
    """Fast deterministic hash to shorten long strings."""
    h1 = 0xDEADBEEF
    h2 = 0x41C6CE57
    for ch in s:
        c = ord(ch)
        h1 = ((h1 ^ c) * 2654435761) & 0xFFFFFFFF
        h2 = ((h2 ^ c) * 1597334677) & 0xFFFFFFFF
    h1 = (((h1 ^ (h1 >> 16)) * 2246822507) & 0xFFFFFFFF) ^ (((h2 ^ (h2 >> 13)) * 3266489909) & 0xFFFFFFFF)
    h2 = (((h2 ^ (h2 >> 16)) * 2246822507) & 0xFFFFFFFF) ^ (((h1 ^ (h1 >> 13)) * 3266489909) & 0xFFFFFFFF)
    return format(h2 & 0xFFFFFFFF, "x") + format(h1 & 0xFFFFFFFF, "x")


@dataclass
class OpenAIResponsesStreamOptions:
    service_tier: str | None = None
    apply_service_tier_pricing: Callable[[Usage, str | None], None] | None = None


@dataclass
class ConvertResponsesMessagesOptions:
    include_system_prompt: bool = True


@dataclass
class ConvertResponsesToolsOptions:
    strict: bool | None = False


def convert_responses_messages(
    model: Model,
    context: Context,
    allowed_tool_call_providers: frozenset[str] | set[str],
    options: ConvertResponsesMessagesOptions | None = None,
) -> list[dict[str, Any]]:
    """Convert Context messages to OpenAI Responses API input format.

    Args:
        model: Target model.
        context: Conversation context.
        allowed_tool_call_providers: Providers whose tool call IDs may need normalization.
        options: Optional conversion options.

    Returns:
        List of input items in OpenAI Responses API format.
    """
    messages: list[dict[str, Any]] = []

    def normalize_tool_call_id(tool_id: str, m: Model, _a: AssistantMessage) -> str:
        if m.provider not in allowed_tool_call_providers:
            return tool_id
        if "|" not in tool_id:
            return tool_id
        call_id, item_id = tool_id.split("|", 1)
        import re

        sanitized_call = re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)
        sanitized_item = re.sub(r"[^a-zA-Z0-9_\-]", "_", item_id)
        if not sanitized_item.startswith("fc"):
            sanitized_item = f"fc_{sanitized_item}"
        norm_call = sanitized_call[:64].rstrip("_")
        norm_item = sanitized_item[:64].rstrip("_")
        return f"{norm_call}|{norm_item}"

    transformed = transform_messages(context.messages, model, normalize_tool_call_id)

    include_system = options.include_system_prompt if options else None
    if include_system is None:
        include_system = True

    if include_system and context.system_prompt:
        role = "developer" if model.reasoning else "system"
        messages.append({"role": role, "content": sanitize_surrogates(context.system_prompt)})

    msg_index = 0
    for msg in transformed:
        if msg.role == "user":
            if isinstance(msg.content, str):
                messages.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": sanitize_surrogates(msg.content)}],
                    }
                )
            else:
                content_parts: list[dict[str, Any]] = []
                for item in msg.content:
                    if isinstance(item, TextContent):
                        content_parts.append({"type": "input_text", "text": sanitize_surrogates(item.text)})
                    else:
                        content_parts.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{item.mime_type};base64,{item.data}",
                            }
                        )
                filtered = (
                    [c for c in content_parts if c.get("type") != "input_image"]
                    if "image" not in model.input
                    else content_parts
                )
                if not filtered:
                    msg_index += 1
                    continue
                messages.append({"role": "user", "content": filtered})

        elif msg.role == "assistant":
            output_items: list[dict[str, Any]] = []
            is_diff_model = msg.model != model.id and msg.provider == model.provider and msg.api == model.api

            for block in msg.content:
                if isinstance(block, ThinkingContent):
                    if block.thinking_signature:
                        try:
                            reasoning_item = json.loads(block.thinking_signature)
                            # Only include fields the API accepts as input.
                            # model_dump() includes status/content etc. that
                            # the Responses API rejects as unknown parameters.
                            allowed = {"id", "type", "encrypted_content", "summary"}
                            filtered = {k: v for k, v in reasoning_item.items() if k in allowed}
                            output_items.append(filtered)
                        except (json.JSONDecodeError, TypeError):
                            pass

                elif isinstance(block, TextContent):
                    msg_id = block.text_signature
                    if not msg_id:
                        msg_id = f"msg_{msg_index}"
                    elif len(msg_id) > 64:
                        msg_id = f"msg_{_short_hash(msg_id)}"
                    output_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": sanitize_surrogates(block.text), "annotations": []}
                            ],
                            "status": "completed",
                            "id": msg_id,
                        }
                    )

                elif isinstance(block, ToolCall):
                    parts = block.id.split("|", 1)
                    call_id = parts[0]
                    item_id: str | None = parts[1] if len(parts) > 1 else None

                    if is_diff_model and item_id and item_id.startswith("fc_"):
                        item_id = None

                    fc_item: dict[str, Any] = {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": block.name,
                        "arguments": json.dumps(block.arguments),
                    }
                    if item_id is not None:
                        fc_item["id"] = item_id
                    output_items.append(fc_item)

            if not output_items:
                msg_index += 1
                continue
            messages.extend(output_items)

        elif msg.role == "toolResult":
            text_result = "\n".join(c.text for c in msg.content if isinstance(c, TextContent))
            has_images = any(isinstance(c, ImageContent) for c in msg.content)
            has_text = bool(text_result)
            call_id = msg.tool_call_id.split("|")[0]

            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": sanitize_surrogates(text_result if has_text else "(see attached image)"),
                }
            )

            if has_images and "image" in model.input:
                img_parts: list[dict[str, Any]] = [
                    {"type": "input_text", "text": "Attached image(s) from tool result:"}
                ]
                for img_block in msg.content:
                    if isinstance(img_block, ImageContent):
                        img_parts.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{img_block.mime_type};base64,{img_block.data}",
                            }
                        )
                messages.append({"role": "user", "content": img_parts})

        msg_index += 1

    return messages


def convert_responses_tools(
    tools: list[Tool],
    options: ConvertResponsesToolsOptions | None = None,
) -> list[dict[str, Any]]:
    """Convert Tool list to OpenAI Responses API tool format.

    Args:
        tools: List of tools to convert.
        options: Optional conversion options (strict mode).

    Returns:
        List of tool dicts in OpenAI Responses API format.
    """
    strict: bool | None = False if options is None else options.strict
    result = []
    for tool in tools:
        entry: dict[str, Any] = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if strict is not None:
            entry["strict"] = strict
        result.append(entry)
    return result


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Get a value from a dict or object attribute."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def process_responses_stream(
    openai_stream: Any,
    output: AssistantMessage,
    model: Model,
    options: OpenAIResponsesStreamOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """Process an OpenAI Responses API stream into AssistantMessageEvents.

    Args:
        openai_stream: Async iterable of ResponseStreamEvent objects.
        output: AssistantMessage to accumulate results into (mutated in place).
        model: The model being used.
        options: Optional stream options (service tier pricing).

    Yields:
        AssistantMessageEvent items.
    """
    current_item: dict[str, Any] | None = None
    current_block: TextContent | ThinkingContent | ToolCall | None = None
    blocks = output.content

    def block_index() -> int:
        return len(blocks) - 1

    # Track partial JSON for toolCall blocks
    partial_json: str = ""

    async for event in openai_stream:
        etype = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
        if etype is None:
            continue

        if etype == "response.output_item.added":
            item = _get(event, "item")
            item_type = _get(item, "type")

            if item_type == "reasoning":
                current_item = {"type": "reasoning", "summary": [], "_raw": item}
                current_block = ThinkingContent(type="thinking", thinking="")
                blocks.append(current_block)
                yield ThinkingStartEvent(content_index=block_index(), partial=output)

            elif item_type == "message":
                current_item = {"type": "message", "content": [], "_raw": item}
                current_block = TextContent(type="text", text="")
                blocks.append(current_block)
                yield TextStartEvent(content_index=block_index(), partial=output)

            elif item_type == "function_call":
                call_id = _get(item, "call_id", "")
                item_id = _get(item, "id", "")
                name = _get(item, "name", "")
                args = _get(item, "arguments", "") or ""
                current_item = {"type": "function_call", "_raw": item}
                current_block = ToolCall(
                    type="toolCall",
                    id=f"{call_id}|{item_id}",
                    name=name,
                    arguments={},
                )
                partial_json = args
                blocks.append(current_block)
                yield ToolCallStartEvent(content_index=block_index(), partial=output)

        elif etype == "response.reasoning_summary_part.added":
            if current_item and current_item.get("type") == "reasoning":
                part = _get(event, "part")
                current_item["summary"].append({"text": _get(part, "text", "")})

        elif etype == "response.reasoning_summary_text.delta":
            if current_item and current_item.get("type") == "reasoning" and isinstance(current_block, ThinkingContent):
                delta = _get(event, "delta", "")
                summary = current_item.get("summary", [])
                if summary:
                    summary[-1]["text"] = summary[-1].get("text", "") + delta
                current_block.thinking += delta
                yield ThinkingDeltaEvent(content_index=block_index(), delta=delta, partial=output)

        elif etype == "response.reasoning_summary_part.done":
            if current_item and current_item.get("type") == "reasoning" and isinstance(current_block, ThinkingContent):
                summary = current_item.get("summary", [])
                if summary:
                    summary[-1]["text"] = summary[-1].get("text", "") + "\n\n"
                current_block.thinking += "\n\n"
                yield ThinkingDeltaEvent(content_index=block_index(), delta="\n\n", partial=output)

        elif etype == "response.content_part.added":
            if current_item and current_item.get("type") == "message":
                part = _get(event, "part")
                part_type = _get(part, "type")
                if part_type in ("output_text", "refusal"):
                    text_val = _get(part, "text", "") or _get(part, "refusal", "")
                    current_item["content"].append({"type": part_type, "text": text_val})

        elif etype == "response.output_text.delta":
            if current_item and current_item.get("type") == "message" and isinstance(current_block, TextContent):
                content_list = current_item.get("content", [])
                if not content_list:
                    continue
                last_part = content_list[-1]
                if last_part.get("type") == "output_text":
                    delta = _get(event, "delta", "")
                    current_block.text += delta
                    last_part["text"] = last_part.get("text", "") + delta
                    yield TextDeltaEvent(content_index=block_index(), delta=delta, partial=output)

        elif etype == "response.refusal.delta":
            if current_item and current_item.get("type") == "message" and isinstance(current_block, TextContent):
                content_list = current_item.get("content", [])
                if not content_list:
                    continue
                last_part = content_list[-1]
                if last_part.get("type") == "refusal":
                    delta = _get(event, "delta", "")
                    current_block.text += delta
                    last_part["text"] = last_part.get("text", "") + delta
                    yield TextDeltaEvent(content_index=block_index(), delta=delta, partial=output)

        elif etype == "response.function_call_arguments.delta":
            if current_item and current_item.get("type") == "function_call" and isinstance(current_block, ToolCall):
                delta = _get(event, "delta", "")
                partial_json += delta
                current_block.arguments = parse_streaming_json(partial_json)
                yield ToolCallDeltaEvent(content_index=block_index(), delta=delta, partial=output)

        elif etype == "response.function_call_arguments.done":
            if current_item and current_item.get("type") == "function_call" and isinstance(current_block, ToolCall):
                partial_json = _get(event, "arguments", "") or ""
                current_block.arguments = parse_streaming_json(partial_json)

        elif etype == "response.output_item.done":
            item = _get(event, "item")
            item_type = _get(item, "type")

            if item_type == "reasoning" and isinstance(current_block, ThinkingContent):
                summary_items = current_item.get("summary", []) if current_item else []
                current_block.thinking = "\n\n".join(s.get("text", "") for s in summary_items)
                if isinstance(item, dict):
                    raw_item = item
                else:
                    # OpenAI SDK object — try _raw, then model_dump, then fallback
                    raw_item = _get(item, "_raw", None)
                    if raw_item is None:
                        dump_fn = getattr(item, "model_dump", None)
                        raw_item = dump_fn() if dump_fn else {"type": "reasoning"}
                current_block.thinking_signature = json.dumps(raw_item)
                yield ThinkingEndEvent(
                    content_index=block_index(),
                    content=current_block.thinking,
                    partial=output,
                )
                current_block = None

            elif item_type == "message" and isinstance(current_block, TextContent):
                content_list = current_item.get("content", []) if current_item else []
                current_block.text = "".join(p.get("text", "") or p.get("refusal", "") for p in content_list)
                item_id = _get(item, "id", "")
                current_block.text_signature = item_id
                yield TextEndEvent(
                    content_index=block_index(),
                    content=current_block.text,
                    partial=output,
                )
                current_block = None

            elif item_type == "function_call":
                call_id = _get(item, "call_id", "")
                item_id = _get(item, "id", "")
                name = _get(item, "name", "")
                args_str = _get(item, "arguments", "") or "{}"
                args = (
                    parse_streaming_json(partial_json)
                    if (current_block and isinstance(current_block, ToolCall) and partial_json)
                    else parse_streaming_json(args_str)
                )
                tool_call = ToolCall(
                    type="toolCall",
                    id=f"{call_id}|{item_id}",
                    name=name,
                    arguments=args,
                )
                current_block = None
                partial_json = ""
                yield ToolCallEndEvent(content_index=block_index(), tool_call=tool_call, partial=output)

        elif etype == "response.completed":
            response = _get(event, "response")
            if response:
                usage_obj = _get(response, "usage")
                if usage_obj:
                    cached = _get(_get(usage_obj, "input_tokens_details") or {}, "cached_tokens", 0) or 0
                    output.usage = Usage(
                        input=(_get(usage_obj, "input_tokens", 0) or 0) - cached,
                        output=_get(usage_obj, "output_tokens", 0) or 0,
                        cache_read=cached,
                        cache_write=0,
                        total_tokens=_get(usage_obj, "total_tokens", 0) or 0,
                    )
                calculate_cost(model, output.usage)
                if options and options.apply_service_tier_pricing:
                    svc_tier = _get(response, "service_tier") or (options.service_tier if options else None)
                    options.apply_service_tier_pricing(output.usage, svc_tier)

                status = _get(response, "status")
                output.stop_reason = _map_responses_stop_reason(status)
                if any(isinstance(b, ToolCall) for b in output.content) and output.stop_reason == "stop":
                    output.stop_reason = "toolUse"

        elif etype == "error":
            code = _get(event, "code", "")
            msg = _get(event, "message", "Unknown error")
            raise RuntimeError(f"Error Code {code}: {msg}")

        elif etype == "response.failed":
            raise RuntimeError("Unknown error")


def _map_responses_stop_reason(status: str | None) -> StopReason:
    if not status:
        return "stop"
    mapping: dict[str, StopReason] = {
        "completed": "stop",
        "incomplete": "length",
        "failed": "error",
        "cancelled": "error",
        "in_progress": "stop",
        "queued": "stop",
    }
    return mapping.get(status, "stop")
