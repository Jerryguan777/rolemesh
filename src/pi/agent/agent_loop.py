"""Agent loop — Python port of packages/agent/src/agent-loop.ts.

Uses async generators instead of EventStream.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from collections.abc import AsyncGenerator
from typing import Any

from pi.agent.types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentTool,
    AgentToolResult,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from pi.ai.stream import stream_simple as _default_stream_simple
from pi.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
)
from pi.ai.utils.validation import validate_tool_arguments


async def _call_async(fn: Any, *args: Any) -> Any:
    """Call a function that may return a coroutine or a plain value."""
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_get_messages(fn: Any) -> list[AgentMessage]:
    """Call an optional message-getter callback and return messages (or empty list)."""
    if fn is None:
        return []
    result = fn()
    if inspect.isawaitable(result):
        return list(await result)
    return list(result)


async def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: asyncio.Event | None = None,
    stream_fn: StreamFn | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """Start an agent loop with new prompt messages.

    Yields AgentEvents for each step of the conversation.
    """
    new_messages: list[AgentMessage] = list(prompts)
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=list(context.messages) + list(prompts),
        tools=context.tools,
    )

    from pi.agent.types import AgentStartEvent  # avoid circular at module level

    yield AgentStartEvent()
    yield TurnStartEvent()
    for prompt in prompts:
        yield MessageStartEvent(message=prompt)
        yield MessageEndEvent(message=prompt)

    async for event in _run_loop(current_context, new_messages, config, signal, stream_fn):
        yield event


async def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: asyncio.Event | None = None,
    stream_fn: StreamFn | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """Continue an agent loop from the current context without adding a new message.

    The last message in context must be a user or toolResult message.
    """
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")

    last_msg = context.messages[-1]
    if hasattr(last_msg, "role") and last_msg.role == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    new_messages: list[AgentMessage] = []
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=list(context.messages),
        tools=context.tools,
    )

    from pi.agent.types import AgentStartEvent  # avoid circular at module level

    yield AgentStartEvent()
    yield TurnStartEvent()

    async for event in _run_loop(current_context, new_messages, config, signal, stream_fn):
        yield event


async def _run_loop(
    current_context: AgentContext,
    new_messages: list[AgentMessage],
    config: AgentLoopConfig,
    signal: asyncio.Event | None,
    stream_fn: StreamFn | None,
) -> AsyncGenerator[AgentEvent, None]:
    """Main loop logic shared by agent_loop and agent_loop_continue."""
    first_turn = True
    turn_count = 0
    pending_messages: list[AgentMessage] = await _call_get_messages(config.get_steering_messages)

    while True:
        has_more_tool_calls = True
        steering_after_tools: list[AgentMessage] | None = None

        while has_more_tool_calls or len(pending_messages) > 0:
            if not first_turn:
                yield TurnStartEvent()
            else:
                first_turn = False

            if turn_count >= config.max_turns:
                # Abort when the loop exceeds the configured turn limit
                limit_msg = AssistantMessage(
                    api=config.model.api,
                    provider=config.model.provider,
                    model=config.model.id,
                    stop_reason="error",
                    error_message=f"Agent loop exceeded max_turns ({config.max_turns})",
                )
                new_messages.append(limit_msg)
                yield TurnEndEvent(message=limit_msg, tool_results=[])
                yield AgentEndEvent(messages=new_messages)
                return

            turn_count += 1

            # Inject pending steering messages before next assistant response
            if pending_messages:
                for message in pending_messages:
                    yield MessageStartEvent(message=message)
                    yield MessageEndEvent(message=message)
                    current_context.messages.append(message)
                    new_messages.append(message)
                pending_messages = []

            # Stream assistant response
            events, message = await _stream_assistant_response(current_context, config, signal, stream_fn)
            for event in events:
                yield event
            new_messages.append(message)

            if message.stop_reason in ("error", "aborted"):
                yield TurnEndEvent(message=message, tool_results=[])
                yield AgentEndEvent(messages=new_messages)
                return

            # Check for tool calls in the response
            tool_calls = [c for c in message.content if isinstance(c, ToolCall)]
            has_more_tool_calls = len(tool_calls) > 0

            tool_results: list[ToolResultMessage] = []
            if has_more_tool_calls:
                tool_events, tool_results, steering_after_tools = await _execute_tool_calls(
                    current_context.tools,
                    message,
                    signal,
                    config.get_steering_messages,
                )
                for event in tool_events:
                    yield event

                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            yield TurnEndEvent(message=message, tool_results=tool_results)

            # Get steering messages after turn completes
            if steering_after_tools:
                pending_messages = steering_after_tools
                steering_after_tools = None
            else:
                pending_messages = await _call_get_messages(config.get_steering_messages)

        # Check for follow-up messages after agent would stop
        follow_up_messages = await _call_get_messages(config.get_follow_up_messages)
        if follow_up_messages:
            pending_messages = follow_up_messages
            continue

        break

    yield AgentEndEvent(messages=new_messages)


async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: asyncio.Event | None,
    stream_fn: StreamFn | None,
) -> tuple[list[AgentEvent], AssistantMessage]:
    """Stream an assistant response from the LLM.

    Returns a list of events and the final AssistantMessage.
    """
    events: list[AgentEvent] = []

    # Apply context transform if configured
    messages = list(context.messages)
    if config.transform_context is not None:
        messages = await _call_async(config.transform_context, messages, signal)

    # Convert messages to LLM-compatible format
    if config.convert_to_llm is not None:
        llm_messages = await _call_async(config.convert_to_llm, messages)
    else:
        # Default: keep only standard LLM message roles
        llm_messages = [m for m in messages if hasattr(m, "role") and m.role in ("user", "assistant", "toolResult")]

    # Build LLM context with tools from agent context
    llm_context = Context(
        system_prompt=context.system_prompt,
        messages=llm_messages,
        tools=[Tool(name=t.name, description=t.description, parameters=t.parameters) for t in (context.tools or [])],
    )

    # Resolve API key (supports expiring tokens via get_api_key callback)
    resolved_api_key: str | None = None
    if config.get_api_key is not None:
        raw_key = await _call_async(config.get_api_key, config.model.provider)
        resolved_api_key = raw_key if isinstance(raw_key, str) else None
    if resolved_api_key is None:
        resolved_api_key = config.api_key

    # Build stream options with resolved API key and abort signal
    stream_opts = dataclasses.replace(config, api_key=resolved_api_key, signal=signal)

    actual_stream_fn = stream_fn if stream_fn is not None else _default_stream_simple

    response = actual_stream_fn(config.model, llm_context, stream_opts)

    partial_message: AssistantMessage | None = None
    added_partial = False

    async for event in response:
        if isinstance(event, DoneEvent):
            final_message = event.message
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                events.append(MessageStartEvent(message=final_message))
            events.append(MessageEndEvent(message=final_message))
            return events, final_message
        elif isinstance(event, ErrorEvent):
            final_message = event.error
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                events.append(MessageStartEvent(message=final_message))
            events.append(MessageEndEvent(message=final_message))
            return events, final_message
        elif isinstance(event, StartEvent):
            partial_message = event.partial
            context.messages.append(partial_message)
            added_partial = True
            events.append(MessageStartEvent(message=dataclasses.replace(partial_message)))
        else:
            # Streaming delta events — update partial and emit MessageUpdate
            if partial_message is not None:
                partial_message = event.partial
                context.messages[-1] = partial_message
                events.append(
                    MessageUpdateEvent(
                        message=dataclasses.replace(partial_message),
                        assistant_message_event=event,
                    )
                )

    # Fallback: return whatever partial we have if stream ended without done/error
    if partial_message is not None:
        return events, partial_message

    # Return an error message if nothing was streamed
    error_msg = AssistantMessage(
        api=config.model.api,
        provider=config.model.provider,
        model=config.model.id,
        stop_reason="error",
        error_message="Stream ended without a done event",
    )
    events.append(MessageStartEvent(message=error_msg))
    events.append(MessageEndEvent(message=error_msg))
    return events, error_msg


async def _execute_tool_calls(
    tools: list[AgentTool] | None,
    assistant_message: AssistantMessage,
    signal: asyncio.Event | None,
    get_steering_messages: Any,
) -> tuple[list[AgentEvent], list[ToolResultMessage], list[AgentMessage] | None]:
    """Execute tool calls from an assistant message sequentially.

    Returns events, tool results, and optional steering messages.
    """
    tool_calls = [c for c in assistant_message.content if isinstance(c, ToolCall)]
    events: list[AgentEvent] = []
    results: list[ToolResultMessage] = []
    steering_messages: list[AgentMessage] | None = None

    for index, tool_call in enumerate(tool_calls):
        tool = next((t for t in (tools or []) if t.name == tool_call.name), None)

        events.append(
            ToolExecutionStartEvent(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                args=tool_call.arguments,
            )
        )

        tool_result: AgentToolResult
        is_error = False

        try:
            if tool is None:
                raise ValueError(f"Tool {tool_call.name} not found")

            # Validate arguments against JSON Schema before executing
            pi_tool = Tool(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            validated_args = validate_tool_arguments(pi_tool, tool_call)

            partial_events: list[AgentEvent] = []
            tc_id = tool_call.id
            tc_name = tool_call.name
            tc_args = tool_call.arguments

            def on_update(
                partial: AgentToolResult,
                _tc_id: str = tc_id,
                _tc_name: str = tc_name,
                _tc_args: dict[str, Any] = tc_args,
                _events: list[AgentEvent] = partial_events,
            ) -> None:
                _events.append(
                    ToolExecutionUpdateEvent(
                        tool_call_id=_tc_id,
                        tool_name=_tc_name,
                        args=_tc_args,
                        partial_result=partial,
                    )
                )

            tool_result = await tool.execute(tc_id, validated_args, signal, on_update)
            events.extend(partial_events)
        except Exception as exc:
            tool_result = AgentToolResult(
                content=[TextContent(text=str(exc))],
                details={},
            )
            is_error = True

        events.append(
            ToolExecutionEndEvent(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                result=tool_result,
                is_error=is_error,
            )
        )

        tool_result_message = ToolResultMessage(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            content=tool_result.content,
            details=tool_result.details,
            is_error=is_error,
            timestamp=time.time() * 1000,
        )
        results.append(tool_result_message)
        events.append(MessageStartEvent(message=tool_result_message))
        events.append(MessageEndEvent(message=tool_result_message))

        # Check for steering messages — skip remaining tools if interrupted
        if get_steering_messages is not None:
            steering = await _call_get_messages(get_steering_messages)
            if steering:
                steering_messages = steering
                # Skip any remaining tool calls
                for skipped_call in tool_calls[index + 1 :]:
                    skip_result, skip_events = _skip_tool_call(skipped_call)
                    events.extend(skip_events)
                    results.append(skip_result)
                break

    return events, results, steering_messages


def _skip_tool_call(tool_call: ToolCall) -> tuple[ToolResultMessage, list[AgentEvent]]:
    """Create a skipped tool result for a tool call that was not executed."""
    skip_result = AgentToolResult(
        content=[TextContent(text="Skipped due to queued user message.")],
        details={},
    )
    events: list[AgentEvent] = [
        ToolExecutionStartEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            args=tool_call.arguments,
        ),
        ToolExecutionEndEvent(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            result=skip_result,
            is_error=True,
        ),
    ]
    tool_result_message = ToolResultMessage(
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        content=skip_result.content,
        details={},
        is_error=True,
        timestamp=time.time() * 1000,
    )
    events.append(MessageStartEvent(message=tool_result_message))
    events.append(MessageEndEvent(message=tool_result_message))
    return tool_result_message, events
