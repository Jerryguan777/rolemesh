"""RPC mode — Python port of packages/coding-agent/src/modes/rpc/rpc-mode.ts.

``run_rpc_mode`` reads JSON-line commands from stdin, dispatches them to the
agent session, and writes JSON responses/events to stdout.

Because AgentSession does not yet exist in the Python codebase, its interface
is described by the ``AgentSessionProtocol`` Protocol so this module can be
type-checked without the concrete implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn, Protocol, runtime_checkable

from pi.coding_agent.modes.rpc.rpc_types import (
    RpcAbortBashCommand,
    RpcAbortCommand,
    RpcAbortRetryCommand,
    RpcBashCommand,
    RpcCommand,
    RpcCompactCommand,
    RpcCycleModelCommand,
    RpcCycleThinkingLevelCommand,
    RpcExportHtmlCommand,
    RpcForkCommand,
    RpcGetAvailableModelsCommand,
    RpcGetCommandsCommand,
    RpcGetForkMessagesCommand,
    RpcGetLastAssistantTextCommand,
    RpcGetMessagesCommand,
    RpcGetSessionStatsCommand,
    RpcGetStateCommand,
    RpcNewSessionCommand,
    RpcPromptCommand,
    RpcResponse,
    RpcSetAutoCompactionCommand,
    RpcSetAutoRetryCommand,
    RpcSetFollowUpModeCommand,
    RpcSetModelCommand,
    RpcSetSessionNameCommand,
    RpcSetSteeringModeCommand,
    RpcSetThinkingLevelCommand,
    RpcSwitchSessionCommand,
    deserialize_rpc_command,
    serialize_rpc_response,
    serialize_rpc_session_state,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class AgentSessionProtocol(Protocol):
    """Minimal interface that run_rpc_mode requires from an AgentSession."""

    async def prompt(self, message: str, images: list[Any], streaming_behavior: str | None) -> Any: ...
    async def steer(self, message: str, images: list[Any]) -> Any: ...
    async def follow_up(self, message: str, images: list[Any]) -> Any: ...
    async def abort(self) -> None: ...
    async def new_session(self, parent_session: str | None) -> Any: ...
    async def get_state(self) -> Any: ...
    async def set_model(self, provider: str, model_id: str) -> Any: ...
    async def cycle_model(self) -> Any: ...
    async def get_available_models(self) -> Any: ...
    async def set_thinking_level(self, level: str) -> Any: ...
    async def cycle_thinking_level(self) -> Any: ...
    async def set_steering_mode(self, mode: str) -> Any: ...
    async def set_follow_up_mode(self, mode: str) -> Any: ...
    async def compact(self, custom_instructions: str | None) -> Any: ...
    async def set_auto_compaction(self, enabled: bool) -> Any: ...
    async def set_auto_retry(self, enabled: bool) -> Any: ...
    async def abort_retry(self) -> None: ...
    async def bash(self, command: str) -> Any: ...
    async def abort_bash(self) -> None: ...
    async def get_session_stats(self) -> Any: ...
    async def export_html(self, output_path: str | None) -> Any: ...
    async def switch_session(self, session_path: str) -> Any: ...
    async def fork(self, entry_id: str) -> Any: ...
    async def get_fork_messages(self) -> Any: ...
    async def get_last_assistant_text(self) -> Any: ...
    async def set_session_name(self, name: str) -> Any: ...
    async def get_messages(self) -> Any: ...
    async def get_commands(self) -> Any: ...


# Handler type: coroutine that takes the session and returns response data
_Handler = Callable[[AgentSessionProtocol], Awaitable[Any]]


def _write_response(response: RpcResponse) -> None:
    """Serialise and flush a response to stdout."""
    line = json.dumps(serialize_rpc_response(response))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _ok(cmd_type: str, data: Any = None, cmd_id: str | None = None) -> RpcResponse:
    return RpcResponse(command=cmd_type, success=True, data=data, id=cmd_id)


def _err(cmd_type: str, message: str, cmd_id: str | None = None) -> RpcResponse:
    return RpcResponse(command=cmd_type, success=False, error=message, id=cmd_id)


async def _dispatch(session: AgentSessionProtocol, cmd: RpcCommand) -> RpcResponse:
    """Dispatch a parsed command to the session and build an RpcResponse."""
    try:
        if isinstance(cmd, RpcPromptCommand):
            data = await session.prompt(cmd.message, cmd.images, cmd.streaming_behavior)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcAbortCommand):
            await session.abort()
            return _ok(cmd.type, cmd_id=cmd.id)
        if isinstance(cmd, RpcNewSessionCommand):
            data = await session.new_session(cmd.parent_session)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcGetStateCommand):
            state = await session.get_state()
            return _ok(cmd.type, serialize_rpc_session_state(state) if state is not None else None, cmd.id)
        if isinstance(cmd, RpcSetModelCommand):
            data = await session.set_model(cmd.provider, cmd.model_id)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcGetAvailableModelsCommand):
            data = await session.get_available_models()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSetThinkingLevelCommand):
            data = await session.set_thinking_level(cmd.level)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSetSteeringModeCommand):
            data = await session.set_steering_mode(cmd.mode)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSetFollowUpModeCommand):
            data = await session.set_follow_up_mode(cmd.mode)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcCompactCommand):
            data = await session.compact(cmd.custom_instructions)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSetAutoCompactionCommand):
            data = await session.set_auto_compaction(cmd.enabled)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSetAutoRetryCommand):
            data = await session.set_auto_retry(cmd.enabled)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcAbortRetryCommand):
            await session.abort_retry()
            return _ok(cmd.type, cmd_id=cmd.id)
        if isinstance(cmd, RpcBashCommand):
            data = await session.bash(cmd.command)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcAbortBashCommand):
            await session.abort_bash()
            return _ok(cmd.type, cmd_id=cmd.id)
        if isinstance(cmd, RpcGetSessionStatsCommand):
            data = await session.get_session_stats()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcExportHtmlCommand):
            data = await session.export_html(cmd.output_path)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSwitchSessionCommand):
            data = await session.switch_session(cmd.session_path)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcForkCommand):
            data = await session.fork(cmd.entry_id)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcGetForkMessagesCommand):
            data = await session.get_fork_messages()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcGetLastAssistantTextCommand):
            data = await session.get_last_assistant_text()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcSetSessionNameCommand):
            data = await session.set_session_name(cmd.name)
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcGetMessagesCommand):
            data = await session.get_messages()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcGetCommandsCommand):
            data = await session.get_commands()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcCycleModelCommand):
            data = await session.cycle_model()
            return _ok(cmd.type, data, cmd.id)
        if isinstance(cmd, RpcCycleThinkingLevelCommand):
            data = await session.cycle_thinking_level()
            return _ok(cmd.type, data, cmd.id)
        return _err("unknown", f"Unhandled command type: {cmd.type}", cmd.id)
    except Exception as exc:
        logger.exception("RPC dispatch error for command %s", cmd.type)
        return _err(cmd.type, str(exc), cmd.id)


async def _run_loop(session: AgentSessionProtocol) -> NoReturn:
    """Read stdin line by line, dispatch commands, write responses."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    while True:
        try:
            raw = await reader.readline()
        except Exception:
            break
        if not raw:
            # EOF
            break
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            response = RpcResponse(command="unknown", success=False, error=f"Invalid JSON: {exc}")
            _write_response(response)
            continue
        try:
            cmd = deserialize_rpc_command(data)
        except ValueError as exc:
            response = RpcResponse(command=data.get("type", "unknown"), success=False, error=str(exc))
            _write_response(response)
            continue
        response = await _dispatch(session, cmd)
        _write_response(response)

    # Exit cleanly when stdin closes
    sys.exit(0)


def run_rpc_mode(session: AgentSessionProtocol) -> NoReturn:
    """Entry point: run the RPC loop synchronously. Does not return."""
    asyncio.run(_run_loop(session))


__all__ = [
    "AgentSessionProtocol",
    "run_rpc_mode",
]
