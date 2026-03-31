"""Container-based agent executor.

Moves orchestration logic from runner.run_container_agent() into a class
that uses ContainerRuntime (Docker API) instead of subprocess calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rolemesh.agent.executor import AgentBackendConfig, AgentInput, AgentOutput
from rolemesh.container.runner import (
    build_container_spec,
    build_volume_mounts,
)
from rolemesh.core.config import (
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_TIMEOUT,
    IDLE_TIMEOUT,
)
from rolemesh.core.group_folder import resolve_group_folder_path
from rolemesh.core.logger import get_logger
from rolemesh.ipc.protocol import AgentInitData

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerHandle, ContainerRuntime
    from rolemesh.core.types import RegisteredGroup
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


def _parse_container_output(raw: dict[str, object]) -> AgentOutput:
    """Parse a raw JSON dict into an AgentOutput, handling camelCase keys."""
    result_val = raw.get("result")
    new_sid = raw.get("newSessionId")
    err_val = raw.get("error")
    return AgentOutput(
        status=str(raw.get("status", "error")),  # type: ignore[arg-type]
        result=str(result_val) if result_val is not None else None,
        new_session_id=str(new_sid) if new_sid is not None else None,
        error=str(err_val) if err_val is not None else None,
    )


class ContainerAgentExecutor:
    """Runs agents in containers via ContainerRuntime.

    Logic moved from run_container_agent(), using ContainerRuntime.run(spec)
    instead of subprocess.  Backend config selects image, entrypoint, mounts.
    """

    def __init__(
        self,
        config: AgentBackendConfig,
        runtime: ContainerRuntime,
        transport: NatsTransport,
        registered_groups: Callable[[], dict[str, RegisteredGroup]],
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._transport = transport
        self._registered_groups = registered_groups

    @property
    def name(self) -> str:
        return self._config.name

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[ContainerHandle, str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """Run an agent in a container. Replaces run_container_agent()."""
        start_time = time.monotonic()
        start_epoch_ms = int(time.time() * 1000)

        group_dir = resolve_group_folder_path(inp.group_folder)
        group_dir.mkdir(parents=True, exist_ok=True)

        job_id = f"{inp.group_folder}-{uuid.uuid4().hex[:12]}"

        # Look up group for mount computation
        groups = self._registered_groups()
        group = next((g for g in groups.values() if g.folder == inp.group_folder), None)
        if group is None:
            return AgentOutput(
                status="error",
                result=None,
                error=f"Group not found: {inp.group_folder}",
            )

        mounts = build_volume_mounts(group, inp.is_main, self._config)
        safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", inp.group_folder)
        container_name = f"rolemesh-{safe_name}-{start_epoch_ms}"

        spec = build_container_spec(mounts, container_name, job_id, self._config)

        logger.info(
            "Spawning container agent",
            group=group.name,
            container_name=container_name,
            job_id=job_id,
            mount_count=len(mounts),
            is_main=inp.is_main,
            backend=self._config.name,
        )

        logs_dir = group_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Channel 1: Write initial input to KV before starting container
        kv_init = await self._transport.js.key_value("agent-init")
        agent_init = AgentInitData(
            prompt=inp.prompt,
            group_folder=inp.group_folder,
            chat_jid=inp.chat_jid,
            is_main=inp.is_main,
            session_id=inp.session_id,
            is_scheduled_task=inp.is_scheduled_task,
            assistant_name=inp.assistant_name,
            system_prompt=inp.system_prompt,
            role_config=inp.role_config,
        )
        await kv_init.put(job_id, agent_init.serialize())

        # Start container via ContainerRuntime (NOT subprocess)
        handle = await self._runtime.run(spec)
        on_process(handle, container_name, job_id)

        stderr_buf = ""
        stderr_truncated = False
        new_session_id: str | None = None
        had_streaming_output = False
        timed_out = False

        config_timeout = (
            group.container_config.timeout if group.container_config else CONTAINER_TIMEOUT
        ) or CONTAINER_TIMEOUT
        timeout_ms = max(config_timeout, IDLE_TIMEOUT + 30_000)
        timeout_s = timeout_ms / 1000.0

        # Timeout management
        activity_event = asyncio.Event()

        async def _timeout_watcher() -> None:
            nonlocal timed_out
            while True:
                activity_event.clear()
                try:
                    await asyncio.wait_for(activity_event.wait(), timeout=timeout_s)
                except TimeoutError:
                    timed_out = True
                    logger.error(
                        "Container timeout, stopping gracefully",
                        group=group.name,
                        container_name=container_name,
                    )
                    with contextlib.suppress(OSError):
                        await handle.stop(timeout=15)
                    return

        timeout_task = asyncio.create_task(_timeout_watcher())

        # Channel 2: Subscribe to JetStream for streaming results
        results_sub = None
        if on_output is not None:
            results_sub = await self._transport.js.subscribe(f"agent.{job_id}.results")

        async def _read_results() -> None:
            nonlocal new_session_id, had_streaming_output
            if results_sub is None:
                return
            async for msg in results_sub.messages:
                try:
                    raw = json.loads(msg.data)
                    parsed = _parse_container_output(raw)
                    if parsed.new_session_id:
                        new_session_id = parsed.new_session_id
                    had_streaming_output = True
                    activity_event.set()
                    if on_output is not None:
                        await on_output(parsed)
                    await msg.ack()
                except (json.JSONDecodeError, KeyError, TypeError) as err:
                    logger.warning(
                        "Failed to parse streamed output",
                        group=group.name,
                        error=str(err),
                    )
                    await msg.ack()

        async def _read_stderr() -> None:
            nonlocal stderr_buf, stderr_truncated
            try:
                async for chunk_bytes in handle.read_stderr():
                    chunk = (
                        chunk_bytes.decode("utf-8", errors="replace")
                        if isinstance(chunk_bytes, bytes)
                        else str(chunk_bytes)
                    )
                    lines = chunk.strip().split("\n")
                    for line in lines:
                        if line:
                            logger.debug(line, container=inp.group_folder)
                    if stderr_truncated:
                        continue
                    remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stderr_buf)
                    if len(chunk) > remaining:
                        stderr_buf += chunk[:remaining]
                        stderr_truncated = True
                        logger.warning(
                            "Container stderr truncated due to size limit",
                            group=group.name,
                            size=len(stderr_buf),
                        )
                    else:
                        stderr_buf += chunk
            except (OSError, RuntimeError) as err:
                logger.debug("Stderr read stopped", error=str(err))

        # Run readers concurrently
        tasks: list[asyncio.Task[None]] = [asyncio.create_task(_read_stderr())]
        if results_sub is not None:
            tasks.append(asyncio.create_task(_read_results()))

        # Wait for container to exit
        code = await handle.wait()

        # Cancel the results subscription and readers
        if results_sub is not None:
            await results_sub.unsubscribe()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        timeout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_task

        duration_ms = int((time.monotonic() - start_time) * 1000)

        if timed_out:
            ts = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
            timeout_log = logs_dir / f"container-{ts}.log"
            timeout_log.write_text(
                "\n".join(
                    [
                        "=== Container Run Log (TIMEOUT) ===",
                        f"Timestamp: {datetime.now(UTC).isoformat()}",
                        f"Group: {group.name}",
                        f"Container: {container_name}",
                        f"Job ID: {job_id}",
                        f"Duration: {duration_ms}ms",
                        f"Exit Code: {code}",
                        f"Had Streaming Output: {had_streaming_output}",
                    ]
                ),
                encoding="utf-8",
            )

            if had_streaming_output:
                logger.info(
                    "Container timed out after output (idle cleanup)",
                    group=group.name,
                    container_name=container_name,
                    duration=duration_ms,
                    code=code,
                )
                return AgentOutput(
                    status="success",
                    result=None,
                    new_session_id=new_session_id,
                )

            logger.error(
                "Container timed out with no output",
                group=group.name,
                container_name=container_name,
                duration=duration_ms,
                code=code,
            )
            return AgentOutput(
                status="error",
                result=None,
                error=f"Container timed out after {config_timeout}ms",
            )

        timestamp = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
        log_file = logs_dir / f"container-{timestamp}.log"
        is_verbose = os.environ.get("LOG_LEVEL") in ("debug", "trace")

        log_lines: list[str] = [
            "=== Container Run Log ===",
            f"Timestamp: {datetime.now(UTC).isoformat()}",
            f"Group: {group.name}",
            f"IsMain: {inp.is_main}",
            f"Job ID: {job_id}",
            f"Duration: {duration_ms}ms",
            f"Exit Code: {code}",
            f"Stderr Truncated: {stderr_truncated}",
            "",
        ]

        is_error = code != 0

        if is_verbose or is_error:
            log_lines.extend(
                [
                    "=== Input Summary ===",
                    f"Prompt length: {len(inp.prompt)} chars",
                    f"Session ID: {inp.session_id or 'new'}",
                    f"Job ID: {job_id}",
                    "",
                    "=== Mounts ===",
                    "\n".join(f"{m.host_path} -> {m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts),
                    "",
                    f"=== Stderr{' (TRUNCATED)' if stderr_truncated else ''} ===",
                    stderr_buf,
                ]
            )
        else:
            log_lines.extend(
                [
                    "=== Input Summary ===",
                    f"Prompt length: {len(inp.prompt)} chars",
                    f"Session ID: {inp.session_id or 'new'}",
                    "",
                    "=== Mounts ===",
                    "\n".join(f"{m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts),
                    "",
                ]
            )

        log_file.write_text("\n".join(log_lines), encoding="utf-8")
        logger.debug("Container log written", log_file=str(log_file), verbose=is_verbose)

        if code != 0:
            logger.error(
                "Container exited with error",
                group=group.name,
                code=code,
                duration=duration_ms,
                stderr=stderr_buf,
                log_file=str(log_file),
            )
            return AgentOutput(
                status="error",
                result=None,
                error=f"Container exited with code {code}: {stderr_buf[-200:]}",
            )

        logger.info(
            "Container completed",
            group=group.name,
            duration=duration_ms,
            new_session_id=new_session_id,
        )
        return AgentOutput(
            status="success",
            result=None,
            new_session_id=new_session_id,
        )
