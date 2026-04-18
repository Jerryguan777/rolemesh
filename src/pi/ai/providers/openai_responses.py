"""OpenAI Responses API provider — ported from packages/ai/src/providers/openai-responses.ts."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

from pi.ai.env_api_keys import get_env_api_key
from pi.ai.models import supports_xhigh
from pi.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from pi.ai.providers.openai_responses_shared import (
    OpenAIResponsesStreamOptions,
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)
from pi.ai.providers.simple_options import build_base_options, clamp_reasoning
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
    Usage,
)

_OPENAI_TOOL_CALL_PROVIDERS: frozenset[str] = frozenset(["openai", "openai-codex", "opencode"])


def _resolve_cache_retention(cache_retention: CacheRetention | None) -> CacheRetention:
    if cache_retention:
        return cache_retention
    if os.environ.get("PI_CACHE_RETENTION") == "long":
        return "long"
    return "short"


def _get_prompt_cache_retention(base_url: str, cache_retention: CacheRetention) -> str | None:
    if cache_retention != "long":
        return None
    if "api.openai.com" in base_url:
        return "24h"
    return None


@dataclass
class OpenAIResponsesOptions(StreamOptions):
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    reasoning_summary: Literal["auto", "detailed", "concise"] | None = None
    service_tier: str | None = None


def _get_service_tier_multiplier(service_tier: str | None) -> float:
    if service_tier == "flex":
        return 0.5
    if service_tier == "priority":
        return 2.0
    return 1.0


def _apply_service_tier_pricing(usage: Usage, service_tier: str | None) -> None:
    multiplier = _get_service_tier_multiplier(service_tier)
    if multiplier == 1.0:
        return
    usage.cost.input *= multiplier
    usage.cost.output *= multiplier
    usage.cost.cache_read *= multiplier
    usage.cost.cache_write *= multiplier
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cache_read + usage.cost.cache_write


async def stream_openai_responses(
    model: Model,
    context: Context,
    options: OpenAIResponsesOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """Stream from the OpenAI Responses API."""
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

        messages = convert_responses_messages(model, context, _OPENAI_TOOL_CALL_PROVIDERS)

        cache_retention = _resolve_cache_retention(options.cache_retention if options else None)
        params: dict[str, Any] = {
            "model": model.id,
            "input": messages,
            "stream": True,
            "prompt_cache_key": ((options.session_id if options else None) if cache_retention != "none" else None),
            "prompt_cache_retention": _get_prompt_cache_retention(model.base_url, cache_retention),
            "store": False,
        }

        if options and options.max_tokens:
            params["max_output_tokens"] = options.max_tokens
        if options and options.temperature is not None:
            params["temperature"] = options.temperature
        if options and options.service_tier is not None:
            params["service_tier"] = options.service_tier
        if context.tools:
            params["tools"] = convert_responses_tools(context.tools)

        if model.reasoning:
            reasoning_effort = options.reasoning_effort if options else None
            reasoning_summary = options.reasoning_summary if options else None
            if reasoning_effort or reasoning_summary:
                params["reasoning"] = {
                    "effort": reasoning_effort or "medium",
                    "summary": reasoning_summary or "auto",
                }
                params["include"] = ["reasoning.encrypted_content"]
            else:
                if model.name.startswith("gpt-5"):
                    messages.append(
                        {
                            "role": "developer",
                            "content": [{"type": "input_text", "text": "# Juice: 0 !important"}],
                        }
                    )

        if options and options.on_payload:
            options.on_payload(params)

        # Note: Python OpenAI SDK does not support signal/AbortController.
        # Abort is handled via options.signal (asyncio.Event) checked between chunks.
        openai_stream = await client.responses.create(**params)

        yield StartEvent(partial=output)

        stream_options = OpenAIResponsesStreamOptions(
            service_tier=options.service_tier if options else None,
            apply_service_tier_pricing=_apply_service_tier_pricing,
        )

        async for event in process_responses_stream(openai_stream, output, model, stream_options):
            # Honor abort signal mid-stream. Pi's port uses asyncio.Event
            # (is_set()); the pre-existing `.aborted` attribute checks below
            # inherited from the TS AbortSignal origin never fire because
            # asyncio.Event has no `aborted` attribute. Checking per chunk
            # is the point where the Stop button actually truncates an
            # in-flight LLM response.
            if options and options.signal is not None and options.signal.is_set():
                raise RuntimeError("Request was aborted")
            yield event

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


async def stream_simple_openai_responses(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """High-level OpenAI Responses streaming with automatic reasoning config."""
    api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    reasoning_effort = options.reasoning if options else None
    if not supports_xhigh(model):
        reasoning_effort = clamp_reasoning(reasoning_effort)

    resp_options = OpenAIResponsesOptions(
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
    )
    async for event in stream_openai_responses(model, context, resp_options):
        yield event


streamOpenAIResponses = stream_openai_responses
streamSimpleOpenAIResponses = stream_simple_openai_responses
