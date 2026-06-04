"""Proxy stream function — Python port of packages/agent/src/proxy.ts.

Uses httpx.AsyncClient for HTTP streaming instead of browser fetch.
Events are received as SSE (Server-Sent Events) and reconstructed client-side.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from pi.agent.types import (
    ProxyAssistantMessageEvent,
    ProxyDoneEvent,
    ProxyErrorEvent,
    ProxyStartEvent,
    ProxyStreamOptions,
    ProxyTextDeltaEvent,
    ProxyTextEndEvent,
    ProxyTextStartEvent,
    ProxyThinkingDeltaEvent,
    ProxyThinkingEndEvent,
    ProxyThinkingStartEvent,
    ProxyToolCallDeltaEvent,
    ProxyToolCallEndEvent,
    ProxyToolCallStartEvent,
)
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    Message,
    Model,
    StartEvent,
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
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from pi.ai.utils.json_parse import parse_streaming_json


def _make_partial(model: Model) -> AssistantMessage:
    """Create the initial partial AssistantMessage for proxy streaming."""
    return AssistantMessage(
        api=model.api,
        provider=model.provider,
        model=model.id,
        stop_reason="stop",
        timestamp=time.time() * 1000,
    )


def _parse_usage(usage_data: Any) -> Usage:
    """Parse usage dict from proxy event into Usage dataclass."""
    if isinstance(usage_data, dict):
        cost_data = usage_data.get("cost", {})
        return Usage(
            input=usage_data.get("input", 0),
            output=usage_data.get("output", 0),
            cache_read=usage_data.get("cacheRead", 0),
            cache_write=usage_data.get("cacheWrite", 0),
            total_tokens=usage_data.get("totalTokens", 0),
            cost=UsageCost(
                input=cost_data.get("input", 0.0),
                output=cost_data.get("output", 0.0),
                cache_read=cost_data.get("cacheRead", 0.0),
                cache_write=cost_data.get("cacheWrite", 0.0),
                total=cost_data.get("total", 0.0),
            ),
        )
    return Usage()


def _serialize_content_block(block: Any) -> dict[str, Any]:
    """Serialize a content block to camelCase JSON matching the TS proxy contract."""
    if isinstance(block, TextContent):
        result: dict[str, Any] = {"type": "text", "text": block.text}
        if block.text_signature is not None:
            result["textSignature"] = block.text_signature
        return result
    if isinstance(block, ThinkingContent):
        result = {"type": "thinking", "thinking": block.thinking}
        if block.thinking_signature is not None:
            result["thinkingSignature"] = block.thinking_signature
        return result
    if isinstance(block, ImageContent):
        return {"type": "image", "data": block.data, "mimeType": block.mime_type}
    if isinstance(block, ToolCall):
        result = {
            "type": "toolCall",
            "id": block.id,
            "name": block.name,
            "arguments": block.arguments,
        }
        if block.thought_signature is not None:
            result["thoughtSignature"] = block.thought_signature
        return result
    return {}


def _serialize_message(msg: Message) -> dict[str, Any]:
    """Serialize a Message dataclass to camelCase JSON matching the TS interface."""
    if isinstance(msg, UserMessage):
        content: str | list[dict[str, Any]] = (
            msg.content if isinstance(msg.content, str) else [_serialize_content_block(b) for b in msg.content]
        )
        return {"role": "user", "content": content, "timestamp": msg.timestamp}
    if isinstance(msg, AssistantMessage):
        result: dict[str, Any] = {
            "role": "assistant",
            "content": [_serialize_content_block(b) for b in msg.content],
            "api": msg.api,
            "provider": msg.provider,
            "model": msg.model,
            "stopReason": msg.stop_reason,
            "timestamp": msg.timestamp,
        }
        if msg.error_message is not None:
            result["errorMessage"] = msg.error_message
        return result
    if isinstance(msg, ToolResultMessage):
        return {
            "role": "toolResult",
            "toolCallId": msg.tool_call_id,
            "toolName": msg.tool_name,
            "content": [_serialize_content_block(b) for b in msg.content],
            "isError": msg.is_error,
            "timestamp": msg.timestamp,
        }
    return {}


def _serialize_context(context: Context) -> dict[str, Any]:
    """Serialize a Context to camelCase JSON matching the TS proxy contract."""
    result: dict[str, Any] = {
        "messages": [_serialize_message(m) for m in context.messages],
    }
    if context.system_prompt is not None:
        result["systemPrompt"] = context.system_prompt
    if context.tools:
        result["tools"] = [
            {"name": t.name, "description": t.description, "parameters": t.parameters} for t in context.tools
        ]
    return result


def _parse_proxy_event(data: dict[str, Any]) -> ProxyAssistantMessageEvent | None:
    """Parse a raw JSON dict into a ProxyAssistantMessageEvent."""
    event_type = data.get("type")
    if event_type == "start":
        return ProxyStartEvent()
    if event_type == "text_start":
        return ProxyTextStartEvent(content_index=data.get("contentIndex", 0))
    if event_type == "text_delta":
        return ProxyTextDeltaEvent(
            content_index=data.get("contentIndex", 0),
            delta=data.get("delta", ""),
        )
    if event_type == "text_end":
        return ProxyTextEndEvent(
            content_index=data.get("contentIndex", 0),
            content_signature=data.get("contentSignature"),
        )
    if event_type == "thinking_start":
        return ProxyThinkingStartEvent(content_index=data.get("contentIndex", 0))
    if event_type == "thinking_delta":
        return ProxyThinkingDeltaEvent(
            content_index=data.get("contentIndex", 0),
            delta=data.get("delta", ""),
        )
    if event_type == "thinking_end":
        return ProxyThinkingEndEvent(
            content_index=data.get("contentIndex", 0),
            content_signature=data.get("contentSignature"),
        )
    if event_type == "toolcall_start":
        return ProxyToolCallStartEvent(
            content_index=data.get("contentIndex", 0),
            id=data.get("id", ""),
            tool_name=data.get("toolName", ""),
        )
    if event_type == "toolcall_delta":
        return ProxyToolCallDeltaEvent(
            content_index=data.get("contentIndex", 0),
            delta=data.get("delta", ""),
        )
    if event_type == "toolcall_end":
        return ProxyToolCallEndEvent(content_index=data.get("contentIndex", 0))
    if event_type == "done":
        return ProxyDoneEvent(
            reason=data.get("reason", "stop"),
            usage=_parse_usage(data.get("usage")),
        )
    if event_type == "error":
        return ProxyErrorEvent(
            reason=data.get("reason", "error"),
            error_message=data.get("errorMessage"),
            usage=_parse_usage(data.get("usage")),
        )
    return None


def _process_proxy_event(
    proxy_event: ProxyAssistantMessageEvent,
    partial: AssistantMessage,
    partial_json: dict[int, str],
) -> AssistantMessageEvent | None:
    """Process a proxy event and update the partial message. Returns LLM event or None."""
    if isinstance(proxy_event, ProxyStartEvent):
        return StartEvent(partial=partial)

    if isinstance(proxy_event, ProxyTextStartEvent):
        idx = proxy_event.content_index
        while len(partial.content) <= idx:
            partial.content.append(TextContent())
        partial.content[idx] = TextContent(text="")
        return TextStartEvent(content_index=idx, partial=partial)

    if isinstance(proxy_event, ProxyTextDeltaEvent):
        idx = proxy_event.content_index
        block = partial.content[idx] if idx < len(partial.content) else None
        if isinstance(block, TextContent):
            block.text += proxy_event.delta
            return TextDeltaEvent(content_index=idx, delta=proxy_event.delta, partial=partial)
        raise ValueError("Received text_delta for non-text content")

    if isinstance(proxy_event, ProxyTextEndEvent):
        idx = proxy_event.content_index
        block = partial.content[idx] if idx < len(partial.content) else None
        if isinstance(block, TextContent):
            block.text_signature = proxy_event.content_signature
            return TextEndEvent(content_index=idx, content=block.text, partial=partial)
        raise ValueError("Received text_end for non-text content")

    if isinstance(proxy_event, ProxyThinkingStartEvent):
        idx = proxy_event.content_index
        while len(partial.content) <= idx:
            partial.content.append(TextContent())
        partial.content[idx] = ThinkingContent(thinking="")
        return ThinkingStartEvent(content_index=idx, partial=partial)

    if isinstance(proxy_event, ProxyThinkingDeltaEvent):
        idx = proxy_event.content_index
        block = partial.content[idx] if idx < len(partial.content) else None
        if isinstance(block, ThinkingContent):
            block.thinking += proxy_event.delta
            return ThinkingDeltaEvent(content_index=idx, delta=proxy_event.delta, partial=partial)
        raise ValueError("Received thinking_delta for non-thinking content")

    if isinstance(proxy_event, ProxyThinkingEndEvent):
        idx = proxy_event.content_index
        block = partial.content[idx] if idx < len(partial.content) else None
        if isinstance(block, ThinkingContent):
            block.thinking_signature = proxy_event.content_signature
            return ThinkingEndEvent(content_index=idx, content=block.thinking, partial=partial)
        raise ValueError("Received thinking_end for non-thinking content")

    if isinstance(proxy_event, ProxyToolCallStartEvent):
        idx = proxy_event.content_index
        while len(partial.content) <= idx:
            partial.content.append(TextContent())
        partial.content[idx] = ToolCall(
            id=proxy_event.id,
            name=proxy_event.tool_name,
            arguments={},
        )
        partial_json[idx] = ""
        return ToolCallStartEvent(content_index=idx, partial=partial)

    if isinstance(proxy_event, ProxyToolCallDeltaEvent):
        idx = proxy_event.content_index
        block = partial.content[idx] if idx < len(partial.content) else None
        if isinstance(block, ToolCall):
            partial_json[idx] = partial_json.get(idx, "") + proxy_event.delta
            parsed = parse_streaming_json(partial_json[idx])
            block.arguments = parsed if isinstance(parsed, dict) else {}
            partial.content[idx] = dataclasses.replace(block)
            return ToolCallDeltaEvent(content_index=idx, delta=proxy_event.delta, partial=partial)
        raise ValueError("Received toolcall_delta for non-toolCall content")

    if isinstance(proxy_event, ProxyToolCallEndEvent):
        idx = proxy_event.content_index
        block = partial.content[idx] if idx < len(partial.content) else None
        if isinstance(block, ToolCall):
            partial_json.pop(idx, None)
            return ToolCallEndEvent(content_index=idx, tool_call=block, partial=partial)
        return None

    if isinstance(proxy_event, ProxyDoneEvent):
        partial.stop_reason = proxy_event.reason
        partial.usage = proxy_event.usage
        return DoneEvent(reason=proxy_event.reason, message=partial)

    if isinstance(proxy_event, ProxyErrorEvent):
        partial.stop_reason = proxy_event.reason
        partial.error_message = proxy_event.error_message
        partial.usage = proxy_event.usage
        return ErrorEvent(reason=proxy_event.reason, error=partial)

    return None


def _process_sse_line(
    line: str,
    partial: AssistantMessage,
    partial_json: dict[int, str],
) -> AssistantMessageEvent | None:
    """Parse one SSE 'data: ...' line into an AssistantMessageEvent, or None to skip."""
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if not data_str:
        return None
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return None
    proxy_event = _parse_proxy_event(data)
    if proxy_event is None:
        return None
    return _process_proxy_event(proxy_event, partial, partial_json)


async def stream_proxy(
    model: Model,
    context: Context,
    options: ProxyStreamOptions,
) -> AsyncIterator[AssistantMessageEvent]:
    """Stream LLM responses through a proxy server using httpx.

    The proxy server manages auth and forwards requests to LLM providers.
    Events are received as SSE (Server-Sent Events) and reconstructed client-side.
    """
    partial = _make_partial(model)
    partial_json: dict[int, str] = {}  # track accumulating JSON per content index

    body = {
        "model": {
            "id": model.id,
            "api": model.api,
            "provider": model.provider,
        },
        "context": _serialize_context(context),
        "options": {
            "temperature": options.temperature,
            "maxTokens": options.max_tokens,
            "reasoning": options.reasoning,
        },
    }

    headers = {
        "Authorization": f"Bearer {options.auth_token}",
        "Content-Type": "application/json",
    }

    signal = options.signal

    async with httpx.AsyncClient() as client:
        try:
            async with client.stream(
                "POST",
                f"{options.proxy_url}/api/stream",
                headers=headers,
                json=body,
                timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_message = f"Proxy error: {response.status_code} {response.reason_phrase}"
                    try:
                        error_data = json.loads(error_body)
                        if isinstance(error_data, dict) and error_data.get("error"):
                            error_message = f"Proxy error: {error_data['error']}"
                    except json.JSONDecodeError:
                        pass
                    raise RuntimeError(error_message)

                buffer = ""
                async for chunk in response.aiter_text():
                    if signal is not None and signal.is_set():
                        raise RuntimeError("Request aborted by user")

                    buffer += chunk
                    lines = buffer.split("\n")
                    buffer = lines[-1]

                    for line in lines[:-1]:
                        event = _process_sse_line(line, partial, partial_json)
                        if event is not None:
                            yield event

        except (httpx.RequestError, RuntimeError) as exc:
            error_message = str(exc)
            reason: str = "aborted" if (signal is not None and signal.is_set()) else "error"
            partial.stop_reason = reason  # type: ignore[assignment]
            partial.error_message = error_message
            yield ErrorEvent(reason=reason, error=partial)  # type: ignore[arg-type]
