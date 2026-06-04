"""Azure OpenAI Responses API provider — ported from packages/ai/src/providers/azure-openai-responses.ts."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

from pi.ai.env_api_keys import get_env_api_key
from pi.ai.models import supports_xhigh
from pi.ai.providers.openai_responses_shared import (
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)
from pi.ai.providers.simple_options import build_base_options, clamp_reasoning
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
    Usage,
)

_DEFAULT_AZURE_API_VERSION = "v1"
_AZURE_TOOL_CALL_PROVIDERS: frozenset[str] = frozenset(["openai", "openai-codex", "opencode", "azure-openai-responses"])


@dataclass
class AzureOpenAIResponsesOptions(StreamOptions):
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    reasoning_summary: Literal["auto", "detailed", "concise"] | None = None
    azure_api_version: str | None = None
    azure_resource_name: str | None = None
    azure_base_url: str | None = None
    azure_deployment_name: str | None = None


def _parse_deployment_name_map(value: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not value:
        return result
    for entry in value.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        model_id, deploy = entry.split("=", 1)
        result[model_id.strip()] = deploy.strip()
    return result


def _resolve_deployment_name(model: Model, options: AzureOpenAIResponsesOptions | None) -> str:
    if options and options.azure_deployment_name:
        return options.azure_deployment_name
    mapped = _parse_deployment_name_map(os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME_MAP")).get(model.id)
    return mapped or model.id


def _normalize_azure_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _build_default_base_url(resource_name: str) -> str:
    return f"https://{resource_name}.openai.azure.com/openai/v1"


def _resolve_azure_config(
    model: Model,
    options: AzureOpenAIResponsesOptions | None,
) -> tuple[str, str]:
    """Return (base_url, api_version)."""
    api_version = (
        (options.azure_api_version if options else None)
        or os.environ.get("AZURE_OPENAI_API_VERSION")
        or _DEFAULT_AZURE_API_VERSION
    )

    base_url: str | None = (
        ((options.azure_base_url or "").strip() if options else None)
        or (os.environ.get("AZURE_OPENAI_BASE_URL") or "").strip()
        or None
    )
    resource_name = (options.azure_resource_name if options else None) or os.environ.get("AZURE_OPENAI_RESOURCE_NAME")

    if not base_url and resource_name:
        base_url = _build_default_base_url(resource_name)
    if not base_url and model.base_url:
        base_url = model.base_url
    if not base_url:
        raise ValueError("Azure OpenAI base URL is required. Set AZURE_OPENAI_BASE_URL or AZURE_OPENAI_RESOURCE_NAME.")

    return _normalize_azure_base_url(base_url), api_version


async def stream_azure_openai_responses(
    model: Model,
    context: Context,
    options: AzureOpenAIResponsesOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """Stream from Azure OpenAI Responses API."""
    import openai

    deployment_name = _resolve_deployment_name(model, options)

    output = AssistantMessage(
        role="assistant",
        content=[],
        api="azure-openai-responses",
        provider=model.provider,
        model=model.id,
        usage=Usage(),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )

    try:
        api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider) or ""
        if not api_key:
            azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
            if not azure_key:
                raise ValueError("Azure OpenAI API key is required.")
            api_key = azure_key

        headers: dict[str, str] = dict(model.headers or {})
        if options and options.headers:
            headers.update(options.headers)

        base_url, api_version = _resolve_azure_config(model, options)

        client = openai.AsyncAzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            base_url=base_url,
            default_headers=headers,
        )

        messages = convert_responses_messages(model, context, _AZURE_TOOL_CALL_PROVIDERS)

        params: dict[str, Any] = {
            "model": deployment_name,
            "input": messages,
            "stream": True,
            "prompt_cache_key": options.session_id if options else None,
        }

        if options and options.max_tokens:
            params["max_output_tokens"] = options.max_tokens
        if options and options.temperature is not None:
            params["temperature"] = options.temperature
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
                if model.name.lower().startswith("gpt-5"):
                    messages.append(
                        {
                            "role": "developer",
                            "content": [{"type": "input_text", "text": "# Juice: 0 !important"}],
                        }
                    )

        if options and options.on_payload:
            options.on_payload(params)

        signal_kwarg = {}
        if options and options.signal:
            signal_kwarg["signal"] = options.signal

        openai_stream = await client.responses.create(**params, **signal_kwarg)  # type: ignore[call-overload]

        yield StartEvent(partial=output)

        async for event in process_responses_stream(openai_stream, output, model):
            # Honor abort signal mid-stream — same reasoning as
            # openai_responses.py. The pre-existing `.aborted` checks below
            # never fire because asyncio.Event has no `aborted` attribute.
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


async def stream_simple_azure_openai_responses(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """High-level Azure OpenAI Responses streaming with automatic reasoning config."""
    api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    reasoning_effort = options.reasoning if options else None
    if not supports_xhigh(model):
        reasoning_effort = clamp_reasoning(reasoning_effort)

    az_options = AzureOpenAIResponsesOptions(
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
    async for event in stream_azure_openai_responses(model, context, az_options):
        yield event


streamAzureOpenAIResponses = stream_azure_openai_responses
streamSimpleAzureOpenAIResponses = stream_simple_azure_openai_responses
