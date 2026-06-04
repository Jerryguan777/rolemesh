"""Anthropic provider — ported from packages/ai/src/providers/anthropic.ts."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

from pi.ai.env_api_keys import get_env_api_key
from pi.ai.models import calculate_cost
from pi.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from pi.ai.providers.simple_options import adjust_max_tokens_for_thinking, build_base_options
from pi.ai.providers.transform_messages import transform_messages
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StopReason,
    StreamOptions,
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
    ToolResultMessage,
    Usage,
)
from pi.ai.utils.json_parse import parse_streaming_json
from pi.ai.utils.sanitize_unicode import sanitize_surrogates

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Claude Code stealth mode: mimic Claude Code tool name casing exactly
# ---------------------------------------------------------------------------

_CLAUDE_CODE_VERSION = "2.1.2"

_CLAUDE_CODE_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "KillShell",
    "NotebookEdit",
    "Skill",
    "Task",
    "TaskOutput",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
]

_CC_TOOL_LOOKUP: dict[str, str] = {t.lower(): t for t in _CLAUDE_CODE_TOOLS}


def _to_claude_code_name(name: str) -> str:
    return _CC_TOOL_LOOKUP.get(name.lower(), name)


def _from_claude_code_name(name: str, tools: list[Tool] | None) -> str:
    if tools:
        lower = name.lower()
        for tool in tools:
            if tool.name.lower() == lower:
                return tool.name
    return name


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

AnthropicEffort = Literal["low", "medium", "high", "max"]


@dataclass
class AnthropicOptions(StreamOptions):
    """Anthropic-specific streaming options."""

    thinking_enabled: bool | None = None
    thinking_budget_tokens: int | None = None
    effort: AnthropicEffort | None = None
    interleaved_thinking: bool | None = None
    tool_choice: Literal["auto", "any", "none"] | dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_cache_retention(cache_retention: CacheRetention | None) -> CacheRetention:
    if cache_retention:
        return cache_retention
    if os.environ.get("PI_CACHE_RETENTION") == "long":
        return "long"
    return "short"


def _get_cache_control(
    base_url: str,
    cache_retention: CacheRetention | None,
) -> dict[str, Any] | None:
    retention = _resolve_cache_retention(cache_retention)
    if retention == "none":
        return None
    ttl = "1h" if retention == "long" and "api.anthropic.com" in base_url else None
    result: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        result["ttl"] = ttl
    return result


def _merge_headers(*sources: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for src in sources:
        if src:
            merged.update(src)
    return merged


def _is_oauth_token(api_key: str) -> bool:
    return "sk-ant-oat" in api_key


def _supports_adaptive_thinking(model_id: str) -> bool:
    return "opus-4-6" in model_id or "opus-4.6" in model_id


def _map_thinking_level_to_effort(
    level: str | None,
) -> AnthropicEffort:
    mapping: dict[str | None, AnthropicEffort] = {
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "max",
    }
    return mapping.get(level, "high")


def _normalize_tool_call_id(tool_id: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9_\-]", "_", tool_id)[:64]


def _convert_content_blocks(
    content: list[TextContent | ImageContent],
) -> str | list[dict[str, Any]]:
    """Convert user/tool content blocks to Anthropic API format."""
    has_images = any(c.type == "image" for c in content)
    if not has_images:
        return sanitize_surrogates("\n".join(c.text for c in content if isinstance(c, TextContent)))

    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextContent):
            blocks.append({"type": "text", "text": sanitize_surrogates(block.text)})
        else:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type,
                        "data": block.data,
                    },
                }
            )

    # Add placeholder if only images
    if not any(b["type"] == "text" for b in blocks):
        blocks.insert(0, {"type": "text", "text": "(see attached image)"})

    return blocks


def _convert_tools(tools: list[Tool], is_oauth: bool) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        schema = tool.parameters
        result.append(
            {
                "name": _to_claude_code_name(tool.name) if is_oauth else tool.name,
                "description": tool.description,
                "input_schema": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            }
        )
    return result


def _map_stop_reason(reason: str) -> StopReason:
    mapping: dict[str, StopReason] = {
        "end_turn": "stop",
        "max_tokens": "length",
        "tool_use": "toolUse",
        "refusal": "error",
        "pause_turn": "stop",
        "stop_sequence": "stop",
        "sensitive": "error",
    }
    result = mapping.get(reason)
    if result is None:
        _log.warning("Unknown Anthropic stop_reason %r, defaulting to 'stop'", reason)
        return "stop"
    return result


def _build_messages(
    messages: list[Any],
    model: Model,
    is_oauth: bool,
    cache_control: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Convert Message list to Anthropic API message params."""
    params: list[dict[str, Any]] = []
    transformed = transform_messages(messages, model, lambda tc_id, _m, _a: _normalize_tool_call_id(tc_id))

    i = 0
    while i < len(transformed):
        msg = transformed[i]

        if msg.role == "user":
            if isinstance(msg.content, str):
                if msg.content.strip():
                    params.append({"role": "user", "content": sanitize_surrogates(msg.content)})
            else:
                blocks: list[dict[str, Any]] = []
                for item in msg.content:
                    if isinstance(item, TextContent):
                        blocks.append({"type": "text", "text": sanitize_surrogates(item.text)})
                    else:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": item.mime_type,
                                    "data": item.data,
                                },
                            }
                        )
                # Filter images if model doesn't support them
                filtered = [b for b in blocks if b.get("type") != "image"] if "image" not in model.input else blocks
                filtered = [b for b in filtered if b.get("type") != "text" or b.get("text", "").strip()]
                if not filtered:
                    i += 1
                    continue
                params.append({"role": "user", "content": filtered})

        elif msg.role == "assistant":
            blocks = []
            for block in msg.content:
                if isinstance(block, TextContent):
                    if not block.text.strip():
                        continue
                    blocks.append({"type": "text", "text": sanitize_surrogates(block.text)})
                elif isinstance(block, ThinkingContent):
                    if not block.thinking.strip():
                        continue
                    if not block.thinking_signature or not block.thinking_signature.strip():
                        blocks.append({"type": "text", "text": sanitize_surrogates(block.thinking)})
                    else:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": sanitize_surrogates(block.thinking),
                                "signature": block.thinking_signature,
                            }
                        )
                elif isinstance(block, ToolCall):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": _to_claude_code_name(block.name) if is_oauth else block.name,
                            "input": block.arguments or {},
                        }
                    )
            if not blocks:
                i += 1
                continue
            params.append({"role": "assistant", "content": blocks})

        elif msg.role == "toolResult":
            # Collect consecutive toolResult messages
            tool_results: list[dict[str, Any]] = []
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": _convert_content_blocks(msg.content),
                    "is_error": msg.is_error,
                }
            )
            j = i + 1
            while j < len(transformed) and transformed[j].role == "toolResult":
                next_msg = transformed[j]
                if not isinstance(next_msg, ToolResultMessage):
                    raise TypeError(f"Expected ToolResultMessage, got {type(next_msg).__name__}")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": next_msg.tool_call_id,
                        "content": _convert_content_blocks(next_msg.content),
                        "is_error": next_msg.is_error,
                    }
                )
                j += 1
            i = j - 1
            params.append({"role": "user", "content": tool_results})

        i += 1

    # Add cache_control to last user message
    if cache_control and params:
        last = params[-1]
        if last.get("role") == "user":
            content = last["content"]
            if isinstance(content, list) and content:
                last_block = content[-1]
                if isinstance(last_block, dict) and last_block.get("type") in ("text", "image", "tool_result"):
                    last_block["cache_control"] = cache_control
            elif isinstance(content, str):
                last["content"] = [{"type": "text", "text": content, "cache_control": cache_control}]

    return params


def _create_client(
    model: Model,
    api_key: str,
    interleaved_thinking: bool,
    options_headers: dict[str, str] | None,
    dynamic_headers: dict[str, str] | None,
) -> tuple[Any, bool]:
    """Create an AsyncAnthropic client.

    Returns:
        Tuple of (client, is_oauth_token).
    """
    import anthropic

    oauth = _is_oauth_token(api_key)

    if model.provider == "github-copilot":
        beta_features = []
        if interleaved_thinking:
            beta_features.append("interleaved-thinking-2025-05-14")

        headers = _merge_headers(
            {
                "accept": "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
                **({"anthropic-beta": ",".join(beta_features)} if beta_features else {}),
            },
            model.headers,
            dynamic_headers,
            options_headers,
        )
        client = anthropic.AsyncAnthropic(
            api_key=None,
            auth_token=api_key,
            base_url=model.base_url or None,
            default_headers=headers,
        )
        return client, False

    beta_features = ["fine-grained-tool-streaming-2025-05-14"]
    if interleaved_thinking:
        beta_features.append("interleaved-thinking-2025-05-14")

    if oauth:
        headers = _merge_headers(
            {
                "accept": "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
                "anthropic-beta": f"claude-code-20250219,oauth-2025-04-20,{','.join(beta_features)}",
                "user-agent": f"claude-cli/{_CLAUDE_CODE_VERSION} (external, cli)",
                "x-app": "cli",
            },
            model.headers,
            options_headers,
        )
        client = anthropic.AsyncAnthropic(
            api_key=None,
            auth_token=api_key,
            base_url=model.base_url or None,
            default_headers=headers,
        )
        return client, True

    headers = _merge_headers(
        {
            "accept": "application/json",
            "anthropic-dangerous-direct-browser-access": "true",
            "anthropic-beta": ",".join(beta_features),
        },
        model.headers,
        options_headers,
    )
    client = anthropic.AsyncAnthropic(
        api_key=api_key,
        base_url=model.base_url or None,
        default_headers=headers,
    )
    return client, False


def _build_params(
    model: Model,
    context: Context,
    is_oauth: bool,
    options: AnthropicOptions | None,
) -> dict[str, Any]:
    """Build Anthropic API request params."""
    cache_control = _get_cache_control(model.base_url, options.cache_retention if options else None)
    params: dict[str, Any] = {
        "model": model.id,
        "messages": _build_messages(context.messages, model, is_oauth, cache_control),
        "max_tokens": (options.max_tokens if options and options.max_tokens else None) or (model.max_tokens // 3),
        "stream": True,
    }

    # System prompt
    if is_oauth:
        system: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                **({"cache_control": cache_control} if cache_control else {}),
            }
        ]
        if context.system_prompt:
            system.append(
                {
                    "type": "text",
                    "text": sanitize_surrogates(context.system_prompt),
                    **({"cache_control": cache_control} if cache_control else {}),
                }
            )
        params["system"] = system
    elif context.system_prompt:
        params["system"] = [
            {
                "type": "text",
                "text": sanitize_surrogates(context.system_prompt),
                **({"cache_control": cache_control} if cache_control else {}),
            }
        ]

    if options and options.temperature is not None:
        params["temperature"] = options.temperature

    if context.tools:
        params["tools"] = _convert_tools(context.tools, is_oauth)

    # Thinking mode
    if options and options.thinking_enabled and model.reasoning:
        if _supports_adaptive_thinking(model.id):
            params["thinking"] = {"type": "adaptive"}
            if options.effort:
                params["output_config"] = {"effort": options.effort}
        else:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": options.thinking_budget_tokens or 1024,
            }

    if options and options.metadata:
        user_id = options.metadata.get("user_id")
        if isinstance(user_id, str):
            params["metadata"] = {"user_id": user_id}

    if options and options.tool_choice is not None:
        if isinstance(options.tool_choice, str):
            params["tool_choice"] = {"type": options.tool_choice}
        else:
            params["tool_choice"] = options.tool_choice

    return params


# ---------------------------------------------------------------------------
# Main async generators
# ---------------------------------------------------------------------------


async def stream_anthropic(
    model: Model,
    context: Context,
    options: AnthropicOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """Stream from the Anthropic Messages API.

    Args:
        model: An anthropic-messages model.
        context: Conversation context.
        options: Anthropic-specific options.

    Yields:
        AssistantMessageEvent — streaming events until DoneEvent or ErrorEvent.
    """
    output = AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )

    try:
        api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider) or ""

        copilot_dynamic_headers: dict[str, str] | None = None
        if model.provider == "github-copilot":
            has_imgs = has_copilot_vision_input(context.messages)
            copilot_dynamic_headers = build_copilot_dynamic_headers(context.messages, has_imgs)

        interleaved = options.interleaved_thinking if options and options.interleaved_thinking is not None else True
        client, is_oauth = _create_client(
            model,
            api_key,
            interleaved,
            options.headers if options else None,
            copilot_dynamic_headers,
        )

        params = _build_params(model, context, is_oauth, options)
        if options and options.on_payload:
            options.on_payload(params)

        yield StartEvent(partial=output)

        # Map: API event index → position in output.content
        api_index_to_content_index: dict[int, int] = {}
        # Track partial JSON for toolCall blocks by content index
        partial_json_by_idx: dict[int, str] = {}

        async with client.messages.stream(**{k: v for k, v in params.items() if k != "stream"}) as stream:
            async for event in stream:
                # Honor abort signal mid-stream. Pi's Python port uses
                # asyncio.Event (is_set()); the pre-existing post-loop checks
                # below read a non-existent `.aborted` attribute inherited
                # from the TS AbortSignal origin and never fire. Per-chunk
                # check is what actually truncates an in-flight Claude API
                # response when the user hits Stop.
                if options and options.signal is not None and options.signal.is_set():
                    raise RuntimeError("Request was aborted")
                etype = getattr(event, "type", None)

                if etype == "message_start":
                    usage = event.message.usage
                    output.usage.input = getattr(usage, "input_tokens", 0) or 0
                    output.usage.output = getattr(usage, "output_tokens", 0) or 0
                    output.usage.cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    output.usage.cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    output.usage.total_tokens = (
                        output.usage.input + output.usage.output + output.usage.cache_read + output.usage.cache_write
                    )
                    calculate_cost(model, output.usage)

                elif etype == "content_block_start":
                    api_idx: int = event.index
                    cb = event.content_block
                    cb_type = getattr(cb, "type", None)

                    if cb_type == "text":
                        block = TextContent(type="text", text="")
                        output.content.append(block)
                        content_idx = len(output.content) - 1
                        api_index_to_content_index[api_idx] = content_idx
                        yield TextStartEvent(content_index=content_idx, partial=output)

                    elif cb_type == "thinking":
                        block2 = ThinkingContent(type="thinking", thinking="", thinking_signature="")
                        output.content.append(block2)
                        content_idx = len(output.content) - 1
                        api_index_to_content_index[api_idx] = content_idx
                        yield ThinkingStartEvent(content_index=content_idx, partial=output)

                    elif cb_type == "tool_use":
                        tc = ToolCall(
                            type="toolCall",
                            id=getattr(cb, "id", ""),
                            name=_from_claude_code_name(getattr(cb, "name", ""), context.tools)
                            if is_oauth
                            else getattr(cb, "name", ""),
                            arguments=getattr(cb, "input", {}) or {},
                        )
                        output.content.append(tc)
                        content_idx = len(output.content) - 1
                        api_index_to_content_index[api_idx] = content_idx
                        partial_json_by_idx[content_idx] = ""
                        yield ToolCallStartEvent(content_index=content_idx, partial=output)

                elif etype == "content_block_delta":
                    api_idx = event.index
                    content_idx = api_index_to_content_index.get(api_idx, -1)
                    if content_idx < 0 or content_idx >= len(output.content):
                        continue

                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    current_block = output.content[content_idx]

                    if dtype == "text_delta" and isinstance(current_block, TextContent):
                        current_block.text += delta.text
                        yield TextDeltaEvent(
                            content_index=content_idx,
                            delta=delta.text,
                            partial=output,
                        )

                    elif dtype == "thinking_delta" and isinstance(current_block, ThinkingContent):
                        current_block.thinking += delta.thinking
                        yield ThinkingDeltaEvent(
                            content_index=content_idx,
                            delta=delta.thinking,
                            partial=output,
                        )

                    elif dtype == "input_json_delta" and isinstance(current_block, ToolCall):
                        partial_json_by_idx[content_idx] = partial_json_by_idx.get(content_idx, "") + delta.partial_json
                        current_block.arguments = parse_streaming_json(partial_json_by_idx[content_idx])
                        yield ToolCallDeltaEvent(
                            content_index=content_idx,
                            delta=delta.partial_json,
                            partial=output,
                        )

                    elif dtype == "signature_delta" and isinstance(current_block, ThinkingContent):
                        current_block.thinking_signature = (current_block.thinking_signature or "") + delta.signature

                elif etype == "content_block_stop":
                    api_idx = event.index
                    content_idx = api_index_to_content_index.get(api_idx, -1)
                    if content_idx < 0 or content_idx >= len(output.content):
                        continue

                    done_block = output.content[content_idx]
                    if isinstance(done_block, TextContent):
                        yield TextEndEvent(
                            content_index=content_idx,
                            content=done_block.text,
                            partial=output,
                        )
                    elif isinstance(done_block, ThinkingContent):
                        yield ThinkingEndEvent(
                            content_index=content_idx,
                            content=done_block.thinking,
                            partial=output,
                        )
                    elif isinstance(done_block, ToolCall):
                        done_block.arguments = parse_streaming_json(partial_json_by_idx.pop(content_idx, ""))
                        yield ToolCallEndEvent(
                            content_index=content_idx,
                            tool_call=done_block,
                            partial=output,
                        )

                elif etype == "message_delta":
                    delta = event.delta
                    if getattr(delta, "stop_reason", None):
                        output.stop_reason = _map_stop_reason(delta.stop_reason)
                    usage = event.usage
                    if getattr(usage, "input_tokens", None) is not None:
                        output.usage.input = usage.input_tokens
                    if getattr(usage, "output_tokens", None) is not None:
                        output.usage.output = usage.output_tokens
                    if getattr(usage, "cache_read_input_tokens", None) is not None:
                        output.usage.cache_read = usage.cache_read_input_tokens
                    if getattr(usage, "cache_creation_input_tokens", None) is not None:
                        output.usage.cache_write = usage.cache_creation_input_tokens
                    output.usage.total_tokens = (
                        output.usage.input + output.usage.output + output.usage.cache_read + output.usage.cache_write
                    )
                    calculate_cost(model, output.usage)

        if options and options.signal is not None and options.signal.is_set():
            raise RuntimeError("Request was aborted")

        if output.stop_reason in ("aborted", "error"):
            raise RuntimeError("An unknown error occurred")

        yield DoneEvent(
            reason=output.stop_reason,
            message=output,
        )

    except Exception as exc:
        output.stop_reason = (
            "aborted"
            if options and options.signal is not None and options.signal.is_set()
            else "error"
        )
        output.error_message = str(exc)
        yield ErrorEvent(reason=output.stop_reason, error=output)


async def stream_simple_anthropic(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """High-level Anthropic streaming with automatic thinking configuration.

    Args:
        model: An anthropic-messages model.
        context: Conversation context.
        options: Simple stream options with optional reasoning level.

    Yields:
        AssistantMessageEvent — streaming events.
    """
    api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    reasoning = options.reasoning if options else None

    if not reasoning:
        ant_options = AnthropicOptions(
            temperature=base.temperature,
            max_tokens=base.max_tokens,
            signal=base.signal,
            api_key=base.api_key,
            cache_retention=base.cache_retention,
            session_id=base.session_id,
            headers=base.headers,
            on_payload=base.on_payload,
            max_retry_delay_ms=base.max_retry_delay_ms,
            metadata=base.metadata,
            thinking_enabled=False,
        )
        async for event in stream_anthropic(model, context, ant_options):
            yield event
        return

    if _supports_adaptive_thinking(model.id):
        effort = _map_thinking_level_to_effort(reasoning)
        ant_options = AnthropicOptions(
            temperature=base.temperature,
            max_tokens=base.max_tokens,
            signal=base.signal,
            api_key=base.api_key,
            cache_retention=base.cache_retention,
            session_id=base.session_id,
            headers=base.headers,
            on_payload=base.on_payload,
            max_retry_delay_ms=base.max_retry_delay_ms,
            metadata=base.metadata,
            thinking_enabled=True,
            effort=effort,
        )
        async for event in stream_anthropic(model, context, ant_options):
            yield event
        return

    # Budget-based thinking for older models
    thinking_budgets = options.thinking_budgets if options else None
    max_toks, thinking_budget = adjust_max_tokens_for_thinking(
        base.max_tokens or 0,
        model.max_tokens,
        reasoning,
        thinking_budgets,
    )
    ant_options = AnthropicOptions(
        temperature=base.temperature,
        max_tokens=max_toks,
        signal=base.signal,
        api_key=base.api_key,
        cache_retention=base.cache_retention,
        session_id=base.session_id,
        headers=base.headers,
        on_payload=base.on_payload,
        max_retry_delay_ms=base.max_retry_delay_ms,
        metadata=base.metadata,
        thinking_enabled=True,
        thinking_budget_tokens=thinking_budget,
    )
    async for event in stream_anthropic(model, context, ant_options):
        yield event


# Keep TS naming for compat
streamAnthropic = stream_anthropic
streamSimpleAnthropic = stream_simple_anthropic
