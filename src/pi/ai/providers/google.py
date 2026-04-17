"""Google Generative AI streaming provider.

Ported from packages/ai/src/providers/google.ts.
Uses the google-genai SDK for streaming completions.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from google import genai
from google.genai.types import HttpOptions

from pi.ai.event_stream import AssistantMessageEventStream
from pi.ai.models import calculate_cost
from pi.ai.providers.google_shared import (
    convert_messages,
    convert_tools,
    generate_tool_call_id,
    is_thinking_part,
    map_stop_reason,
    map_tool_choice,
    retain_thought_signature,
)
from pi.ai.providers.simple_options import build_base_options, clamp_reasoning
from pi.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingLevel,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from pi.ai.utils.sanitize_unicode import sanitize_surrogates

GoogleThinkingLevel = Literal["THINKING_LEVEL_UNSPECIFIED", "MINIMAL", "LOW", "MEDIUM", "HIGH"]


@dataclass
class GoogleThinkingConfig:
    """Configuration for Google thinking/reasoning."""

    enabled: bool = False
    budget_tokens: int | None = None
    level: GoogleThinkingLevel | None = None


@dataclass
class GoogleOptions(StreamOptions):
    """Options for the Google Generative AI streaming provider."""

    tool_choice: Literal["auto", "none", "any"] | None = None
    thinking: GoogleThinkingConfig = field(default_factory=GoogleThinkingConfig)


# Set to prevent background tasks from being garbage collected
_background_tasks: set[asyncio.Task[None]] = set()


def _create_client(
    model: Model,
    api_key: str | None = None,
    options_headers: dict[str, str] | None = None,
) -> genai.Client:
    """Create a Google GenAI client with appropriate configuration."""
    http_opts_kwargs: dict[str, Any] = {}
    if model.base_url:
        http_opts_kwargs["base_url"] = model.base_url
        http_opts_kwargs["api_version"] = ""  # baseUrl already includes version path
    if model.headers or options_headers:
        http_opts_kwargs["headers"] = {**(model.headers or {}), **(options_headers or {})}

    http_options: HttpOptions | None = HttpOptions(**http_opts_kwargs) if http_opts_kwargs else None

    return genai.Client(
        api_key=api_key or "",
        http_options=http_options,
    )


def _build_params(
    model: Model,
    context: Context,
    options: GoogleOptions | None = None,
) -> dict[str, Any]:
    """Build parameters for client.models.generate_content_stream()."""
    opts = options or GoogleOptions()
    contents = convert_messages(model, context)

    generation_config: dict[str, Any] = {}
    if opts.temperature is not None:
        generation_config["temperature"] = opts.temperature
    if opts.max_tokens is not None:
        generation_config["max_output_tokens"] = opts.max_tokens

    config: dict[str, Any] = {**generation_config}

    if context.system_prompt:
        config["system_instruction"] = sanitize_surrogates(context.system_prompt)

    if context.tools and len(context.tools) > 0:
        config["tools"] = convert_tools(context.tools)

    if context.tools and len(context.tools) > 0 and opts.tool_choice:
        config["tool_config"] = {
            "function_calling_config": {
                "mode": map_tool_choice(opts.tool_choice),
            },
        }
    else:
        config["tool_config"] = None

    if opts.thinking.enabled and model.reasoning:
        thinking_config: dict[str, Any] = {"include_thoughts": True}
        if opts.thinking.level is not None:
            thinking_config["thinking_level"] = opts.thinking.level
        elif opts.thinking.budget_tokens is not None:
            thinking_config["thinking_budget"] = opts.thinking.budget_tokens
        config["thinking_config"] = thinking_config

    params: dict[str, Any] = {
        "model": model.id,
        "contents": contents,
        "config": config,
    }

    return params


def _is_gemini_3_pro_model(model: Model) -> bool:
    """Check if the model is a Gemini 3 Pro model."""
    return "3-pro" in model.id


def _is_gemini_3_flash_model(model: Model) -> bool:
    """Check if the model is a Gemini 3 Flash model."""
    return "3-flash" in model.id


def _get_gemini3_thinking_level(
    effort: ThinkingLevel,
    model: Model,
) -> GoogleThinkingLevel:
    """Map thinking effort level to Google's thinking level for Gemini 3 models."""
    if _is_gemini_3_pro_model(model):
        if effort in ("minimal", "low"):
            return "LOW"
        return "HIGH"
    mapping: dict[str, GoogleThinkingLevel] = {
        "minimal": "MINIMAL",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }
    return mapping.get(effort, "HIGH")


def _get_google_budget(
    model: Model,
    effort: ThinkingLevel,
    custom_budgets: Any | None = None,
) -> int:
    """Get the thinking budget tokens for a Google model at the given effort level."""
    if custom_budgets is not None:
        custom_val = getattr(custom_budgets, effort, None)
        if custom_val is not None:
            return int(custom_val)

    if "2.5-pro" in model.id:
        budgets: dict[str, int] = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 32768,
        }
        return budgets.get(effort, -1)

    if "2.5-flash" in model.id:
        budgets = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets.get(effort, -1)

    return -1


def stream_google(
    model: Model,
    context: Context,
    options: GoogleOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream a response from the Google Generative AI API.

    Creates an AssistantMessageEventStream and spawns an asyncio task
    that performs the actual streaming in the background.
    """
    stream = AssistantMessageEventStream()

    async def _run() -> None:
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="google-generative-ai",
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

        try:
            opts = options or GoogleOptions()
            api_key = opts.api_key
            if not api_key:
                raise ValueError(f"No API key for provider: {model.provider}")

            client = _create_client(model, api_key, opts.headers)
            params = _build_params(model, context, opts)

            if opts.on_payload is not None:
                opts.on_payload(params)

            # Check abort before starting
            if opts.signal is not None and opts.signal.is_set():
                raise RuntimeError("Request aborted")

            google_stream = client.models.generate_content_stream(**params)

            stream.push(StartEvent(type="start", partial=output))

            current_block: TextContent | ThinkingContent | None = None
            blocks = output.content

            def block_index() -> int:
                return len(blocks) - 1

            for chunk in google_stream:
                # Check abort during streaming
                if opts.signal is not None and opts.signal.is_set():
                    raise RuntimeError("Request was aborted")

                candidates = getattr(chunk, "candidates", None)
                candidate = candidates[0] if candidates else None
                if candidate is not None:
                    content = getattr(candidate, "content", None)
                    parts = getattr(content, "parts", None) if content else None
                    if parts:
                        for part in parts:
                            part_text = getattr(part, "text", None)
                            if part_text is not None:
                                is_thinking = is_thinking_part(_part_to_dict(part))

                                # Need to start a new block if type changed or no current block
                                if (
                                    current_block is None
                                    or (is_thinking and current_block.type != "thinking")
                                    or (not is_thinking and current_block.type != "text")
                                ):
                                    # Close previous block
                                    if current_block is not None:
                                        if current_block.type == "text":
                                            stream.push(
                                                TextEndEvent(
                                                    type="text_end",
                                                    content_index=block_index(),
                                                    content=current_block.text,
                                                    partial=output,
                                                )
                                            )
                                        else:
                                            assert isinstance(current_block, ThinkingContent)
                                            stream.push(
                                                ThinkingEndEvent(
                                                    type="thinking_end",
                                                    content_index=block_index(),
                                                    content=current_block.thinking,
                                                    partial=output,
                                                )
                                            )

                                    # Open new block
                                    if is_thinking:
                                        current_block = ThinkingContent(
                                            type="thinking", thinking="", thinking_signature=None
                                        )
                                        output.content.append(current_block)
                                        stream.push(
                                            ThinkingStartEvent(
                                                type="thinking_start",
                                                content_index=block_index(),
                                                partial=output,
                                            )
                                        )
                                    else:
                                        current_block = TextContent(type="text", text="")
                                        output.content.append(current_block)
                                        stream.push(
                                            TextStartEvent(
                                                type="text_start",
                                                content_index=block_index(),
                                                partial=output,
                                            )
                                        )

                                thought_sig = getattr(part, "thought_signature", None)
                                if thought_sig is None:
                                    thought_sig = getattr(part, "thoughtSignature", None)

                                if current_block.type == "thinking":
                                    assert isinstance(current_block, ThinkingContent)
                                    current_block.thinking += part_text
                                    current_block.thinking_signature = retain_thought_signature(
                                        current_block.thinking_signature, thought_sig
                                    )
                                    stream.push(
                                        ThinkingDeltaEvent(
                                            type="thinking_delta",
                                            content_index=block_index(),
                                            delta=part_text,
                                            partial=output,
                                        )
                                    )
                                else:
                                    assert isinstance(current_block, TextContent)
                                    current_block.text += part_text
                                    current_block.text_signature = retain_thought_signature(
                                        current_block.text_signature, thought_sig
                                    )
                                    stream.push(
                                        TextDeltaEvent(
                                            type="text_delta",
                                            content_index=block_index(),
                                            delta=part_text,
                                            partial=output,
                                        )
                                    )

                            function_call = getattr(part, "function_call", None)
                            if function_call is not None:
                                # Close current text/thinking block
                                if current_block is not None:
                                    if current_block.type == "text":
                                        stream.push(
                                            TextEndEvent(
                                                type="text_end",
                                                content_index=block_index(),
                                                content=current_block.text,
                                                partial=output,
                                            )
                                        )
                                    else:
                                        assert isinstance(current_block, ThinkingContent)
                                        stream.push(
                                            ThinkingEndEvent(
                                                type="thinking_end",
                                                content_index=block_index(),
                                                content=current_block.thinking,
                                                partial=output,
                                            )
                                        )
                                    current_block = None

                                # Generate unique ID if not provided or duplicate
                                provided_id = getattr(function_call, "id", None)
                                needs_new_id = not provided_id or any(
                                    isinstance(b, ToolCall) and b.id == provided_id for b in output.content
                                )
                                fc_name = getattr(function_call, "name", "") or ""
                                tool_call_id = generate_tool_call_id(fc_name) if needs_new_id else str(provided_id)

                                fc_args = getattr(function_call, "args", None)
                                if fc_args is None:
                                    fc_args = {}
                                elif not isinstance(fc_args, dict):
                                    fc_args = dict(fc_args)

                                thought_sig = getattr(part, "thought_signature", None)
                                if thought_sig is None:
                                    thought_sig = getattr(part, "thoughtSignature", None)

                                tool_call = ToolCall(
                                    type="toolCall",
                                    id=tool_call_id,
                                    name=fc_name,
                                    arguments=fc_args,
                                    thought_signature=thought_sig if thought_sig else None,
                                )

                                output.content.append(tool_call)
                                stream.push(
                                    ToolCallStartEvent(
                                        type="toolcall_start",
                                        content_index=block_index(),
                                        partial=output,
                                    )
                                )
                                stream.push(
                                    ToolCallDeltaEvent(
                                        type="toolcall_delta",
                                        content_index=block_index(),
                                        delta=json.dumps(tool_call.arguments),
                                        partial=output,
                                    )
                                )
                                stream.push(
                                    ToolCallEndEvent(
                                        type="toolcall_end",
                                        content_index=block_index(),
                                        tool_call=tool_call,
                                        partial=output,
                                    )
                                )

                    finish_reason = getattr(candidate, "finish_reason", None)
                    if finish_reason:
                        output.stop_reason = map_stop_reason(str(finish_reason))
                        if any(isinstance(b, ToolCall) for b in output.content):
                            output.stop_reason = "toolUse"

                usage_metadata = getattr(chunk, "usage_metadata", None)
                if usage_metadata is not None:
                    prompt_tokens = getattr(usage_metadata, "prompt_token_count", 0) or 0
                    candidates_tokens = getattr(usage_metadata, "candidates_token_count", 0) or 0
                    thoughts_tokens = getattr(usage_metadata, "thoughts_token_count", 0) or 0
                    cached_tokens = getattr(usage_metadata, "cached_content_token_count", 0) or 0
                    total_tokens = getattr(usage_metadata, "total_token_count", 0) or 0

                    output.usage = Usage(
                        input=prompt_tokens,
                        output=candidates_tokens + thoughts_tokens,
                        cache_read=cached_tokens,
                        cache_write=0,
                        total_tokens=total_tokens,
                        cost=UsageCost(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0, total=0.0),
                    )
                    calculate_cost(model, output.usage)

            # Close final block
            if current_block is not None:
                if current_block.type == "text":
                    stream.push(
                        TextEndEvent(
                            type="text_end",
                            content_index=block_index(),
                            content=current_block.text,
                            partial=output,
                        )
                    )
                else:
                    assert isinstance(current_block, ThinkingContent)
                    stream.push(
                        ThinkingEndEvent(
                            type="thinking_end",
                            content_index=block_index(),
                            content=current_block.thinking,
                            partial=output,
                        )
                    )

            if opts.signal is not None and opts.signal.is_set():
                raise RuntimeError("Request was aborted")

            if output.stop_reason in ("aborted", "error"):
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(type="done", reason=output.stop_reason, message=output))
            stream.end()

        except Exception as error:
            opts = options or GoogleOptions()
            output.stop_reason = "aborted" if (opts.signal is not None and opts.signal.is_set()) else "error"
            output.error_message = str(error)
            stream.push(ErrorEvent(type="error", reason=output.stop_reason, error=output))
            stream.end()

    task = asyncio.ensure_future(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return stream


def stream_simple_google(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream a response from Google with simplified options.

    Handles reasoning level mapping for different Gemini model families.
    """
    api_key = (options.api_key if options else None) or ""
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)

    if not (options and options.reasoning):
        return stream_google(
            model,
            context,
            GoogleOptions(
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
                thinking=GoogleThinkingConfig(enabled=False),
            ),
        )

    effort = clamp_reasoning(options.reasoning)
    assert effort is not None

    if _is_gemini_3_pro_model(model) or _is_gemini_3_flash_model(model):
        return stream_google(
            model,
            context,
            GoogleOptions(
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
                thinking=GoogleThinkingConfig(
                    enabled=True,
                    level=_get_gemini3_thinking_level(effort, model),
                ),
            ),
        )

    return stream_google(
        model,
        context,
        GoogleOptions(
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
            thinking=GoogleThinkingConfig(
                enabled=True,
                budget_tokens=_get_google_budget(model, effort, options.thinking_budgets),
            ),
        ),
    )


def _part_to_dict(part: Any) -> dict[str, Any]:
    """Convert a google-genai Part object to a dict for is_thinking_part()."""
    result: dict[str, Any] = {}
    thought = getattr(part, "thought", None)
    if thought is not None:
        result["thought"] = thought
    text = getattr(part, "text", None)
    if text is not None:
        result["text"] = text
    return result
