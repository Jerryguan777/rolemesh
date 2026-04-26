"""Amazon Bedrock Converse streaming provider.

Ported from packages/ai/src/providers/amazon-bedrock.ts.
Uses boto3 to call the Bedrock Runtime ConverseStream API.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import boto3  # type: ignore[import-untyped]
import boto3.session  # type: ignore[import-untyped]
import botocore.session  # type: ignore[import-untyped]

from pi.ai.event_stream import AssistantMessageEventStream
from pi.ai.models import calculate_cost
from pi.ai.providers.simple_options import adjust_max_tokens_for_thinking, build_base_options, clamp_reasoning
from pi.ai.providers.transform_messages import transform_messages
from pi.ai.types import (
    AssistantMessage,
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
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingLevel,
    ThinkingStartEvent,
    Tool,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultMessage,
    Usage,
    UsageCost,
)
from pi.ai.utils.json_parse import parse_streaming_json
from pi.ai.utils.sanitize_unicode import sanitize_surrogates


@dataclass
class BedrockOptions(StreamOptions):
    """Options for the Bedrock Converse streaming provider."""

    region: str | None = None
    profile: str | None = None
    tool_choice: Literal["auto", "any", "none"] | dict[str, str] | None = None
    reasoning: ThinkingLevel | None = None
    thinking_budgets: ThinkingBudgets | None = None
    interleaved_thinking: bool | None = None


def stream_bedrock(
    model: Model,
    context: Context,
    options: BedrockOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream a response from Amazon Bedrock Converse API.

    Creates a boto3 bedrock-runtime client and calls converse_stream(),
    pushing events to an AssistantMessageEventStream.
    """
    if options is None:
        options = BedrockOptions()

    stream = AssistantMessageEventStream()

    output = AssistantMessage(
        role="assistant",
        content=[],
        api="bedrock-converse-stream",
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cache_read=0,
            cache_write=0,
            total_tokens=0,
            cost=UsageCost(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0, total=0.0),
        ),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )

    # Block index tracking: maps bedrock content_block_index -> index in output.content
    block_indices: dict[int, int] = {}
    partial_json: dict[int, str] = {}

    async def _do_stream() -> None:
        try:
            region = (
                options.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
            )

            client_kwargs: dict[str, Any] = {"region_name": region}

            # ``BEDROCK_BASE_URL`` redirects boto3 to the host-side
            # credential proxy when set (rolemesh agent containers
            # have it injected by ``rolemesh.agent.executor._pi_extra_env``).
            # The proxy holds the real ``AWS_BEARER_TOKEN_BEDROCK``
            # and overwrites the Authorization header on every
            # request, so whatever boto3 signs locally is moot — the
            # placeholder env (see below) just keeps boto3 from
            # raising ``NoCredentialsError`` before it even sends.
            base_url = os.environ.get("BEDROCK_BASE_URL", "")
            if base_url:
                client_kwargs["endpoint_url"] = base_url

            if options.profile:
                session = botocore.session.Session(profile=options.profile)
                boto3_session = boto3.session.Session(botocore_session=session)
                client = boto3_session.client("bedrock-runtime", **client_kwargs)
            else:
                # When routed through the rolemesh credential proxy,
                # boto3 still wants *some* credentials before it will
                # construct a request — even though the proxy will
                # replace the Authorization header. Ship dummy SigV4
                # creds so boto3 1.x (no Bearer support) sails through;
                # boto3 1.42+ that honours ``AWS_BEARER_TOKEN_BEDROCK``
                # also continues to work because the proxy still wins.
                if base_url:
                    client_kwargs["aws_access_key_id"] = "dummy-access-key"
                    client_kwargs["aws_secret_access_key"] = "dummy-secret-key"
                client = boto3.client("bedrock-runtime", **client_kwargs)

            cache_retention = _resolve_cache_retention(options.cache_retention)
            command_input: dict[str, Any] = {
                "modelId": model.id,
                "messages": _convert_messages(context, model, cache_retention),
                "inferenceConfig": {},
            }
            if options.max_tokens is not None:
                command_input["inferenceConfig"]["maxTokens"] = options.max_tokens
            if options.temperature is not None:
                command_input["inferenceConfig"]["temperature"] = options.temperature

            system_prompt = _build_system_prompt(context.system_prompt, model, cache_retention)
            if system_prompt is not None:
                command_input["system"] = system_prompt

            tool_config = _convert_tool_config(context.tools, options.tool_choice)
            if tool_config is not None:
                command_input["toolConfig"] = tool_config

            additional_fields = _build_additional_model_request_fields(model, options)
            if additional_fields is not None:
                command_input["additionalModelRequestFields"] = additional_fields

            if options.on_payload is not None:
                options.on_payload(command_input)

            # Run the synchronous boto3 call in a thread executor
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: client.converse_stream(**command_input))

            # Process the event stream - iterate in executor since boto3 streaming is sync
            event_stream = response.get("stream", [])

            def _iter_events() -> list[dict[str, Any]]:
                """Collect a batch of events from the sync iterator."""
                events: list[dict[str, Any]] = []
                try:
                    for item in event_stream:
                        events.append(item)
                except Exception as exc:
                    events.append({"_error": str(exc)})
                return events

            events = await loop.run_in_executor(None, _iter_events)

            for item in events:
                if "_error" in item:
                    raise RuntimeError(item["_error"])

                if "messageStart" in item:
                    msg_start = item["messageStart"]
                    if msg_start.get("role") != "assistant":
                        raise RuntimeError("Unexpected assistant message start but got user message start instead")
                    stream.push(StartEvent(partial=output))
                elif "contentBlockStart" in item:
                    _handle_content_block_start(item["contentBlockStart"], block_indices, partial_json, output, stream)
                elif "contentBlockDelta" in item:
                    _handle_content_block_delta(item["contentBlockDelta"], block_indices, partial_json, output, stream)
                elif "contentBlockStop" in item:
                    _handle_content_block_stop(item["contentBlockStop"], block_indices, partial_json, output, stream)
                elif "messageStop" in item:
                    msg_stop = item["messageStop"]
                    output.stop_reason = _map_stop_reason(msg_stop.get("stopReason"))
                elif "metadata" in item:
                    _handle_metadata(item["metadata"], model, output)
                elif "internalServerException" in item:
                    raise RuntimeError(f"Internal server error: {item['internalServerException'].get('message', '')}")
                elif "modelStreamErrorException" in item:
                    raise RuntimeError(f"Model stream error: {item['modelStreamErrorException'].get('message', '')}")
                elif "validationException" in item:
                    raise RuntimeError(f"Validation error: {item['validationException'].get('message', '')}")
                elif "throttlingException" in item:
                    raise RuntimeError(f"Throttling error: {item['throttlingException'].get('message', '')}")
                elif "serviceUnavailableException" in item:
                    raise RuntimeError(f"Service unavailable: {item['serviceUnavailableException'].get('message', '')}")

            if options.signal is not None and options.signal.is_set():
                raise RuntimeError("Request was aborted")

            if output.stop_reason in ("error", "aborted"):
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stop_reason, message=output))
            stream.end()

        except Exception as exc:
            output.stop_reason = "aborted" if (options.signal is not None and options.signal.is_set()) else "error"
            output.error_message = str(exc)
            stream.push(ErrorEvent(reason=output.stop_reason, error=output))
            stream.end()

    _task = asyncio.ensure_future(_do_stream())  # noqa: RUF006
    return stream


def stream_simple_bedrock(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream with simplified options, automatically configuring thinking for Claude models."""
    base = build_base_options(model, options, None)

    if options is None or options.reasoning is None:
        return stream_bedrock(
            model,
            context,
            BedrockOptions(
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
                reasoning=None,
            ),
        )

    if model.id.find("anthropic.claude") != -1 or model.id.find("anthropic/claude") != -1:
        if _supports_adaptive_thinking(model.id):
            return stream_bedrock(
                model,
                context,
                BedrockOptions(
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
                    reasoning=options.reasoning,
                    thinking_budgets=options.thinking_budgets,
                ),
            )

        max_tokens, thinking_budget = adjust_max_tokens_for_thinking(
            base.max_tokens or 0,
            model.max_tokens,
            options.reasoning,
            options.thinking_budgets,
        )

        clamped = clamp_reasoning(options.reasoning)
        budgets = ThinkingBudgets()
        if options.thinking_budgets is not None:
            budgets = ThinkingBudgets(
                minimal=options.thinking_budgets.minimal,
                low=options.thinking_budgets.low,
                medium=options.thinking_budgets.medium,
                high=options.thinking_budgets.high,
            )
        if clamped is not None:
            setattr(budgets, clamped, thinking_budget)

        return stream_bedrock(
            model,
            context,
            BedrockOptions(
                temperature=base.temperature,
                max_tokens=max_tokens,
                signal=base.signal,
                api_key=base.api_key,
                cache_retention=base.cache_retention,
                session_id=base.session_id,
                headers=base.headers,
                on_payload=base.on_payload,
                max_retry_delay_ms=base.max_retry_delay_ms,
                metadata=base.metadata,
                reasoning=options.reasoning,
                thinking_budgets=budgets,
            ),
        )

    return stream_bedrock(
        model,
        context,
        BedrockOptions(
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
            reasoning=options.reasoning,
            thinking_budgets=options.thinking_budgets,
        ),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _handle_content_block_start(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index: int = event.get("contentBlockIndex", 0)
    start = event.get("start", {})

    if "toolUse" in start:
        tool_use = start["toolUse"]
        block = ToolCall(
            id=tool_use.get("toolUseId", ""),
            name=tool_use.get("name", ""),
            arguments={},
        )
        output.content.append(block)
        idx = len(output.content) - 1
        block_indices[content_block_index] = idx
        partial_json[content_block_index] = ""
        stream.push(ToolCallStartEvent(content_index=idx, partial=output))


def _handle_content_block_delta(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index: int = event.get("contentBlockIndex", 0)
    delta = event.get("delta", {})
    idx = block_indices.get(content_block_index)

    if "text" in delta:
        text_delta: str = delta["text"]
        if idx is None:
            # No text block yet - create one (contentBlockStart is not sent for text blocks)
            block = TextContent(text="")
            output.content.append(block)
            idx = len(output.content) - 1
            block_indices[content_block_index] = idx
            stream.push(TextStartEvent(content_index=idx, partial=output))

        content_block = output.content[idx]
        if isinstance(content_block, TextContent):
            content_block.text += text_delta
            stream.push(TextDeltaEvent(content_index=idx, delta=text_delta, partial=output))

    elif "toolUse" in delta and idx is not None:
        tool_delta = delta["toolUse"]
        input_delta: str = tool_delta.get("input", "")
        content_block = output.content[idx]
        if isinstance(content_block, ToolCall):
            partial_json[content_block_index] = partial_json.get(content_block_index, "") + input_delta
            content_block.arguments = parse_streaming_json(partial_json[content_block_index])
            stream.push(ToolCallDeltaEvent(content_index=idx, delta=input_delta, partial=output))

    elif "reasoningContent" in delta:
        reasoning = delta["reasoningContent"]
        if idx is None:
            # Create thinking block on first reasoning delta
            thinking_block = ThinkingContent(thinking="", thinking_signature="")
            output.content.append(thinking_block)
            idx = len(output.content) - 1
            block_indices[content_block_index] = idx
            stream.push(ThinkingStartEvent(content_index=idx, partial=output))

        content_block = output.content[idx]
        if isinstance(content_block, ThinkingContent):
            reasoning_text: str | None = reasoning.get("text")
            if reasoning_text:
                content_block.thinking += reasoning_text
                stream.push(ThinkingDeltaEvent(content_index=idx, delta=reasoning_text, partial=output))

            reasoning_sig: str | None = reasoning.get("signature")
            if reasoning_sig:
                content_block.thinking_signature = (content_block.thinking_signature or "") + reasoning_sig


def _handle_content_block_stop(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index: int = event.get("contentBlockIndex", 0)
    idx = block_indices.get(content_block_index)
    if idx is None:
        return

    block = output.content[idx]

    if isinstance(block, TextContent):
        stream.push(TextEndEvent(content_index=idx, content=block.text, partial=output))
    elif isinstance(block, ThinkingContent):
        stream.push(ThinkingEndEvent(content_index=idx, content=block.thinking, partial=output))
    elif isinstance(block, ToolCall):
        pj = partial_json.get(content_block_index, "")
        block.arguments = parse_streaming_json(pj)
        partial_json.pop(content_block_index, None)
        stream.push(ToolCallEndEvent(content_index=idx, tool_call=block, partial=output))

    # Clean up index tracking
    block_indices.pop(content_block_index, None)


def _handle_metadata(
    event: dict[str, Any],
    model: Model,
    output: AssistantMessage,
) -> None:
    usage = event.get("usage")
    if usage is not None:
        output.usage.input = usage.get("inputTokens", 0)
        output.usage.output = usage.get("outputTokens", 0)
        output.usage.cache_read = usage.get("cacheReadInputTokens", 0)
        output.usage.cache_write = usage.get("cacheWriteInputTokens", 0)
        output.usage.total_tokens = usage.get("totalTokens", 0) or (output.usage.input + output.usage.output)
        calculate_cost(model, output.usage)


def _map_stop_reason(reason: str | None) -> StopReason:
    """Map Bedrock stop reason to our StopReason type."""
    if reason in ("end_turn", "stop_sequence"):
        return "stop"
    if reason in ("max_tokens", "model_context_window_exceeded"):
        return "length"
    if reason == "tool_use":
        return "toolUse"
    return "error"


def _convert_messages(
    context: Context,
    model: Model,
    cache_retention: CacheRetention,
) -> list[dict[str, Any]]:
    """Convert context messages to Bedrock Converse message format."""
    result: list[dict[str, Any]] = []
    transformed = transform_messages(context.messages, model, lambda tc_id, _m, _a: _normalize_tool_call_id(tc_id))

    i = 0
    while i < len(transformed):
        m = transformed[i]

        if m.role == "user":
            from pi.ai.types import UserMessage

            assert isinstance(m, UserMessage)
            if isinstance(m.content, str):
                content_blocks: list[dict[str, Any]] = [{"text": sanitize_surrogates(m.content)}]
            else:
                content_blocks = []
                for c in m.content:
                    if isinstance(c, TextContent):
                        content_blocks.append({"text": sanitize_surrogates(c.text)})
                    elif isinstance(c, ImageContent):
                        content_blocks.append({"image": _create_image_block(c.mime_type, c.data)})
                    else:
                        raise ValueError("Unknown user content type")
            result.append({"role": "user", "content": content_blocks})

        elif m.role == "assistant":
            assert isinstance(m, AssistantMessage)
            # Skip assistant messages with empty content
            if len(m.content) == 0:
                i += 1
                continue

            assistant_blocks: list[dict[str, Any]] = []
            for ac in m.content:
                if isinstance(ac, TextContent):
                    if ac.text.strip() == "":
                        continue
                    assistant_blocks.append({"text": sanitize_surrogates(ac.text)})
                elif isinstance(ac, ToolCall):
                    assistant_blocks.append(
                        {
                            "toolUse": {"toolUseId": ac.id, "name": ac.name, "input": ac.arguments},
                        }
                    )
                elif isinstance(ac, ThinkingContent):
                    if ac.thinking.strip() == "":
                        continue
                    if _supports_thinking_signature(model):
                        assistant_blocks.append(
                            {
                                "reasoningContent": {
                                    "reasoningText": {
                                        "text": sanitize_surrogates(ac.thinking),
                                        "signature": ac.thinking_signature,
                                    },
                                },
                            }
                        )
                    else:
                        assistant_blocks.append(
                            {
                                "reasoningContent": {
                                    "reasoningText": {"text": sanitize_surrogates(ac.thinking)},
                                },
                            }
                        )
                else:
                    raise ValueError("Unknown assistant content type")

            # Skip if all content blocks were filtered out
            if len(assistant_blocks) == 0:
                i += 1
                continue

            result.append({"role": "assistant", "content": assistant_blocks})

        elif m.role == "toolResult":
            assert isinstance(m, ToolResultMessage)
            # Collect all consecutive toolResult messages into a single user message
            tool_results: list[dict[str, Any]] = []

            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": m.tool_call_id,
                        "content": [
                            {"image": _create_image_block(c.mime_type, c.data)}
                            if isinstance(c, ImageContent)
                            else {"text": sanitize_surrogates(c.text)}
                            for c in m.content
                        ],
                        "status": "error" if m.is_error else "success",
                    },
                }
            )

            # Look ahead for consecutive toolResult messages
            j = i + 1
            while j < len(transformed) and transformed[j].role == "toolResult":
                next_msg = transformed[j]
                assert isinstance(next_msg, ToolResultMessage)
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": next_msg.tool_call_id,
                            "content": [
                                {"image": _create_image_block(c.mime_type, c.data)}
                                if isinstance(c, ImageContent)
                                else {"text": sanitize_surrogates(c.text)}
                                for c in next_msg.content
                            ],
                            "status": "error" if next_msg.is_error else "success",
                        },
                    }
                )
                j += 1

            # Skip the messages we've already processed
            i = j - 1

            result.append({"role": "user", "content": tool_results})
        else:
            raise ValueError("Unknown message role")

        i += 1

    # Add cache point to the last user message for supported Claude models
    if cache_retention != "none" and _supports_prompt_caching(model) and len(result) > 0:
        last_message = result[-1]
        if last_message.get("role") == "user" and last_message.get("content") is not None:
            cache_point: dict[str, Any] = {"cachePoint": {"type": "default"}}
            if cache_retention == "long":
                cache_point["cachePoint"]["ttl"] = {"unit": "HOURS", "value": 1}
            last_message["content"].append(cache_point)

    return result


def _convert_tool_config(
    tools: list[Tool] | None,
    tool_choice: Literal["auto", "any", "none"] | dict[str, str] | None,
) -> dict[str, Any] | None:
    """Convert tools and tool_choice to Bedrock ToolConfiguration format.

    Bedrock Converse caps tool names at 64 characters, while Anthropic
    native and OpenAI accept up to 128. We do NOT silently truncate
    here — a hidden mapping layer would just defer the failure to a
    confusing point downstream (the agent would call a name that no
    longer matches its tool registry). Fail loudly with the offending
    name so operators know to apply the short-prefix MCP naming
    scheme upstream.
    """
    if not tools or tool_choice == "none":
        return None

    for tool in tools:
        if len(tool.name) > 64:
            raise ValueError(
                f"Bedrock Converse: tool name {tool.name!r} is "
                f"{len(tool.name)} chars; max 64. Apply the upstream "
                f"short-prefix MCP naming scheme before sending to "
                f"this provider."
            )

    bedrock_tools: list[dict[str, Any]] = [
        {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"json": tool.parameters},
            },
        }
        for tool in tools
    ]

    bedrock_tool_choice: dict[str, Any] | None = None
    if tool_choice == "auto":
        bedrock_tool_choice = {"auto": {}}
    elif tool_choice == "any":
        bedrock_tool_choice = {"any": {}}
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        bedrock_tool_choice = {"tool": {"name": tool_choice.get("name", "")}}

    config: dict[str, Any] = {"tools": bedrock_tools}
    if bedrock_tool_choice is not None:
        config["toolChoice"] = bedrock_tool_choice
    return config


def _build_system_prompt(
    system_prompt: str | None,
    model: Model,
    cache_retention: CacheRetention,
) -> list[dict[str, Any]] | None:
    """Build Bedrock system prompt content blocks."""
    if not system_prompt:
        return None

    blocks: list[dict[str, Any]] = [{"text": sanitize_surrogates(system_prompt)}]

    # Add cache point for supported Claude models
    if cache_retention != "none" and _supports_prompt_caching(model):
        cache_point: dict[str, Any] = {"cachePoint": {"type": "default"}}
        if cache_retention == "long":
            cache_point["cachePoint"]["ttl"] = {"unit": "HOURS", "value": 1}
        blocks.append(cache_point)

    return blocks


def _build_additional_model_request_fields(
    model: Model,
    options: BedrockOptions,
) -> dict[str, Any] | None:
    """Build additional model request fields for thinking/reasoning support."""
    if not options.reasoning or not model.reasoning:
        return None

    model_id = model.id
    if "anthropic.claude" in model_id or "anthropic/claude" in model_id:
        if _supports_adaptive_thinking(model_id):
            result: dict[str, Any] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": _map_thinking_level_to_effort(options.reasoning)},
            }
        else:
            default_budgets: dict[str, int] = {
                "minimal": 1024,
                "low": 2048,
                "medium": 8192,
                "high": 16384,
                "xhigh": 16384,
            }

            # Custom budgets override defaults (xhigh not in ThinkingBudgets, use high)
            level = "high" if options.reasoning == "xhigh" else options.reasoning
            budget = default_budgets[options.reasoning]
            if options.thinking_budgets is not None:
                custom_val = getattr(options.thinking_budgets, level, None)
                if custom_val is not None:
                    budget = custom_val

            result = {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": budget,
                },
            }

        # Add interleaved thinking beta for non-adaptive models
        if not _supports_adaptive_thinking(model_id):
            interleaved = options.interleaved_thinking if options.interleaved_thinking is not None else True
            if interleaved:
                result["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]

        return result

    return None


def _create_image_block(mime_type: str, data: str) -> dict[str, Any]:
    """Convert base64 image data to Bedrock image format."""
    format_map: dict[str, str] = {
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    fmt = format_map.get(mime_type)
    if fmt is None:
        raise ValueError(f"Unknown image type: {mime_type}")

    image_bytes = base64.b64decode(data)
    return {"source": {"bytes": image_bytes}, "format": fmt}


def _supports_prompt_caching(model: Model) -> bool:
    """Check if the model supports prompt caching."""
    if model.cost.cache_read or model.cost.cache_write:
        return True

    model_id = model.id.lower()
    # Claude 4.x models
    if "claude" in model_id and ("-4-" in model_id or "-4." in model_id):
        return True
    # Claude 3.7 Sonnet
    if "claude-3-7-sonnet" in model_id:
        return True
    # Claude 3.5 Haiku
    return "claude-3-5-haiku" in model_id


def _supports_thinking_signature(model: Model) -> bool:
    """Check if the model supports thinking signatures in reasoningContent.

    Only Anthropic Claude models support the signature field.
    """
    model_id = model.id.lower()
    return "anthropic.claude" in model_id or "anthropic/claude" in model_id


def _supports_adaptive_thinking(model_id: str) -> bool:
    """Check if the model supports adaptive thinking (Opus 4.6+)."""
    return "opus-4-6" in model_id or "opus-4.6" in model_id


def _resolve_cache_retention(cache_retention: CacheRetention | None) -> CacheRetention:
    """Resolve cache retention preference, defaulting to 'short'."""
    if cache_retention is not None:
        return cache_retention
    if os.environ.get("PI_CACHE_RETENTION") == "long":
        return "long"
    return "short"


def _normalize_tool_call_id(tool_call_id: str) -> str:
    """Normalize a tool call ID to match Bedrock's requirements."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id)
    return sanitized[:64] if len(sanitized) > 64 else sanitized


def _map_thinking_level_to_effort(level: ThinkingLevel | None) -> str:
    """Map thinking level to Bedrock effort string."""
    if level in ("minimal", "low"):
        return "low"
    if level == "medium":
        return "medium"
    if level == "high":
        return "high"
    if level == "xhigh":
        return "max"
    return "high"
