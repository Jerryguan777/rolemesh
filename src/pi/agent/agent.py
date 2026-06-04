"""Agent class — Python port of packages/agent/src/agent.ts."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from typing import Any

from pi.agent.agent_loop import agent_loop, agent_loop_continue
from pi.agent.types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentState,
    AgentTool,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    StreamFn,
    ThinkingLevel,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
)
from pi.ai.models import get_model
from pi.ai.stream import stream_simple
from pi.ai.types import (
    AssistantMessage,
    ImageContent,
    Message,
    Model,
    TextContent,
    ThinkingBudgets,
    Transport,
    UserMessage,
)


def _default_convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """Keep only LLM-compatible messages (user, assistant, toolResult)."""
    return [m for m in messages if hasattr(m, "role") and m.role in ("user", "assistant", "toolResult")]


def _make_default_model() -> Model:
    """Return default model, falling back to empty Model if not registered."""
    try:
        result = get_model("google", "gemini-2.5-flash-lite-preview-06-17")
        return result if result is not None else Model()
    except Exception:
        return Model()


@dataclasses.dataclass
class AgentOptions:
    """Options for constructing an Agent."""

    initial_state: AgentState | None = None
    convert_to_llm: Callable[[list[AgentMessage]], list[Message]] | None = None
    transform_context: Callable[[list[AgentMessage], asyncio.Event | None], Any] | None = None
    steering_mode: str = "one-at-a-time"  # "all" | "one-at-a-time"
    follow_up_mode: str = "one-at-a-time"  # "all" | "one-at-a-time"
    stream_fn: StreamFn | None = None
    session_id: str | None = None
    get_api_key: Callable[[str], Any] | None = None
    thinking_budgets: ThinkingBudgets | None = None
    transport: Transport = "sse"
    max_retry_delay_ms: int | None = None
    max_turns: int = 50


class Agent:
    """Agent that uses the agent loop to process prompts with tool calling."""

    def __init__(self, opts: AgentOptions | None = None) -> None:
        if opts is None:
            opts = AgentOptions()

        default_state = AgentState(
            system_prompt="",
            model=_make_default_model(),
            thinking_level="off",
            tools=[],
            messages=[],
            is_streaming=False,
            stream_message=None,
            pending_tool_calls=set(),
            error=None,
        )

        if opts.initial_state is not None:
            self._state = AgentState(
                system_prompt=opts.initial_state.system_prompt,
                model=opts.initial_state.model,
                thinking_level=opts.initial_state.thinking_level,
                tools=opts.initial_state.tools,
                messages=list(opts.initial_state.messages),
                is_streaming=opts.initial_state.is_streaming,
                stream_message=opts.initial_state.stream_message,
                pending_tool_calls=set(opts.initial_state.pending_tool_calls),
                error=opts.initial_state.error,
            )
        else:
            self._state = default_state

        self._listeners: set[Callable[[AgentEvent], None]] = set()
        self._abort_event: asyncio.Event | None = None
        self._convert_to_llm: Callable[[list[AgentMessage]], list[Message]] = (
            opts.convert_to_llm or _default_convert_to_llm
        )
        self._transform_context = opts.transform_context
        self._steering_mode = opts.steering_mode
        self._follow_up_mode = opts.follow_up_mode
        self.stream_fn: StreamFn = opts.stream_fn or stream_simple
        self._session_id = opts.session_id
        self.get_api_key = opts.get_api_key
        self._thinking_budgets = opts.thinking_budgets
        self._transport: Transport = opts.transport
        self._max_retry_delay_ms = opts.max_retry_delay_ms
        self._max_turns = opts.max_turns
        self._steering_queue: list[AgentMessage] = []
        self._follow_up_queue: list[AgentMessage] = []
        self._running_future: asyncio.Future[None] | None = None

    # --- Properties ---

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self._session_id = value

    @property
    def thinking_budgets(self) -> ThinkingBudgets | None:
        return self._thinking_budgets

    @thinking_budgets.setter
    def thinking_budgets(self, value: ThinkingBudgets | None) -> None:
        self._thinking_budgets = value

    @property
    def transport(self) -> Transport:
        return self._transport

    def set_transport(self, value: Transport) -> None:
        self._transport = value

    @property
    def max_retry_delay_ms(self) -> int | None:
        return self._max_retry_delay_ms

    @max_retry_delay_ms.setter
    def max_retry_delay_ms(self, value: int | None) -> None:
        self._max_retry_delay_ms = value

    @property
    def state(self) -> AgentState:
        return self._state

    # --- Subscription ---

    def subscribe(self, fn: Callable[[AgentEvent], None]) -> Callable[[], None]:
        """Subscribe to agent events. Returns an unsubscribe function."""
        self._listeners.add(fn)
        return lambda: self._listeners.discard(fn)

    # --- State mutators ---

    def set_system_prompt(self, v: str) -> None:
        self._state.system_prompt = v

    def set_model(self, m: Model) -> None:
        self._state.model = m

    def set_thinking_level(self, level: ThinkingLevel) -> None:
        self._state.thinking_level = level

    def set_steering_mode(self, mode: str) -> None:
        self._steering_mode = mode

    def get_steering_mode(self) -> str:
        return self._steering_mode

    def set_follow_up_mode(self, mode: str) -> None:
        self._follow_up_mode = mode

    def get_follow_up_mode(self) -> str:
        return self._follow_up_mode

    def set_tools(self, t: list[AgentTool]) -> None:
        self._state.tools = t

    def replace_messages(self, ms: list[AgentMessage]) -> None:
        self._state.messages = list(ms)

    def append_message(self, m: AgentMessage) -> None:
        self._state.messages = [*self._state.messages, m]

    def steer(self, m: AgentMessage) -> None:
        """Queue a steering message to interrupt the agent mid-run."""
        self._steering_queue.append(m)

    def follow_up(self, m: AgentMessage) -> None:
        """Queue a follow-up message to process after the agent finishes."""
        self._follow_up_queue.append(m)

    def clear_steering_queue(self) -> None:
        self._steering_queue = []

    def clear_follow_up_queue(self) -> None:
        self._follow_up_queue = []

    def clear_all_queues(self) -> None:
        self._steering_queue = []
        self._follow_up_queue = []

    def has_queued_messages(self) -> bool:
        return bool(self._steering_queue) or bool(self._follow_up_queue)

    def clear_messages(self) -> None:
        self._state.messages = []

    def abort(self) -> None:
        """Abort the current agent loop."""
        if self._abort_event is not None:
            self._abort_event.set()

    async def wait_for_idle(self) -> None:
        """Wait for the agent to finish processing."""
        if self._running_future is not None:
            await self._running_future

    def reset(self) -> None:
        """Reset conversation state."""
        self._state.messages = []
        self._state.is_streaming = False
        self._state.stream_message = None
        self._state.pending_tool_calls = set()
        self._state.error = None
        self._steering_queue = []
        self._follow_up_queue = []

    def _dequeue_steering_messages(self) -> list[AgentMessage]:
        if self._steering_mode == "one-at-a-time":
            if self._steering_queue:
                first = self._steering_queue[0]
                self._steering_queue = self._steering_queue[1:]
                return [first]
            return []
        result = list(self._steering_queue)
        self._steering_queue = []
        return result

    def _dequeue_follow_up_messages(self) -> list[AgentMessage]:
        if self._follow_up_mode == "one-at-a-time":
            if self._follow_up_queue:
                first = self._follow_up_queue[0]
                self._follow_up_queue = self._follow_up_queue[1:]
                return [first]
            return []
        result = list(self._follow_up_queue)
        self._follow_up_queue = []
        return result

    async def prompt(
        self,
        input: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ) -> None:
        """Send a prompt to the agent."""
        if self._state.is_streaming:
            raise RuntimeError(
                "Agent is already processing a prompt. "
                "Use steer() or follow_up() to queue messages, or wait for completion."
            )

        msgs: list[AgentMessage]
        if isinstance(input, list):
            msgs = input
        elif isinstance(input, str):
            content: list[TextContent | ImageContent] = [TextContent(text=input)]
            if images:
                content.extend(images)
            msgs = [UserMessage(content=content, timestamp=time.time() * 1000)]
        else:
            msgs = [input]

        await self._run_loop(msgs)

    async def continue_(self) -> None:
        """Continue from current context (for retries and resuming queued messages)."""
        if self._state.is_streaming:
            raise RuntimeError("Agent is already processing. Wait for completion before continuing.")

        messages = self._state.messages
        if not messages:
            raise RuntimeError("No messages to continue from")

        last = messages[-1]
        if hasattr(last, "role") and last.role == "assistant":
            queued_steering = self._dequeue_steering_messages()
            if queued_steering:
                await self._run_loop(queued_steering, skip_initial_steering_poll=True)
                return

            queued_follow_up = self._dequeue_follow_up_messages()
            if queued_follow_up:
                await self._run_loop(queued_follow_up)
                return

            raise RuntimeError("Cannot continue from message role: assistant")

        await self._run_loop(None)

    async def _run_loop(
        self,
        messages: list[AgentMessage] | None,
        skip_initial_steering_poll: bool = False,
    ) -> None:
        """Internal: run the agent loop and dispatch events to subscribers."""
        model = self._state.model
        if not model:
            raise RuntimeError("No model configured")

        running_loop = asyncio.get_running_loop()
        self._running_future = running_loop.create_future()
        self._abort_event = asyncio.Event()
        self._state.is_streaming = True
        self._state.stream_message = None
        self._state.error = None

        # Map thinking_level "off" to None for the LLM reasoning parameter
        reasoning = None if self._state.thinking_level == "off" else self._state.thinking_level

        context = AgentContext(
            system_prompt=self._state.system_prompt,
            messages=list(self._state.messages),
            tools=self._state.tools,
        )

        skip_poll = skip_initial_steering_poll

        def get_steering() -> list[AgentMessage]:
            nonlocal skip_poll
            if skip_poll:
                skip_poll = False
                return []
            return self._dequeue_steering_messages()

        config = AgentLoopConfig(
            model=model,
            reasoning=reasoning,
            session_id=self._session_id,
            transport=self._transport,
            thinking_budgets=self._thinking_budgets,
            max_retry_delay_ms=self._max_retry_delay_ms,
            max_turns=self._max_turns,
            convert_to_llm=self._convert_to_llm,
            transform_context=self._transform_context,
            get_api_key=self.get_api_key,
            get_steering_messages=get_steering,
            get_follow_up_messages=self._dequeue_follow_up_messages,
        )

        abort_signal = self._abort_event
        partial: AgentMessage | None = None

        try:
            gen = (
                agent_loop(messages, context, config, abort_signal, self.stream_fn)
                if messages is not None
                else agent_loop_continue(context, config, abort_signal, self.stream_fn)
            )

            async for event in gen:
                partial = self._handle_loop_event(event, partial)
                self._emit(event)

            # Handle any remaining partial message (e.g. aborted mid-stream)
            if partial is not None and isinstance(partial, AssistantMessage) and partial.content:
                has_content = any(
                    (isinstance(c, TextContent) and c.text.strip()) or (isinstance(c, ImageContent))
                    for c in partial.content
                )
                if has_content:
                    self.append_message(partial)
                elif abort_signal.is_set():
                    raise RuntimeError("Request was aborted")

        except Exception as err:
            error_msg_text = str(err)
            error_msg = AssistantMessage(
                api=model.api,
                provider=model.provider,
                model=model.id,
                stop_reason="aborted" if abort_signal.is_set() else "error",
                error_message=error_msg_text,
                timestamp=time.time() * 1000,
            )
            self.append_message(error_msg)
            self._state.error = error_msg_text
            self._emit(AgentEndEvent(messages=[error_msg]))
        finally:
            self._state.is_streaming = False
            self._state.stream_message = None
            self._state.pending_tool_calls = set()
            self._abort_event = None
            if self._running_future is not None and not self._running_future.done():
                self._running_future.set_result(None)
            self._running_future = None

    def _handle_loop_event(self, event: AgentEvent, partial: AgentMessage | None) -> AgentMessage | None:
        """Update agent state for one loop event. Returns updated partial message."""
        if isinstance(event, (MessageStartEvent, MessageUpdateEvent)):
            partial = event.message
            self._state.stream_message = event.message
        elif isinstance(event, MessageEndEvent):
            partial = None
            self._state.stream_message = None
            self.append_message(event.message)
        elif isinstance(event, ToolExecutionStartEvent):
            s = set(self._state.pending_tool_calls)
            s.add(event.tool_call_id)
            self._state.pending_tool_calls = s
        elif isinstance(event, ToolExecutionEndEvent):
            s = set(self._state.pending_tool_calls)
            s.discard(event.tool_call_id)
            self._state.pending_tool_calls = s
        elif isinstance(event, TurnEndEvent):
            if isinstance(event.message, AssistantMessage) and event.message.error_message:
                self._state.error = event.message.error_message
        elif isinstance(event, AgentEndEvent):
            self._state.is_streaming = False
            self._state.stream_message = None
        return partial

    def _emit(self, e: AgentEvent) -> None:
        """Dispatch an event to all registered listeners."""
        for listener in list(self._listeners):
            try:
                listener(e)
            except Exception:
                logging.getLogger(__name__).exception("Subscriber raised exception")
