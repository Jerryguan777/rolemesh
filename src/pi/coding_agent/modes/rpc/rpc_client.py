"""RPC client — Python port of packages/coding-agent/src/modes/rpc/rpc-client.ts.

Spawns a subprocess in RPC mode and communicates via stdin/stdout JSON lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pi.agent.types import ThinkingLevel
from pi.ai.types import ImageContent
from pi.coding_agent.modes.rpc.rpc_types import (
    RpcResponse,
    deserialize_rpc_response,
    serialize_rpc_session_state,
)

logger = logging.getLogger(__name__)

# Type for event listener callbacks; receives raw event dict
EventListener = Callable[[dict[str, Any]], None]
RpcEventListener = EventListener
# Unsubscribe function returned by on_event()
Unsubscribe = Callable[[], None]


@dataclass
class RpcClientOptions:
    """Options for creating an RPC client."""

    cli_path: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    args: list[str] | None = None


@dataclass
class ModelInfo:
    """Model information returned by RPC."""

    provider: str = ""
    id: str = ""
    context_window: int = 0
    reasoning: bool = False


def _encode_image(img: ImageContent) -> dict[str, Any]:
    return {"type": "image", "data": img.data, "mimeType": img.mime_type}


class RpcClient:
    """Client that communicates with a pi coding-agent subprocess via JSON-line RPC."""

    def __init__(self, command: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._command = command
        self._cwd = cwd
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._listeners: list[EventListener] = []
        self._pending: dict[str, asyncio.Future[RpcResponse]] = {}
        self._stderr_lines: list[str] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the subprocess and begin reading its stdout."""
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def stop(self) -> None:
        """Terminate the subprocess and cancel background tasks."""
        if self._process is not None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        # Resolve any pending futures with an error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("RpcClient stopped before response received"))
        self._pending.clear()

    def on_event(self, listener: EventListener) -> Unsubscribe:
        """Register an event listener. Returns an unsubscribe callable."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def get_stderr(self) -> str:
        """Return all stderr output collected so far."""
        return "\n".join(self._stderr_lines)

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    data: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("RpcClient: failed to parse JSON line: %s", line)
                    continue
                msg_type = data.get("type")
                if msg_type == "response":
                    response = deserialize_rpc_response(data)
                    cmd_id = response.id
                    if cmd_id is not None and cmd_id in self._pending:
                        future = self._pending.pop(cmd_id)
                        if not future.done():
                            future.set_result(response)
                    else:
                        self._emit_event(data)
                else:
                    self._emit_event(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RpcClient: unexpected error reading stdout")

    async def _read_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        try:
            async for raw_line in self._process.stderr:
                line = raw_line.decode(errors="replace").rstrip()
                self._stderr_lines.append(line)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RpcClient: unexpected error reading stderr")

    def _emit_event(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("RpcClient: event listener raised an exception")

    async def _send(self, cmd: dict[str, Any], timeout: float = 30.0) -> RpcResponse:
        """Write a command to stdin and wait for the corresponding response.

        Raises ``TimeoutError`` if no response is received within ``timeout`` seconds.
        """
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("RpcClient is not started")
        cmd_id = cmd.get("id")
        if cmd_id is None:
            cmd_id = str(uuid.uuid4())
            cmd = {**cmd, "id": cmd_id}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RpcResponse] = loop.create_future()
        self._pending[cmd_id] = future
        payload = json.dumps(cmd) + "\n"
        self._process.stdin.write(payload.encode())
        await self._process.stdin.drain()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            self._pending.pop(cmd_id, None)
            if not future.done():
                future.cancel()
            raise TimeoutError(f"Timeout waiting for response to {cmd.get('type')!r}") from None

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------

    def _images_payload(self, images: list[ImageContent] | None) -> list[dict[str, Any]]:
        if not images:
            return []
        return [_encode_image(img) for img in images]

    async def prompt(
        self,
        message: str,
        images: list[ImageContent] | None = None,
        streaming_behavior: str | None = None,
    ) -> RpcResponse:
        cmd: dict[str, Any] = {"type": "prompt", "message": message}
        imgs = self._images_payload(images)
        if imgs:
            cmd["images"] = imgs
        if streaming_behavior is not None:
            cmd["streamingBehavior"] = streaming_behavior
        return await self._send(cmd)

    async def steer(self, message: str, images: list[ImageContent] | None = None) -> RpcResponse:
        cmd: dict[str, Any] = {"type": "steer", "message": message}
        imgs = self._images_payload(images)
        if imgs:
            cmd["images"] = imgs
        return await self._send(cmd)

    async def follow_up(self, message: str, images: list[ImageContent] | None = None) -> RpcResponse:
        cmd: dict[str, Any] = {"type": "follow_up", "message": message}
        imgs = self._images_payload(images)
        if imgs:
            cmd["images"] = imgs
        return await self._send(cmd)

    async def abort(self) -> RpcResponse:
        return await self._send({"type": "abort"})

    async def new_session(self, parent_session: str | None = None) -> RpcResponse:
        cmd: dict[str, Any] = {"type": "new_session"}
        if parent_session is not None:
            cmd["parentSession"] = parent_session
        return await self._send(cmd)

    async def get_state(self) -> RpcResponse:
        return await self._send({"type": "get_state"})

    async def set_model(self, provider: str, model_id: str) -> RpcResponse:
        return await self._send({"type": "set_model", "provider": provider, "modelId": model_id})

    async def cycle_model(self) -> RpcResponse:
        return await self._send({"type": "cycle_model"})

    async def get_available_models(self) -> RpcResponse:
        return await self._send({"type": "get_available_models"})

    async def set_thinking_level(self, level: ThinkingLevel) -> RpcResponse:
        return await self._send({"type": "set_thinking_level", "level": level})

    async def cycle_thinking_level(self) -> RpcResponse:
        return await self._send({"type": "cycle_thinking_level"})

    async def set_steering_mode(self, mode: str) -> RpcResponse:
        return await self._send({"type": "set_steering_mode", "mode": mode})

    async def set_follow_up_mode(self, mode: str) -> RpcResponse:
        return await self._send({"type": "set_follow_up_mode", "mode": mode})

    async def compact(self, custom_instructions: str | None = None) -> RpcResponse:
        cmd: dict[str, Any] = {"type": "compact"}
        if custom_instructions is not None:
            cmd["customInstructions"] = custom_instructions
        return await self._send(cmd)

    async def set_auto_compaction(self, enabled: bool) -> RpcResponse:
        return await self._send({"type": "set_auto_compaction", "enabled": enabled})

    async def set_auto_retry(self, enabled: bool) -> RpcResponse:
        return await self._send({"type": "set_auto_retry", "enabled": enabled})

    async def abort_retry(self) -> RpcResponse:
        return await self._send({"type": "abort_retry"})

    async def bash(self, command: str) -> RpcResponse:
        return await self._send({"type": "bash", "command": command})

    async def abort_bash(self) -> RpcResponse:
        return await self._send({"type": "abort_bash"})

    async def get_session_stats(self) -> RpcResponse:
        return await self._send({"type": "get_session_stats"})

    async def export_html(self, output_path: str | None = None) -> RpcResponse:
        cmd: dict[str, Any] = {"type": "export_html"}
        if output_path is not None:
            cmd["outputPath"] = output_path
        return await self._send(cmd)

    async def switch_session(self, session_path: str) -> RpcResponse:
        return await self._send({"type": "switch_session", "sessionPath": session_path})

    async def fork(self, entry_id: str) -> RpcResponse:
        return await self._send({"type": "fork", "entryId": entry_id})

    async def get_fork_messages(self) -> RpcResponse:
        return await self._send({"type": "get_fork_messages"})

    async def get_last_assistant_text(self) -> RpcResponse:
        return await self._send({"type": "get_last_assistant_text"})

    async def set_session_name(self, name: str) -> RpcResponse:
        return await self._send({"type": "set_session_name", "name": name})

    async def get_messages(self) -> RpcResponse:
        return await self._send({"type": "get_messages"})

    async def get_commands(self) -> RpcResponse:
        return await self._send({"type": "get_commands"})

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def wait_for_idle(self, timeout: float = 60.0) -> list[dict[str, Any]]:
        """Collect events until an agent_end or timeout. Returns collected events."""
        events: list[dict[str, Any]] = []
        done = asyncio.Event()

        def listener(event: dict[str, Any]) -> None:
            events.append(event)
            if event.get("type") in ("agent_end", "error"):
                done.set()

        unsub = self.on_event(listener)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            unsub()
        return events

    async def collect_events(self, timeout: float = 60.0) -> list[dict[str, Any]]:
        """Collect all events until idle or timeout."""
        return await self.wait_for_idle(timeout=timeout)

    async def prompt_and_wait(
        self,
        message: str,
        images: list[ImageContent] | None = None,
        timeout: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Send a prompt and wait for the agent to become idle. Returns events."""
        events: list[dict[str, Any]] = []
        done = asyncio.Event()

        def listener(event: dict[str, Any]) -> None:
            events.append(event)
            if event.get("type") in ("agent_end", "error"):
                done.set()

        unsub = self.on_event(listener)
        try:
            await self.prompt(message, images)
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            unsub()
        return events


# Re-export for convenience
__all__ = [
    "EventListener",
    "RpcClient",
    "Unsubscribe",
    "serialize_rpc_session_state",
]
