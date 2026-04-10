"""Google Vertex AI streaming provider.

Ported from packages/ai/src/providers/google-vertex.ts.
"""

from __future__ import annotations

import asyncio
import json
import os
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
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from pi.ai.utils.sanitize_unicode import sanitize_surrogates

# Google thinking level type
GoogleThinkingLevel = Literal["THINKING_LEVEL_UNSPECIFIED", "MINIMAL", "LOW", "MEDIUM", "HIGH"]

API_VERSION = "v1"

# Set to prevent background tasks from being garbage collected
_background_tasks: set[asyncio.Task[None]] = set()


@dataclass
class GoogleVertexThinkingConfig:
    enabled: bool = False
    budget_tokens: int | None = None  # -1 for dynamic, 0 to disable
    level: GoogleThinkingLevel | None = None


@dataclass
class GoogleVertexOptions(StreamOptions):
    tool_choice: Literal["auto", "none", "any"] | None = None
    thinking: GoogleVertexThinkingConfig = field(default_factory=GoogleVertexThinkingConfig)
    project: str | None = None
    location: str | None = None


def _resolve_project(options: GoogleVertexOptions | None) -> str:
    """Resolve Vertex AI project ID from options or environment variables."""
    project = (
        (options.project if options else None)
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
    )
    if not project:
        raise ValueError(
            "Vertex AI requires a project ID. Set GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT or pass project in options."
        )
    return project


def _resolve_location(options: GoogleVertexOptions | None) -> str:
    """Resolve Vertex AI location from options or environment variables."""
    location = (options.location if options else None) or os.environ.get("GOOGLE_CLOUD_LOCATION")
    if not location:
        raise ValueError("Vertex AI requires a location. Set GOOGLE_CLOUD_LOCATION or pass location in options.")
    return location


def _create_client(
    model: Model,
    project: str,
    location: str,
    options_headers: dict[str, str] | None = None,
) -> genai.Client:
    """Create a Google GenAI client configured for Vertex AI."""
    merged_headers: dict[str, str] = {}
    if model.headers:
        merged_headers.update(model.headers)
    if options_headers:
        merged_headers.update(options_headers)

    http_options: HttpOptions | None = None
    if merged_headers:
        http_options = HttpOptions(headers=merged_headers)

    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=http_options,
    )


def _build_params(
    model: Model,
    context: Context,
    options: GoogleVertexOptions | None = None,
) -> dict[str, Any]:
    """Build parameters for the generateContentStream call."""
    contents = convert_messages(model, context)

    generation_config: dict[str, Any] = {}
    if options and options.temperature is not None:
        generation_config["temperature"] = options.temperature
    if options and options.max_tokens is not None:
        generation_config["maxOutputTokens"] = options.max_tokens

    config: dict[str, Any] = {}
    if generation_config:
        config.update(generation_config)
    if context.system_prompt:
        config["systemInstruction"] = sanitize_surrogates(context.system_prompt)
    if context.tools and len(context.tools) > 0:
        config["tools"] = convert_tools(context.tools)

    if context.tools and len(context.tools) > 0 and options and options.tool_choice:
        config["toolConfig"] = {
            "functionCallingConfig": {
                "mode": map_tool_choice(options.tool_choice),
            },
        }

    if options and options.thinking.enabled and model.reasoning:
        thinking_config: dict[str, Any] = {"includeThoughts": True}
        if options.thinking.level is not None:
            thinking_config["thinkingLevel"] = options.thinking.level
        elif options.thinking.budget_tokens is not None:
            thinking_config["thinkingBudget"] = options.thinking.budget_tokens
        config["thinkingConfig"] = thinking_config

    params: dict[str, Any] = {
        "model": model.id,
        "contents": contents,
        "config": config,
    }

    return params


def _is_gemini_3_pro_model(model: Model) -> bool:
    return "3-pro" in model.id


def _is_gemini_3_flash_model(model: Model) -> bool:
    return "3-flash" in model.id


def _get_gemini_3_thinking_level(effort: str, model: Model) -> GoogleThinkingLevel:
    """Get thinking level for Gemini 3 models."""
    if _is_gemini_3_pro_model(model):
        if effort in ("minimal", "low"):
            return "LOW"
        return "HIGH"
    level_map: dict[str, GoogleThinkingLevel] = {
        "minimal": "MINIMAL",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }
    return level_map.get(effort, "HIGH")


def _get_google_budget(model: Model, effort: str, custom_budgets: Any = None) -> int:
    """Get thinking budget tokens for 2.5 models."""
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


def stream_google_vertex(
    model: Model,
    context: Context,
    options: GoogleVertexOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream a response from Google Vertex AI."""
    stream = AssistantMessageEventStream()

    async def _run() -> None:
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="google-vertex",
            provider=model.provider,
            model=model.id,
            usage=Usage(cost=UsageCost()),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

        try:
            project = _resolve_project(options)
            location = _resolve_location(options)
            client = _create_client(model, project, location, options.headers if options else None)
            params = _build_params(model, context, options)
            if options and options.on_payload:
                options.on_payload(params)

            # Check abort signal before starting
            if options and options.signal and options.signal.is_set():
                raise RuntimeError("Request aborted")

            google_stream = await asyncio.to_thread(
                client.models.generate_content_stream,
                **params,
            )

            stream.push(StartEvent(partial=output))
            current_block: TextContent | ThinkingContent | None = None
            blocks = output.content

            def block_index() -> int:
                return len(blocks) - 1

            for chunk in google_stream:
                # Check abort signal
                if options and options.signal and options.signal.is_set():
                    raise RuntimeError("Request was aborted")

                candidates = getattr(chunk, "candidates", None)
                candidate = candidates[0] if candidates else None
                if candidate:
                    content = getattr(candidate, "content", None)
                    parts = getattr(content, "parts", None) if content else None
                    if parts:
                        for part in parts:
                            part_text = getattr(part, "text", None)
                            if part_text is not None:
                                part_dict: dict[str, Any] = {}
                                if hasattr(part, "thought"):
                                    part_dict["thought"] = part.thought
                                if hasattr(part, "thought_signature"):
                                    part_dict["thoughtSignature"] = part.thought_signature

                                is_thinking = is_thinking_part(part_dict)
                                if (
                                    current_block is None
                                    or (is_thinking and current_block.type != "thinking")
                                    or (not is_thinking and current_block.type != "text")
                                ):
                                    if current_block is not None:
                                        if current_block.type == "text":
                                            assert isinstance(current_block, TextContent)
                                            stream.push(
                                                TextEndEvent(
                                                    content_index=block_index(),
                                                    content=current_block.text,
                                                    partial=output,
                                                )
                                            )
                                        else:
                                            assert isinstance(current_block, ThinkingContent)
                                            stream.push(
                                                ThinkingEndEvent(
                                                    content_index=block_index(),
                                                    content=current_block.thinking,
                                                    partial=output,
                                                )
                                            )
                                    if is_thinking:
                                        current_block = ThinkingContent(thinking="")
                                        output.content.append(current_block)
                                        stream.push(ThinkingStartEvent(content_index=block_index(), partial=output))
                                    else:
                                        current_block = TextContent(text="")
                                        output.content.append(current_block)
                                        stream.push(TextStartEvent(content_index=block_index(), partial=output))

                                thought_sig = getattr(part, "thought_signature", None)
                                if isinstance(current_block, ThinkingContent):
                                    current_block.thinking += part_text
                                    current_block.thinking_signature = retain_thought_signature(
                                        current_block.thinking_signature, thought_sig
                                    )
                                    stream.push(
                                        ThinkingDeltaEvent(
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
                                            content_index=block_index(),
                                            delta=part_text,
                                            partial=output,
                                        )
                                    )

                            function_call = getattr(part, "function_call", None)
                            if function_call:
                                if current_block is not None:
                                    if isinstance(current_block, TextContent):
                                        stream.push(
                                            TextEndEvent(
                                                content_index=block_index(),
                                                content=current_block.text,
                                                partial=output,
                                            )
                                        )
                                    else:
                                        assert isinstance(current_block, ThinkingContent)
                                        stream.push(
                                            ThinkingEndEvent(
                                                content_index=block_index(),
                                                content=current_block.thinking,
                                                partial=output,
                                            )
                                        )
                                    current_block = None

                                provided_id = getattr(function_call, "id", None)
                                needs_new_id = not provided_id or any(
                                    isinstance(b, ToolCall) and b.id == provided_id for b in output.content
                                )
                                fc_name = getattr(function_call, "name", "") or ""
                                tool_call_id = generate_tool_call_id(fc_name) if needs_new_id else str(provided_id)

                                fc_args = getattr(function_call, "args", None) or {}
                                if not isinstance(fc_args, dict):
                                    fc_args = dict(fc_args)

                                thought_sig = getattr(part, "thought_signature", None)
                                tool_call = ToolCall(
                                    id=tool_call_id,
                                    name=fc_name,
                                    arguments=fc_args,
                                    thought_signature=thought_sig if thought_sig else None,
                                )

                                output.content.append(tool_call)
                                stream.push(ToolCallStartEvent(content_index=block_index(), partial=output))
                                stream.push(
                                    ToolCallDeltaEvent(
                                        content_index=block_index(),
                                        delta=json.dumps(tool_call.arguments),
                                        partial=output,
                                    )
                                )
                                stream.push(
                                    ToolCallEndEvent(
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
                if usage_metadata:
                    output.usage = Usage(
                        input=getattr(usage_metadata, "prompt_token_count", 0) or 0,
                        output=(getattr(usage_metadata, "candidates_token_count", 0) or 0)
                        + (getattr(usage_metadata, "thoughts_token_count", 0) or 0),
                        cache_read=getattr(usage_metadata, "cached_content_token_count", 0) or 0,
                        cache_write=0,
                        total_tokens=getattr(usage_metadata, "total_token_count", 0) or 0,
                        cost=UsageCost(),
                    )
                    calculate_cost(model, output.usage)

            # Close any remaining block
            if current_block is not None:
                if isinstance(current_block, TextContent):
                    stream.push(
                        TextEndEvent(
                            content_index=block_index(),
                            content=current_block.text,
                            partial=output,
                        )
                    )
                else:
                    assert isinstance(current_block, ThinkingContent)
                    stream.push(
                        ThinkingEndEvent(
                            content_index=block_index(),
                            content=current_block.thinking,
                            partial=output,
                        )
                    )

            if options and options.signal and options.signal.is_set():
                raise RuntimeError("Request was aborted")

            if output.stop_reason in ("aborted", "error"):
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stop_reason, message=output))
            stream.end()

        except Exception as exc:
            output.stop_reason = "aborted" if (options and options.signal and options.signal.is_set()) else "error"
            output.error_message = str(exc)
            stream.push(ErrorEvent(reason=output.stop_reason, error=output))
            stream.end()

    task = asyncio.ensure_future(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return stream


def stream_simple_google_vertex(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream with simplified options, auto-configuring thinking based on reasoning level."""
    base = build_base_options(model, options)

    if not options or not options.reasoning:
        return stream_google_vertex(
            model,
            context,
            GoogleVertexOptions(
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
                thinking=GoogleVertexThinkingConfig(enabled=False),
            ),
        )

    effort = clamp_reasoning(options.reasoning)
    assert effort is not None

    if _is_gemini_3_pro_model(model) or _is_gemini_3_flash_model(model):
        return stream_google_vertex(
            model,
            context,
            GoogleVertexOptions(
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
                thinking=GoogleVertexThinkingConfig(
                    enabled=True,
                    level=_get_gemini_3_thinking_level(effort, model),
                ),
            ),
        )

    return stream_google_vertex(
        model,
        context,
        GoogleVertexOptions(
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
            thinking=GoogleVertexThinkingConfig(
                enabled=True,
                budget_tokens=_get_google_budget(model, effort, options.thinking_budgets),
            ),
        ),
    )
