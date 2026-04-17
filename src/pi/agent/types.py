"""Agent types — Python port of packages/agent/src/types.ts."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ImageContent,
    Message,
    Model,
    SimpleStreamOptions,
    StartEvent,
    TextContent,
    ToolResultMessage,
    Usage,
)

# ThinkingLevel for agents: extends pi.ai.ThinkingLevel with "off" (no reasoning)
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]


# CustomAgentMessages: empty by default — apps extend by subclassing to add
# custom message types (mirrors TS declaration-merging pattern).
@dataclass
class CustomAgentMessages:
    pass


# AgentMessage: union of standard LLM messages; apps can widen this type alias
AgentMessage = Message


@dataclass
class AgentToolResult:
    """Result from a tool execution, with content blocks and opaque details."""

    content: list[TextContent | ImageContent]
    details: Any


# Callback for streaming tool execution updates
AgentToolUpdateCallback = Callable[["AgentToolResult"], None]

# StreamFn: same signature as stream_simple from pi.ai
StreamFn = Callable[
    [Model, Context, SimpleStreamOptions | None],
    AsyncIterator[AssistantMessageEvent],
]


class AgentTool(ABC):
    """Abstract base class for agent tools. Concrete tools inherit and implement execute()."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name used to match LLM tool calls."""
        ...

    @property
    @abstractmethod
    def label(self) -> str:
        """Human-readable label for display in UI."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description passed to the LLM in the tool schema."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema object describing the tool's input parameters."""
        ...

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Execute the tool and return a result."""
        ...


@dataclass
class AgentState:
    """Agent state containing all configuration and conversation data."""

    system_prompt: str
    model: Model
    thinking_level: ThinkingLevel
    tools: list[AgentTool]
    messages: list[AgentMessage]
    is_streaming: bool
    stream_message: AgentMessage | None
    pending_tool_calls: set[str]
    error: str | None = None


@dataclass
class AgentContext:
    """Context passed to the agent loop, using AgentTool instead of Tool."""

    system_prompt: str
    messages: list[AgentMessage]
    tools: list[AgentTool] | None = None


@dataclass
class AgentLoopConfig(SimpleStreamOptions):
    """Configuration for the agent loop. Extends SimpleStreamOptions with agent callbacks."""

    model: Model = field(default_factory=Model)
    # Maximum number of LLM turns (prompt + tool-call rounds) before the loop aborts.
    # Prevents runaway loops when a misbehaving LLM keeps requesting tool calls indefinitely.
    max_turns: int = 50
    # max_retry_delay_ms: mirrors TS AgentOptions.maxRetryDelayMs — reserved for a future
    # retry-on-transient-error implementation; currently carried through but not acted upon.
    convert_to_llm: Callable[[list[AgentMessage]], list[Message]] | None = None
    transform_context: Callable[[list[AgentMessage], asyncio.Event | None], Any] | None = None
    get_api_key: Callable[[str], Any] | None = None
    get_steering_messages: Callable[[], Any] | None = None
    get_follow_up_messages: Callable[[], Any] | None = None


# --- AgentEvent discriminated union ---


@dataclass
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass
class AgentEndEvent:
    messages: list[AgentMessage] = field(default_factory=list)
    type: Literal["agent_end"] = "agent_end"


@dataclass
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass
class TurnEndEvent:
    message: AgentMessage = field(default_factory=AssistantMessage)
    tool_results: list[ToolResultMessage] = field(default_factory=list)
    type: Literal["turn_end"] = "turn_end"


@dataclass
class PromptTurnCompleteEvent:
    """Fired once per user-visible prompt (initial + each follow-up) when the
    agent has finished answering it — i.e. when the tool-call-driven inner loop
    exits and no steering messages are queued.

    Distinguishes a per-prompt completion from a per-turn completion
    (TurnEndEvent fires after every LLM call, including tool-calling turns).
    Consumers that bridge prompt batches to an outer protocol (for example, a
    NATS bridge delivering one visible reply per user message) should key off
    this event rather than TurnEndEvent.
    """

    message: AgentMessage = field(default_factory=AssistantMessage)
    type: Literal["prompt_turn_complete"] = "prompt_turn_complete"


@dataclass
class MessageStartEvent:
    message: AgentMessage = field(default_factory=AssistantMessage)
    type: Literal["message_start"] = "message_start"


@dataclass
class MessageUpdateEvent:
    message: AgentMessage = field(default_factory=AssistantMessage)
    assistant_message_event: AssistantMessageEvent = field(default_factory=StartEvent)
    type: Literal["message_update"] = "message_update"


@dataclass
class MessageEndEvent:
    message: AgentMessage = field(default_factory=AssistantMessage)
    type: Literal["message_end"] = "message_end"


@dataclass
class ToolExecutionStartEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    type: Literal["tool_execution_start"] = "tool_execution_start"


@dataclass
class ToolExecutionUpdateEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    partial_result: AgentToolResult = field(default_factory=lambda: AgentToolResult(content=[], details=None))
    type: Literal["tool_execution_update"] = "tool_execution_update"


@dataclass
class ToolExecutionEndEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    result: AgentToolResult = field(default_factory=lambda: AgentToolResult(content=[], details=None))
    is_error: bool = False
    type: Literal["tool_execution_end"] = "tool_execution_end"


# Union of all agent event types
AgentEvent = (
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | PromptTurnCompleteEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
)

# --- Proxy event types ---


@dataclass
class ProxyStartEvent:
    type: Literal["start"] = "start"


@dataclass
class ProxyTextStartEvent:
    content_index: int = 0
    type: Literal["text_start"] = "text_start"


@dataclass
class ProxyTextDeltaEvent:
    content_index: int = 0
    delta: str = ""
    type: Literal["text_delta"] = "text_delta"


@dataclass
class ProxyTextEndEvent:
    content_index: int = 0
    content_signature: str | None = None
    type: Literal["text_end"] = "text_end"


@dataclass
class ProxyThinkingStartEvent:
    content_index: int = 0
    type: Literal["thinking_start"] = "thinking_start"


@dataclass
class ProxyThinkingDeltaEvent:
    content_index: int = 0
    delta: str = ""
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass
class ProxyThinkingEndEvent:
    content_index: int = 0
    content_signature: str | None = None
    type: Literal["thinking_end"] = "thinking_end"


@dataclass
class ProxyToolCallStartEvent:
    content_index: int = 0
    id: str = ""
    tool_name: str = ""
    type: Literal["toolcall_start"] = "toolcall_start"


@dataclass
class ProxyToolCallDeltaEvent:
    content_index: int = 0
    delta: str = ""
    type: Literal["toolcall_delta"] = "toolcall_delta"


@dataclass
class ProxyToolCallEndEvent:
    content_index: int = 0
    type: Literal["toolcall_end"] = "toolcall_end"


@dataclass
class ProxyDoneEvent:
    reason: Literal["stop", "length", "toolUse"] = "stop"
    usage: Usage = field(default_factory=Usage)
    type: Literal["done"] = "done"


@dataclass
class ProxyErrorEvent:
    reason: Literal["aborted", "error"] = "error"
    error_message: str | None = None
    usage: Usage = field(default_factory=Usage)
    type: Literal["error"] = "error"


# Union of all proxy event types
ProxyAssistantMessageEvent = (
    ProxyStartEvent
    | ProxyTextStartEvent
    | ProxyTextDeltaEvent
    | ProxyTextEndEvent
    | ProxyThinkingStartEvent
    | ProxyThinkingDeltaEvent
    | ProxyThinkingEndEvent
    | ProxyToolCallStartEvent
    | ProxyToolCallDeltaEvent
    | ProxyToolCallEndEvent
    | ProxyDoneEvent
    | ProxyErrorEvent
)


@dataclass
class ProxyStreamOptions(SimpleStreamOptions):
    """Options for streaming through a proxy server."""

    auth_token: str = ""
    proxy_url: str = ""
