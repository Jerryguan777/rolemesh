"""OpenAI completions provider — ported from packages/ai/src/providers/openai-completions.ts."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

from pi.ai.env_api_keys import get_env_api_key
from pi.ai.models import calculate_cost, supports_xhigh
from pi.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from pi.ai.providers.simple_options import build_base_options, clamp_reasoning
from pi.ai.providers.transform_messages import transform_messages
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    DoneEvent,
    ErrorEvent,
    Message,
    Model,
    OpenAICompletionsCompat,
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


@dataclass
class OpenAICompletionsOptions(StreamOptions):
    tool_choice: Literal["auto", "none", "required"] | dict[str, Any] | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None


# ---------------------------------------------------------------------------
# Compat detection
# ---------------------------------------------------------------------------


def _detect_compat(model: Model) -> OpenAICompletionsCompat:
    provider = model.provider
    base_url = model.base_url

    is_zai = provider == "zai" or "api.z.ai" in base_url
    _non_standard_urls = ("cerebras.ai", "api.x.ai", "mistral.ai", "chutes.ai", "deepseek.com", "opencode.ai")
    is_non_standard = (
        provider in ("cerebras", "xai", "mistral", "opencode")
        or any(s in base_url for s in _non_standard_urls)
        or is_zai
    )
    use_max_tokens = provider == "mistral" or any(s in base_url for s in ("mistral.ai", "chutes.ai"))
    is_grok = provider == "xai" or "api.x.ai" in base_url
    is_mistral = provider == "mistral" or "mistral.ai" in base_url

    return OpenAICompletionsCompat(
        supports_store=not is_non_standard,
        supports_developer_role=not is_non_standard,
        supports_reasoning_effort=not is_grok and not is_zai,
        supports_usage_in_streaming=True,
        max_tokens_field="max_tokens" if use_max_tokens else "max_completion_tokens",
        requires_tool_result_name=is_mistral,
        requires_assistant_after_tool_result=False,
        requires_thinking_as_text=is_mistral,
        requires_mistral_tool_ids=is_mistral,
        thinking_format="zai" if is_zai else "openai",
        open_router_routing=None,
        vercel_gateway_routing=None,
        supports_strict_mode=True,
    )


def _get_compat(model: Model) -> OpenAICompletionsCompat:
    detected = _detect_compat(model)
    if not model.compat or not isinstance(model.compat, OpenAICompletionsCompat):
        return detected

    mc = model.compat
    return OpenAICompletionsCompat(
        supports_store=(mc.supports_store if mc.supports_store is not None else detected.supports_store),
        supports_developer_role=(
            mc.supports_developer_role if mc.supports_developer_role is not None else detected.supports_developer_role
        ),
        supports_reasoning_effort=(
            mc.supports_reasoning_effort
            if mc.supports_reasoning_effort is not None
            else detected.supports_reasoning_effort
        ),
        supports_usage_in_streaming=(
            mc.supports_usage_in_streaming
            if mc.supports_usage_in_streaming is not None
            else detected.supports_usage_in_streaming
        ),
        max_tokens_field=(mc.max_tokens_field if mc.max_tokens_field is not None else detected.max_tokens_field),
        requires_tool_result_name=(
            mc.requires_tool_result_name
            if mc.requires_tool_result_name is not None
            else detected.requires_tool_result_name
        ),
        requires_assistant_after_tool_result=(
            mc.requires_assistant_after_tool_result
            if mc.requires_assistant_after_tool_result is not None
            else detected.requires_assistant_after_tool_result
        ),
        requires_thinking_as_text=(
            mc.requires_thinking_as_text
            if mc.requires_thinking_as_text is not None
            else detected.requires_thinking_as_text
        ),
        requires_mistral_tool_ids=(
            mc.requires_mistral_tool_ids
            if mc.requires_mistral_tool_ids is not None
            else detected.requires_mistral_tool_ids
        ),
        thinking_format=(mc.thinking_format if mc.thinking_format is not None else detected.thinking_format),
        open_router_routing=(
            mc.open_router_routing if mc.open_router_routing is not None else detected.open_router_routing
        ),
        vercel_gateway_routing=(
            mc.vercel_gateway_routing if mc.vercel_gateway_routing is not None else detected.vercel_gateway_routing
        ),
        supports_strict_mode=(
            mc.supports_strict_mode if mc.supports_strict_mode is not None else detected.supports_strict_mode
        ),
    )


# ---------------------------------------------------------------------------
# Tool ID normalization
# ---------------------------------------------------------------------------


def _normalize_mistral_tool_id(tool_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]", "", tool_id)
    if len(normalized) < 9:
        padding = "ABCDEFGHI"
        normalized = normalized + padding[: 9 - len(normalized)]
    return normalized[:9]


def _has_tool_history(messages: list[Message]) -> bool:
    for msg in messages:
        if msg.role == "toolResult":
            return True
        if msg.role == "assistant" and any(isinstance(b, ToolCall) for b in msg.content):
            return True
    return False


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def convert_messages(
    model: Model,
    context: Context,
    compat: OpenAICompletionsCompat,
) -> list[dict[str, Any]]:
    """Convert Context messages to OpenAI chat completions format."""
    params: list[dict[str, Any]] = []

    def normalize_id(tool_id: str, _m: Model = model, _a: Any = None) -> str:
        if compat.requires_mistral_tool_ids:
            return _normalize_mistral_tool_id(tool_id)
        if "|" in tool_id:
            call_id = tool_id.split("|")[0]
            return re.sub(r"[^a-zA-Z0-9_\-]", "_", call_id)[:40]
        if model.provider == "openai":
            return tool_id[:40] if len(tool_id) > 40 else tool_id
        return tool_id

    transformed = transform_messages(context.messages, model, normalize_id)

    if context.system_prompt:
        use_developer = model.reasoning and compat.supports_developer_role
        role = "developer" if use_developer else "system"
        params.append({"role": role, "content": sanitize_surrogates(context.system_prompt)})

    last_role: str | None = None

    i = 0
    while i < len(transformed):
        msg = transformed[i]

        if compat.requires_assistant_after_tool_result and last_role == "toolResult" and msg.role == "user":
            params.append({"role": "assistant", "content": "I have processed the tool results."})

        if msg.role == "user":
            if isinstance(msg.content, str):
                params.append({"role": "user", "content": sanitize_surrogates(msg.content)})
            else:
                parts: list[dict[str, Any]] = []
                for item in msg.content:
                    if isinstance(item, TextContent):
                        parts.append({"type": "text", "text": sanitize_surrogates(item.text)})
                    else:
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{item.mime_type};base64,{item.data}"},
                            }
                        )
                filtered = [p for p in parts if p.get("type") != "image_url"] if "image" not in model.input else parts
                if not filtered:
                    i += 1
                    last_role = msg.role
                    continue
                params.append({"role": "user", "content": filtered})

        elif msg.role == "assistant":
            assistant_param: dict[str, Any] = {
                "role": "assistant",
                "content": "" if compat.requires_assistant_after_tool_result else None,
            }

            text_blocks = [b for b in msg.content if isinstance(b, TextContent) and b.text.strip()]
            if text_blocks:
                if model.provider == "github-copilot":
                    assistant_param["content"] = "".join(sanitize_surrogates(b.text) for b in text_blocks)
                else:
                    assistant_param["content"] = [
                        {"type": "text", "text": sanitize_surrogates(b.text)} for b in text_blocks
                    ]

            thinking_blocks = [b for b in msg.content if isinstance(b, ThinkingContent) and b.thinking.strip()]
            if thinking_blocks:
                if compat.requires_thinking_as_text:
                    thinking_text = "\n\n".join(b.thinking for b in thinking_blocks)
                    if isinstance(assistant_param.get("content"), list):
                        assistant_param["content"].insert(0, {"type": "text", "text": thinking_text})
                    else:
                        assistant_param["content"] = [{"type": "text", "text": thinking_text}]
                else:
                    sig = thinking_blocks[0].thinking_signature
                    if sig:
                        assistant_param[sig] = "\n".join(b.thinking for b in thinking_blocks)

            tool_calls = [b for b in msg.content if isinstance(b, ToolCall)]
            if tool_calls:
                assistant_param["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in tool_calls
                ]
                # reasoning_details from thought_signature
                reasoning_details = []
                for tc in tool_calls:
                    if tc.thought_signature:
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            reasoning_details.append(json.loads(tc.thought_signature))
                if reasoning_details:
                    assistant_param["reasoning_details"] = reasoning_details

            content = assistant_param.get("content")
            has_content = bool(content if isinstance(content, str) else (content is not None and len(content) > 0))
            if not has_content and not assistant_param.get("tool_calls"):
                i += 1
                last_role = msg.role
                continue
            params.append(assistant_param)

        elif msg.role == "toolResult":
            image_blocks: list[dict[str, Any]] = []
            j = i

            while j < len(transformed) and transformed[j].role == "toolResult":
                tool_msg = transformed[j]
                if not isinstance(tool_msg, ToolResultMessage):
                    raise TypeError(f"Expected ToolResultMessage, got {type(tool_msg).__name__}")
                text_result = "\n".join(c.text for c in tool_msg.content if isinstance(c, TextContent))
                has_text = bool(text_result)

                tool_result_param: dict[str, Any] = {
                    "role": "tool",
                    "content": sanitize_surrogates(text_result if has_text else "(see attached image)"),
                    "tool_call_id": tool_msg.tool_call_id,
                }
                if compat.requires_tool_result_name and tool_msg.tool_name:
                    tool_result_param["name"] = tool_msg.tool_name
                params.append(tool_result_param)

                if "image" in model.input:
                    for block in tool_msg.content:
                        if hasattr(block, "mime_type") and hasattr(block, "data"):
                            image_blocks.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"},
                                }
                            )
                j += 1

            i = j - 1

            if image_blocks:
                if compat.requires_assistant_after_tool_result:
                    params.append({"role": "assistant", "content": "I have processed the tool results."})
                params.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Attached image(s) from tool result:"}, *image_blocks],
                    }
                )
                last_role = "user"
            else:
                last_role = "toolResult"
            i += 1
            continue

        last_role = msg.role
        i += 1

    return params


def _convert_tools(tools: list[Tool], compat: OpenAICompletionsCompat) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                **({"strict": False} if compat.supports_strict_mode is not False else {}),
            },
        }
        for tool in tools
    ]


def _maybe_add_openrouter_cache_control(model: Model, messages: list[dict[str, Any]]) -> None:
    if model.provider != "openrouter" or not model.id.startswith("anthropic/"):
        return
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            return
        if isinstance(content, list):
            for j in range(len(content) - 1, -1, -1):
                part = content[j]
                if isinstance(part, dict) and part.get("type") == "text":
                    part["cache_control"] = {"type": "ephemeral"}
                    return


def _map_stop_reason(reason: str | None) -> StopReason:
    if reason is None:
        return "stop"
    mapping: dict[str, StopReason] = {
        "stop": "stop",
        "length": "length",
        "function_call": "toolUse",
        "tool_calls": "toolUse",
        "content_filter": "error",
    }
    return mapping.get(reason, "stop")


# ---------------------------------------------------------------------------
# Main stream functions
# ---------------------------------------------------------------------------


async def stream_openai_completions(
    model: Model,
    context: Context,
    options: OpenAICompletionsOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """Stream from OpenAI-compatible Chat Completions API."""
    import openai

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

        headers: dict[str, str] = dict(model.headers or {})
        if model.provider == "github-copilot":
            has_imgs = has_copilot_vision_input(context.messages)
            copilot_hdrs = build_copilot_dynamic_headers(context.messages, has_imgs)
            headers.update(copilot_hdrs)
        if options and options.headers:
            headers.update(options.headers)

        client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=model.base_url or None,
            default_headers=headers,
        )

        compat = _get_compat(model)
        messages = convert_messages(model, context, compat)
        _maybe_add_openrouter_cache_control(model, messages)

        params: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "stream": True,
        }

        if compat.supports_usage_in_streaming is not False:
            params["stream_options"] = {"include_usage": True}
        if compat.supports_store:
            params["store"] = False
        if options and options.max_tokens:
            if compat.max_tokens_field == "max_tokens":
                params["max_tokens"] = options.max_tokens
            else:
                params["max_completion_tokens"] = options.max_tokens
        if options and options.temperature is not None:
            params["temperature"] = options.temperature

        if context.tools:
            params["tools"] = _convert_tools(context.tools, compat)
        elif _has_tool_history(context.messages):
            params["tools"] = []

        if options and options.tool_choice:
            params["tool_choice"] = options.tool_choice

        if compat.thinking_format == "zai" and model.reasoning:
            params["thinking"] = {"type": "enabled" if (options and options.reasoning_effort) else "disabled"}
        elif compat.thinking_format == "qwen" and model.reasoning:
            params["enable_thinking"] = bool(options and options.reasoning_effort)
        elif options and options.reasoning_effort and model.reasoning and compat.supports_reasoning_effort:
            params["reasoning_effort"] = options.reasoning_effort

        _mc_compat = model.compat if isinstance(model.compat, OpenAICompletionsCompat) else None
        if "openrouter.ai" in model.base_url and _mc_compat and _mc_compat.open_router_routing:
            params["provider"] = {
                "only": _mc_compat.open_router_routing.only,
                "order": _mc_compat.open_router_routing.order,
            }
        if "ai-gateway.vercel.sh" in model.base_url and _mc_compat and _mc_compat.vercel_gateway_routing:
            routing = _mc_compat.vercel_gateway_routing
            gateway_opts: dict[str, list[str]] = {}
            if routing.only:
                gateway_opts["only"] = routing.only
            if routing.order:
                gateway_opts["order"] = routing.order
            if gateway_opts:
                params["providerOptions"] = {"gateway": gateway_opts}

        if options and options.on_payload:
            options.on_payload(params)

        openai_stream = await client.chat.completions.create(**params)

        yield StartEvent(partial=output)

        current_block: TextContent | ThinkingContent | ToolCall | None = None
        partial_args: str = ""
        blocks = output.content

        def block_index() -> int:
            return len(blocks) - 1

        async for chunk in openai_stream:
            # Honor abort signal mid-stream. Pi's port uses asyncio.Event for
            # cancellation (is_set()); the existing post-loop checks below
            # read a non-existent `.aborted` attribute inherited from the TS
            # AbortSignal origin and never fire. Checking per chunk is the
            # point where the orchestrator's Stop button actually truncates
            # an in-flight LLM response.
            if options and options.signal is not None and options.signal.is_set():
                raise RuntimeError("Request was aborted")
            # Usage chunk
            if hasattr(chunk, "usage") and chunk.usage:
                usage = chunk.usage
                cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
                inp = (getattr(usage, "prompt_tokens", 0) or 0) - cached
                out_tokens = (getattr(usage, "completion_tokens", 0) or 0) + reasoning
                output.usage = Usage(
                    input=inp,
                    output=out_tokens,
                    cache_read=cached,
                    cache_write=0,
                    total_tokens=inp + out_tokens + cached,
                )
                calculate_cost(model, output.usage)

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if not choice:
                continue

            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                output.stop_reason = _map_stop_reason(finish_reason)

            delta = getattr(choice, "delta", None)
            if not delta:
                continue

            # Text content
            content_val = getattr(delta, "content", None)
            if content_val is not None and content_val != "" and len(content_val) > 0:
                if not isinstance(current_block, TextContent):
                    # Finish previous block
                    if isinstance(current_block, ThinkingContent):
                        yield ThinkingEndEvent(
                            content_index=block_index(), content=current_block.thinking, partial=output
                        )
                    elif isinstance(current_block, ToolCall):
                        current_block.arguments = parse_streaming_json(partial_args)
                        partial_args = ""
                        yield ToolCallEndEvent(content_index=block_index(), tool_call=current_block, partial=output)
                    current_block = TextContent(type="text", text="")
                    blocks.append(current_block)
                    yield TextStartEvent(content_index=block_index(), partial=output)
                if isinstance(current_block, TextContent):
                    current_block.text += content_val
                    yield TextDeltaEvent(content_index=block_index(), delta=content_val, partial=output)

            # Reasoning/thinking content
            reasoning_fields = ["reasoning_content", "reasoning", "reasoning_text"]
            found_reasoning: str | None = None
            for rfield in reasoning_fields:
                val = getattr(delta, rfield, None)
                if val is not None and val != "" and len(val) > 0:
                    found_reasoning = rfield
                    break

            if found_reasoning:
                delta_val = getattr(delta, found_reasoning, "")
                if not isinstance(current_block, ThinkingContent):
                    if isinstance(current_block, TextContent):
                        yield TextEndEvent(content_index=block_index(), content=current_block.text, partial=output)
                    elif isinstance(current_block, ToolCall):
                        current_block.arguments = parse_streaming_json(partial_args)
                        partial_args = ""
                        yield ToolCallEndEvent(content_index=block_index(), tool_call=current_block, partial=output)
                    current_block = ThinkingContent(type="thinking", thinking="", thinking_signature=found_reasoning)
                    blocks.append(current_block)
                    yield ThinkingStartEvent(content_index=block_index(), partial=output)
                if isinstance(current_block, ThinkingContent):
                    current_block.thinking += delta_val
                    yield ThinkingDeltaEvent(content_index=block_index(), delta=delta_val, partial=output)

            # Tool calls
            delta_tool_calls = getattr(delta, "tool_calls", None) or []
            for tc_delta in delta_tool_calls:
                tc_id = getattr(tc_delta, "id", None) or ""
                if not isinstance(current_block, ToolCall) or (tc_id and current_block.id != tc_id):
                    # Finish previous block
                    if isinstance(current_block, TextContent):
                        yield TextEndEvent(content_index=block_index(), content=current_block.text, partial=output)
                    elif isinstance(current_block, ThinkingContent):
                        yield ThinkingEndEvent(
                            content_index=block_index(), content=current_block.thinking, partial=output
                        )
                    elif isinstance(current_block, ToolCall):
                        current_block.arguments = parse_streaming_json(partial_args)
                        partial_args = ""
                        yield ToolCallEndEvent(content_index=block_index(), tool_call=current_block, partial=output)
                    fn = getattr(tc_delta, "function", None)
                    current_block = ToolCall(
                        type="toolCall",
                        id=tc_id or "",
                        name=(getattr(fn, "name", "") or "") if fn else "",
                        arguments={},
                    )
                    partial_args = ""
                    blocks.append(current_block)
                    yield ToolCallStartEvent(content_index=block_index(), partial=output)

                if isinstance(current_block, ToolCall):
                    if tc_id:
                        current_block.id = tc_id
                    fn2 = getattr(tc_delta, "function", None)
                    if fn2:
                        fn_name = getattr(fn2, "name", None) or ""
                        fn_args = getattr(fn2, "arguments", None) or ""
                        if fn_name:
                            current_block.name = fn_name
                        if fn_args:
                            partial_args += fn_args
                            current_block.arguments = parse_streaming_json(partial_args)
                    yield ToolCallDeltaEvent(
                        content_index=block_index(),
                        delta=(getattr(getattr(tc_delta, "function", None), "arguments", "") or ""),
                        partial=output,
                    )

            # reasoning_details (gpt-5 series)
            reasoning_details = getattr(delta, "reasoning_details", None)
            if isinstance(reasoning_details, list):
                for detail in reasoning_details:
                    if not isinstance(detail, dict):
                        continue
                    if detail.get("type") == "reasoning.encrypted" and detail.get("id") and detail.get("data"):
                        for b in output.content:
                            if isinstance(b, ToolCall) and b.id == detail["id"]:
                                b.thought_signature = json.dumps(detail)

        # Finish the last open block
        if isinstance(current_block, TextContent):
            yield TextEndEvent(content_index=block_index(), content=current_block.text, partial=output)
        elif isinstance(current_block, ThinkingContent):
            yield ThinkingEndEvent(content_index=block_index(), content=current_block.thinking, partial=output)
        elif isinstance(current_block, ToolCall):
            current_block.arguments = parse_streaming_json(partial_args)
            yield ToolCallEndEvent(content_index=block_index(), tool_call=current_block, partial=output)

        if options and options.signal is not None and options.signal.is_set():
            raise RuntimeError("Request was aborted")
        if output.stop_reason in ("aborted", "error"):
            raise RuntimeError("An unknown error occurred")

        yield DoneEvent(reason=output.stop_reason, message=output)

    except Exception as exc:
        output.stop_reason = (
            "aborted"
            if options and options.signal is not None and options.signal.is_set()
            else "error"
        )
        output.error_message = str(exc)
        yield ErrorEvent(reason=output.stop_reason, error=output)


async def stream_simple_openai_completions(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """High-level OpenAI completions streaming with automatic reasoning config."""
    api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    reasoning_effort = options.reasoning if options else None
    if not supports_xhigh(model):
        reasoning_effort = clamp_reasoning(reasoning_effort)

    tool_choice: Any = None
    if options and isinstance(options, OpenAICompletionsOptions):
        tool_choice = options.tool_choice

    comp_options = OpenAICompletionsOptions(
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
        reasoning_effort=reasoning_effort,
        tool_choice=tool_choice,
    )
    async for event in stream_openai_completions(model, context, comp_options):
        yield event


# TS naming aliases
streamOpenAICompletions = stream_openai_completions
streamSimpleOpenAICompletions = stream_simple_openai_completions
