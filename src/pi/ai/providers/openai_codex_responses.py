"""OpenAI Codex Responses provider — ported from packages/ai/src/providers/openai-codex-responses.ts."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from pi.ai.env_api_keys import get_env_api_key
from pi.ai.models import supports_xhigh
from pi.ai.providers.openai_responses_shared import (
    ConvertResponsesMessagesOptions,
    ConvertResponsesToolsOptions,
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"
_MAX_RETRIES = 3
_BASE_DELAY_MS = 1000
_CODEX_TOOL_CALL_PROVIDERS: frozenset[str] = frozenset(["openai", "openai-codex", "opencode"])

_CODEX_RESPONSE_STATUSES = frozenset(["completed", "incomplete", "failed", "cancelled", "queued", "in_progress"])

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class OpenAICodexResponsesOptions(StreamOptions):
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    reasoning_summary: Literal["auto", "concise", "detailed", "off", "on"] | None = None
    text_verbosity: Literal["low", "medium", "high"] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_retryable_error(status: int, error_text: str) -> bool:
    if status in (429, 500, 502, 503, 504):
        return True
    return bool(
        re.search(
            r"rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused",
            error_text,
            re.IGNORECASE,
        )
    )


def _resolve_codex_url(base_url: str | None) -> str:
    raw = (base_url or "").strip() or _DEFAULT_CODEX_BASE_URL
    normalized = raw.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def _clamp_reasoning_effort(model_id: str, effort: str) -> str:
    mid = model_id.split("/")[-1] if "/" in model_id else model_id
    if (mid.startswith("gpt-5.2") or mid.startswith("gpt-5.3")) and effort == "minimal":
        return "low"
    if mid == "gpt-5.1" and effort == "xhigh":
        return "high"
    if mid == "gpt-5.1-codex-mini":
        return "high" if effort in ("high", "xhigh") else "medium"
    return effort


def _extract_account_id(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token")
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        account_id = payload.get(_JWT_CLAIM_PATH, {}).get("chatgpt_account_id")
        if not account_id:
            raise ValueError("No account ID in token")
        return str(account_id)
    except Exception as exc:
        raise ValueError(f"Failed to extract accountId from token: {exc}") from exc


def _build_headers(
    init_headers: dict[str, str] | None,
    additional_headers: dict[str, str] | None,
    account_id: str,
    token: str,
    session_id: str | None = None,
) -> dict[str, str]:
    platform = os.uname().sysname.lower() if hasattr(os, "uname") else "unknown"
    headers: dict[str, str] = dict(init_headers or {})
    headers["Authorization"] = f"Bearer {token}"
    headers["chatgpt-account-id"] = account_id
    headers["OpenAI-Beta"] = "responses=experimental"
    headers["originator"] = "pi"
    headers["User-Agent"] = f"pi ({platform})"
    headers["accept"] = "text/event-stream"
    headers["content-type"] = "application/json"
    for k, v in (additional_headers or {}).items():
        headers[k] = v
    if session_id:
        headers["session_id"] = session_id
    return headers


def _build_request_body(
    model: Model,
    context: Context,
    options: OpenAICodexResponsesOptions | None,
) -> dict[str, Any]:
    messages = convert_responses_messages(
        model,
        context,
        _CODEX_TOOL_CALL_PROVIDERS,
        ConvertResponsesMessagesOptions(include_system_prompt=False),
    )

    body: dict[str, Any] = {
        "model": model.id,
        "store": False,
        "stream": True,
        "instructions": context.system_prompt,
        "input": messages,
        "text": {"verbosity": (options.text_verbosity if options else None) or "medium"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": options.session_id if options else None,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    if options and options.temperature is not None:
        body["temperature"] = options.temperature

    if context.tools:
        body["tools"] = convert_responses_tools(context.tools, ConvertResponsesToolsOptions(strict=None))

    if options and options.reasoning_effort is not None:
        body["reasoning"] = {
            "effort": _clamp_reasoning_effort(model.id, options.reasoning_effort),
            "summary": options.reasoning_summary or "auto",
        }

    return body


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


async def _parse_sse(
    response: httpx.Response,
) -> AsyncGenerator[dict[str, Any], None]:
    buffer = ""
    async for raw_chunk in response.aiter_text():
        buffer += raw_chunk
        while "\n\n" in buffer:
            idx = buffer.index("\n\n")
            chunk = buffer[:idx]
            buffer = buffer[idx + 2 :]
            data_lines = [line[5:].strip() for line in chunk.split("\n") if line.startswith("data:")]
            if data_lines:
                data = "\n".join(data_lines).strip()
                if data and data != "[DONE]":
                    with contextlib.suppress(json.JSONDecodeError):
                        yield json.loads(data)


async def _map_codex_events(
    events: AsyncGenerator[dict[str, Any], None],
) -> AsyncGenerator[Any, None]:
    async for event in events:
        etype = event.get("type")
        if not etype:
            continue
        if etype == "error":
            code = event.get("code", "")
            msg = event.get("message", "")
            raise RuntimeError(f"Codex error: {msg or code or json.dumps(event)}")
        if etype == "response.failed":
            err_msg = (event.get("response") or {}).get("error", {}).get("message", "")
            raise RuntimeError(err_msg or "Codex response failed")
        if etype in ("response.done", "response.completed"):
            response = event.get("response", {})
            if response:
                status = response.get("status")
                if status not in _CODEX_RESPONSE_STATUSES:
                    status = None
                yield {**event, "type": "response.completed", "response": {**response, "status": status}}
            else:
                yield {**event, "type": "response.completed"}
            continue
        yield event


# ---------------------------------------------------------------------------
# Main stream function
# ---------------------------------------------------------------------------


async def stream_openai_codex_responses(
    model: Model,
    context: Context,
    options: OpenAICodexResponsesOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """Stream from OpenAI Codex Responses API via direct HTTP/SSE."""
    output = AssistantMessage(
        role="assistant",
        content=[],
        api="openai-codex-responses",
        provider=model.provider,
        model=model.id,
        usage=Usage(),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )

    try:
        transport = options.transport if options and options.transport else "sse"
        if transport != "sse":
            raise NotImplementedError(
                f"Transport '{transport}' is not implemented for OpenAI Codex Responses; only 'sse' is supported."
            )

        api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider) or ""
        if not api_key:
            raise ValueError(f"No API key for provider: {model.provider}")

        account_id = _extract_account_id(api_key)
        body = _build_request_body(model, context, options)

        if options and options.on_payload:
            options.on_payload(body)

        request_headers = _build_headers(
            model.headers,
            options.headers if options else None,
            account_id,
            api_key,
            options.session_id if options else None,
        )

        url = _resolve_codex_url(model.base_url)
        body_json = json.dumps(body)

        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            # Pre-attempt abort check: asyncio.Event.is_set(), not the
            # non-existent .aborted attribute from the TS port.
            if options and options.signal is not None and options.signal.is_set():
                raise RuntimeError("Request was aborted")

            try:
                async with (
                    httpx.AsyncClient(timeout=300.0) as client,
                    client.stream(
                        "POST",
                        url,
                        headers=request_headers,
                        content=body_json,
                    ) as response,
                ):
                    if response.status_code == 200:
                        yield StartEvent(partial=output)
                        async for event in process_responses_stream(
                            _map_codex_events(_parse_sse(response)), output, model
                        ):
                            # Per-chunk abort check — truncates the stream
                            # mid-response when Stop is clicked.
                            if options and options.signal is not None and options.signal.is_set():
                                raise RuntimeError("Request was aborted")
                            yield event

                        if options and options.signal is not None and options.signal.is_set():
                            raise RuntimeError("Request was aborted")
                        if output.stop_reason in ("aborted", "error"):
                            raise RuntimeError("An unknown error occurred")

                        yield DoneEvent(reason=output.stop_reason, message=output)
                        return

                    error_text = await response.aread()
                    error_text_str = error_text.decode("utf-8", errors="replace")

                    if attempt < _MAX_RETRIES and _is_retryable_error(response.status_code, error_text_str):
                        delay = _BASE_DELAY_MS * (2**attempt) / 1000
                        await asyncio.sleep(delay)
                        continue

                    raise RuntimeError(f"HTTP {response.status_code}: {error_text_str}")

            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY_MS * (2**attempt) / 1000
                    await asyncio.sleep(delay)
                    continue
                raise

        raise last_error or RuntimeError("Failed after retries")

    except Exception as exc:
        output.stop_reason = (
            "aborted"
            if options and options.signal is not None and options.signal.is_set()
            else "error"
        )
        output.error_message = str(exc)
        yield ErrorEvent(reason=output.stop_reason, error=output)


async def stream_simple_openai_codex_responses(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AsyncGenerator[AssistantMessageEvent, None]:
    """High-level Codex streaming with automatic reasoning config."""
    api_key = (options.api_key if options and options.api_key else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    reasoning_effort = options.reasoning if options else None
    if not supports_xhigh(model):
        reasoning_effort = clamp_reasoning(reasoning_effort)

    codex_options = OpenAICodexResponsesOptions(
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
    async for event in stream_openai_codex_responses(model, context, codex_options):
        yield event


streamOpenAICodexResponses = stream_openai_codex_responses
streamSimpleOpenAICodexResponses = stream_simple_openai_codex_responses
