"""Core types for the pi.ai package — Python port of packages/ai/src/types.ts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

# --- API and Provider enums ---


class KnownApi(StrEnum):
    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"
    AZURE_OPENAI_RESPONSES = "azure-openai-responses"
    OPENAI_CODEX_RESPONSES = "openai-codex-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    BEDROCK_CONVERSE_STREAM = "bedrock-converse-stream"
    GOOGLE_GENERATIVE_AI = "google-generative-ai"
    GOOGLE_GEMINI_CLI = "google-gemini-cli"
    GOOGLE_VERTEX = "google-vertex"


# Api is any string (KnownApi or custom)
Api = str


class KnownProvider(StrEnum):
    AMAZON_BEDROCK = "amazon-bedrock"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GOOGLE_GEMINI_CLI = "google-gemini-cli"
    GOOGLE_ANTIGRAVITY = "google-antigravity"
    GOOGLE_VERTEX = "google-vertex"
    OPENAI = "openai"
    AZURE_OPENAI_RESPONSES = "azure-openai-responses"
    OPENAI_CODEX = "openai-codex"
    GITHUB_COPILOT = "github-copilot"
    XAI = "xai"
    GROQ = "groq"
    CEREBRAS = "cerebras"
    OPENROUTER = "openrouter"
    VERCEL_AI_GATEWAY = "vercel-ai-gateway"
    ZAI = "zai"
    MISTRAL = "mistral"
    MINIMAX = "minimax"
    MINIMAX_CN = "minimax-cn"
    HUGGINGFACE = "huggingface"
    OPENCODE = "opencode"
    KIMI_CODING = "kimi-coding"


# Provider is any string (KnownProvider or custom)
Provider = str

ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]

CacheRetention = Literal["none", "short", "long"]

Transport = Literal["sse", "websocket", "auto"]

StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]


# --- Data classes ---


@dataclass
class ThinkingBudgets:
    minimal: int | None = None
    low: int | None = None
    medium: int | None = None
    high: int | None = None


@dataclass
class StreamOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    signal: asyncio.Event | None = None
    api_key: str | None = None
    transport: Transport | None = None
    cache_retention: CacheRetention | None = None
    session_id: str | None = None
    on_payload: Callable[[Any], None] | None = None
    headers: dict[str, str] | None = None
    max_retry_delay_ms: int | None = None
    metadata: dict[str, Any] | None = None


# ProviderStreamOptions is StreamOptions with arbitrary extra keys.
# In TS this is `StreamOptions & Record<string, unknown>`.
# In Python we use a subclass that accepts extra kwargs via a dict field.
@dataclass
class ProviderStreamOptions(StreamOptions):
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimpleStreamOptions(StreamOptions):
    reasoning: ThinkingLevel | None = None
    thinking_budgets: ThinkingBudgets | None = None


@dataclass
class TextContent:
    type: Literal["text"] = "text"
    text: str = ""
    text_signature: str | None = None


@dataclass
class ThinkingContent:
    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    thinking_signature: str | None = None


@dataclass
class ImageContent:
    type: Literal["image"] = "image"
    data: str = ""  # base64 encoded
    mime_type: str = ""


@dataclass
class ToolCall:
    type: Literal["toolCall"] = "toolCall"
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    thought_signature: str | None = None  # Google-specific


# Content block union type
ContentBlock = TextContent | ThinkingContent | ImageContent | ToolCall
AssistantContentBlock = TextContent | ThinkingContent | ToolCall
UserContentBlock = TextContent | ImageContent


@dataclass
class UsageCost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0
    cost: UsageCost = field(default_factory=UsageCost)


@dataclass
class UserMessage:
    role: Literal["user"] = "user"
    content: str | list[UserContentBlock] = ""
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


@dataclass
class AssistantMessage:
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContentBlock] = field(default_factory=list)
    api: Api = ""
    provider: Provider = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


@dataclass
class ToolResultMessage:
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str = ""
    tool_name: str = ""
    content: list[UserContentBlock] = field(default_factory=list)
    details: Any = None  # TS: unknown — opaque tool-specific data
    is_error: bool = False
    timestamp: float = field(default_factory=lambda: time.time() * 1000)


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass
class Tool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON Schema dict


@dataclass
class Context:
    system_prompt: str | None = None
    messages: list[Message] = field(default_factory=list)
    tools: list[Tool] | None = None


# --- Streaming events (discriminated union) ---


@dataclass
class StartEvent:
    type: Literal["start"] = "start"
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class TextStartEvent:
    type: Literal["text_start"] = "text_start"
    content_index: int = 0
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class TextDeltaEvent:
    type: Literal["text_delta"] = "text_delta"
    content_index: int = 0
    delta: str = ""
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class TextEndEvent:
    type: Literal["text_end"] = "text_end"
    content_index: int = 0
    content: str = ""
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ThinkingStartEvent:
    type: Literal["thinking_start"] = "thinking_start"
    content_index: int = 0
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ThinkingDeltaEvent:
    type: Literal["thinking_delta"] = "thinking_delta"
    content_index: int = 0
    delta: str = ""
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ThinkingEndEvent:
    type: Literal["thinking_end"] = "thinking_end"
    content_index: int = 0
    content: str = ""
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ToolCallStartEvent:
    type: Literal["toolcall_start"] = "toolcall_start"
    content_index: int = 0
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ToolCallDeltaEvent:
    type: Literal["toolcall_delta"] = "toolcall_delta"
    content_index: int = 0
    delta: str = ""
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ToolCallEndEvent:
    type: Literal["toolcall_end"] = "toolcall_end"
    content_index: int = 0
    tool_call: ToolCall = field(default_factory=ToolCall)
    partial: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class DoneEvent:
    type: Literal["done"] = "done"
    reason: StopReason = "stop"
    message: AssistantMessage = field(default_factory=AssistantMessage)


@dataclass
class ErrorEvent:
    type: Literal["error"] = "error"
    reason: Literal["aborted", "error"] = "error"
    error: AssistantMessage = field(default_factory=AssistantMessage)


AssistantMessageEvent = (
    StartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ThinkingStartEvent
    | ThinkingDeltaEvent
    | ThinkingEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | DoneEvent
    | ErrorEvent
)


# --- Compatibility types ---


@dataclass
class OpenRouterRouting:
    only: list[str] | None = None
    order: list[str] | None = None


@dataclass
class VercelGatewayRouting:
    only: list[str] | None = None
    order: list[str] | None = None


@dataclass
class OpenAICompletionsCompat:
    supports_store: bool | None = None
    supports_developer_role: bool | None = None
    supports_reasoning_effort: bool | None = None
    supports_usage_in_streaming: bool | None = None
    max_tokens_field: Literal["max_completion_tokens", "max_tokens"] | None = None
    requires_tool_result_name: bool | None = None
    requires_assistant_after_tool_result: bool | None = None
    requires_thinking_as_text: bool | None = None
    requires_mistral_tool_ids: bool | None = None
    thinking_format: Literal["openai", "zai", "qwen"] | None = None
    open_router_routing: OpenRouterRouting | None = None
    vercel_gateway_routing: VercelGatewayRouting | None = None
    supports_strict_mode: bool | None = None


@dataclass
class OpenAIResponsesCompat:
    pass  # Reserved for future use


CompatType = OpenAICompletionsCompat | OpenAIResponsesCompat | None


# --- Model ---


@dataclass
class ModelCost:
    input: float = 0.0  # $/million tokens
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


@dataclass
class Model:
    id: str = ""
    name: str = ""
    api: Api = ""
    provider: Provider = ""
    base_url: str = ""
    reasoning: bool = False
    input: list[str] = field(default_factory=lambda: ["text"])  # "text" | "image"
    cost: ModelCost = field(default_factory=ModelCost)
    context_window: int = 0
    max_tokens: int = 0
    headers: dict[str, str] | None = None
    compat: CompatType = None


# --- Serialization ---


def serialize_usage_cost(cost: UsageCost) -> dict[str, float]:
    return {
        "input": cost.input,
        "output": cost.output,
        "cacheRead": cost.cache_read,
        "cacheWrite": cost.cache_write,
        "total": cost.total,
    }


def deserialize_usage_cost(data: dict[str, Any]) -> UsageCost:
    return UsageCost(
        input=data.get("input", 0.0),
        output=data.get("output", 0.0),
        cache_read=data.get("cacheRead", 0.0),
        cache_write=data.get("cacheWrite", 0.0),
        total=data.get("total", 0.0),
    )


def serialize_usage(usage: Usage) -> dict[str, Any]:
    return {
        "input": usage.input,
        "output": usage.output,
        "cacheRead": usage.cache_read,
        "cacheWrite": usage.cache_write,
        "totalTokens": usage.total_tokens,
        "cost": serialize_usage_cost(usage.cost),
    }


def deserialize_usage(data: dict[str, Any]) -> Usage:
    return Usage(
        input=data.get("input", 0),
        output=data.get("output", 0),
        cache_read=data.get("cacheRead", 0),
        cache_write=data.get("cacheWrite", 0),
        total_tokens=data.get("totalTokens", 0),
        cost=deserialize_usage_cost(data.get("cost", {})),
    )


def serialize_content_block(block: ContentBlock) -> dict[str, Any]:
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
    raise ValueError(f"Unknown content block type: {type(block)}")  # pragma: no cover


def deserialize_content_block(data: dict[str, Any]) -> ContentBlock:
    block_type = data.get("type")
    if block_type == "text":
        return TextContent(
            text=data.get("text", ""),
            text_signature=data.get("textSignature"),
        )
    if block_type == "thinking":
        return ThinkingContent(
            thinking=data.get("thinking", ""),
            thinking_signature=data.get("thinkingSignature"),
        )
    if block_type == "image":
        return ImageContent(
            data=data.get("data", ""),
            mime_type=data.get("mimeType", ""),
        )
    if block_type == "toolCall":
        return ToolCall(
            id=data.get("id", ""),
            name=data.get("name", ""),
            arguments=data.get("arguments", {}),
            thought_signature=data.get("thoughtSignature"),
        )
    raise ValueError(f"Unknown content block type: {block_type}")


def serialize_message(msg: Message) -> dict[str, Any]:
    if isinstance(msg, UserMessage):
        content: Any = (
            msg.content if isinstance(msg.content, str) else [serialize_content_block(b) for b in msg.content]
        )
        return {"role": "user", "content": content, "timestamp": msg.timestamp}

    if isinstance(msg, AssistantMessage):
        result: dict[str, Any] = {
            "role": "assistant",
            "content": [serialize_content_block(b) for b in msg.content],
            "api": msg.api,
            "provider": msg.provider,
            "model": msg.model,
            "usage": serialize_usage(msg.usage),
            "stopReason": msg.stop_reason,
            "timestamp": msg.timestamp,
        }
        if msg.error_message is not None:
            result["errorMessage"] = msg.error_message
        return result

    if isinstance(msg, ToolResultMessage):
        result = {
            "role": "toolResult",
            "toolCallId": msg.tool_call_id,
            "toolName": msg.tool_name,
            "content": [serialize_content_block(b) for b in msg.content],
            "isError": msg.is_error,
            "timestamp": msg.timestamp,
        }
        if msg.details is not None:
            result["details"] = msg.details
        return result

    raise ValueError(f"Unknown message type: {type(msg)}")  # pragma: no cover


def deserialize_message(data: dict[str, Any]) -> Message:
    role = data.get("role")
    if role == "user":
        content: str | list[UserContentBlock]
        raw_content = data.get("content", "")
        if isinstance(raw_content, str):
            content = raw_content
        else:
            blocks = [deserialize_content_block(b) for b in raw_content]
            content = [b for b in blocks if isinstance(b, (TextContent, ImageContent))]
        return UserMessage(
            content=content,
            timestamp=data.get("timestamp", 0),
        )

    if role == "assistant":
        blocks_raw = data.get("content", [])
        assistant_blocks: list[AssistantContentBlock] = []
        for b in blocks_raw:
            block = deserialize_content_block(b)
            if isinstance(block, (TextContent, ThinkingContent, ToolCall)):
                assistant_blocks.append(block)
        return AssistantMessage(
            content=assistant_blocks,
            api=data.get("api", ""),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            usage=deserialize_usage(data.get("usage", {})),
            stop_reason=data.get("stopReason", "stop"),
            error_message=data.get("errorMessage"),
            timestamp=data.get("timestamp", 0),
        )

    if role == "toolResult":
        blocks_raw = data.get("content", [])
        tool_blocks: list[UserContentBlock] = []
        for b in blocks_raw:
            block = deserialize_content_block(b)
            if isinstance(block, (TextContent, ImageContent)):
                tool_blocks.append(block)
        return ToolResultMessage(
            tool_call_id=data.get("toolCallId", ""),
            tool_name=data.get("toolName", ""),
            content=tool_blocks,
            details=data.get("details"),
            is_error=data.get("isError", False),
            timestamp=data.get("timestamp", 0),
        )

    raise ValueError(f"Unknown message role: {role}")


def serialize_model(model: Model) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": model.id,
        "name": model.name,
        "api": model.api,
        "provider": model.provider,
        "baseUrl": model.base_url,
        "reasoning": model.reasoning,
        "input": model.input,
        "cost": {
            "input": model.cost.input,
            "output": model.cost.output,
            "cacheRead": model.cost.cache_read,
            "cacheWrite": model.cost.cache_write,
        },
        "contextWindow": model.context_window,
        "maxTokens": model.max_tokens,
    }
    if model.headers is not None:
        result["headers"] = model.headers
    return result


def deserialize_model(data: dict[str, Any]) -> Model:
    cost_data = data.get("cost", {})
    return Model(
        id=data.get("id", ""),
        name=data.get("name", ""),
        api=data.get("api", ""),
        provider=data.get("provider", ""),
        base_url=data.get("baseUrl", ""),
        reasoning=data.get("reasoning", False),
        input=data.get("input", ["text"]),
        cost=ModelCost(
            input=cost_data.get("input", 0.0),
            output=cost_data.get("output", 0.0),
            cache_read=cost_data.get("cacheRead", 0.0),
            cache_write=cost_data.get("cacheWrite", 0.0),
        ),
        context_window=data.get("contextWindow", 0),
        max_tokens=data.get("maxTokens", 0),
        headers=data.get("headers"),
    )


# --- Stream type aliases (used by providers) ---

AssistantMessageEventStream = AsyncGenerator[AssistantMessageEvent, None]

StreamFunction = Callable[..., AssistantMessageEventStream]
