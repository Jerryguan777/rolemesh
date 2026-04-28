"""Per-sample container execution for the eval framework.

Reuses the production ``ContainerAgentExecutor`` rather than rolling a
parallel orchestrator. The price is a few zero-config noop paths inside
the executor (approval/safety hook chains stay unwired, EC-2 lifecycle
publishes are suppressed when EC is off, an empty ``container-*.log``
gets written per run). The benefit is that any improvement to the
production container path — including tool wiring, MCP rewriting,
backend selection — is exercised by eval too.

The eval-specific concerns this module owns:
  * sample isolation — every sample uses its own ``group_folder`` and
    ``chat_jid`` keyed on ``(run_id, sample_idx)`` so backend session
    files and KV entries don't cross-pollute.
  * event collection — a custom ``on_output`` callback accumulates
    ``ToolUseEvent`` names, the final ``ResultEvent`` text, and the
    last reported ``UsageSnapshot``. The orchestrator's message-storage
    side effect path is intentionally not invoked.
  * shutdown — production agent containers stay alive for follow-up
    turns; eval wants exactly one turn per container, so we publish
    ``agent.{job_id}.shutdown`` as soon as the batch-final marker
    arrives. Without this, every sample would block ``handle.wait()``
    for the full ``CONTAINER_TIMEOUT`` (~30 min) before reaping.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rolemesh.agent.container_executor import ContainerAgentExecutor
from rolemesh.agent.executor import (
    BACKEND_CONFIGS,
    AgentBackendConfig,
    AgentInput,
    AgentOutput,
)
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.container.runtime import ContainerRuntime
    from rolemesh.core.types import Coworker
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


# Internal-tag pattern matches the production orchestrator's stripping
# in main._on_output so eval scores the same text the user would see.
_INTERNAL_RE = re.compile(r"<internal>[\s\S]*?</internal>")


@dataclass
class SampleExecution:
    """What the runner produces for one sample."""

    output_text: str
    observed_tool_calls: list[str]
    usage: dict[str, Any] | None
    latency_ms: int
    status: str
    error: str | None = None
    result_event_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _backend_for_coworker(coworker: Coworker) -> AgentBackendConfig:
    """Pick the AgentBackendConfig that matches a coworker's backend.

    Falls back to the claude config if the coworker references an
    unknown backend — same behavior as the production scheduler when
    it dispatches a turn.
    """
    name = coworker.agent_backend or "claude-code"
    return BACKEND_CONFIGS.get(name, BACKEND_CONFIGS["claude-code"])


def _safe_chat_jid(group_folder: str) -> str:
    """Slot a chat_jid that won't collide with real channel ids."""
    return f"{group_folder}@eval"


class EvalRunner:
    """Runs eval samples through the production container executor.

    One instance is shared across all samples in an eval run.
    ``executor_factory`` is exposed so tests (and the
    ``EVAL_BACKEND_FACTORY`` escape hatch) can swap in a stub without
    standing up Docker.
    """

    def __init__(
        self,
        *,
        runtime: ContainerRuntime,
        transport: NatsTransport,
        get_coworker: Any,  # Callable[[str], Coworker | None]
        run_id: str,
        timeout_s: float = 300.0,
    ) -> None:
        self._runtime = runtime
        self._transport = transport
        self._get_coworker = get_coworker
        self._run_id = run_id
        self._timeout_s = timeout_s
        self._executors: dict[str, ContainerAgentExecutor] = {}

    def _executor(self, backend: AgentBackendConfig) -> ContainerAgentExecutor:
        """Cache one executor per backend to amortize setup cost."""
        ex = self._executors.get(backend.name)
        if ex is None:
            ex = ContainerAgentExecutor(
                config=backend,
                runtime=self._runtime,
                transport=self._transport,
                get_coworker=self._get_coworker,
            )
            self._executors[backend.name] = ex
        return ex

    async def execute_sample(
        self,
        *,
        coworker: Coworker,
        sample_idx: int,
        prompt: str,
    ) -> SampleExecution:
        """Run one sample end-to-end. Never raises — failures are
        captured as ``status='error'`` so the eval continues.
        """
        # Per-sample isolation. group_folder participates in container
        # names and KV keys; chat_jid prefixes WS routing. Both are
        # eval-prefixed so production grep/triage filters do not pick
        # them up. session_id is pinned to None so each sample opens a
        # fresh backend session — no transcript bleed-through.
        group_folder = f"eval-{self._run_id}-{sample_idx}"
        chat_jid = _safe_chat_jid(group_folder)

        # Use the live coworker's permissions so eval mirrors what the
        # production user would see; the eval framework does not try to
        # widen or narrow the role for evaluation purposes.
        permissions = coworker.permissions or AgentPermissions()

        agent_input = AgentInput(
            prompt=prompt,
            group_folder=group_folder,
            chat_jid=chat_jid,
            permissions=permissions.to_dict(),
            tenant_id=coworker.tenant_id,
            coworker_id=coworker.id,
            # conversation_id participates in the session-file path on
            # the Pi backend (``/workspace/sessions/{conversation_id}.jsonl``).
            # Empty string would make every sample share a single
            # ``.jsonl`` file and pick up stale transcript state from a
            # prior run; per-sample group_folder keeps each turn on its
            # own session file. Claude backend ignores this for session
            # routing (it goes through SDK options).
            conversation_id=group_folder,
            user_id="",
            session_id=None,
            is_scheduled_task=False,
            assistant_name=coworker.name,
            system_prompt=coworker.system_prompt,
            role_config=None,
        )

        observed_tool_calls: list[str] = []
        last_usage: dict[str, Any] | None = None
        last_result_text: str | None = None
        result_event_count = 0
        safety_block: dict[str, Any] | None = None
        captured_job_id: str | None = None
        shutdown_tasks: set[asyncio.Task[None]] = set()

        def _on_process(_container_name: str, job_id: str) -> None:
            nonlocal captured_job_id
            captured_job_id = job_id

        async def _request_shutdown() -> None:
            """Wind the container down after a turn so we don't sit
            on the executor's 30-min idle timer between samples.

            Best-effort: a 5 s request-reply matches the production
            scheduler's contract (``container/scheduler.py:request_shutdown``);
            any failure is swallowed because the timeout watcher in the
            executor is the eventual backstop.
            """
            if captured_job_id is None:
                return
            try:
                await self._transport.nc.request(
                    f"agent.{captured_job_id}.shutdown",
                    b"shutdown",
                    timeout=5.0,
                )
            except (OSError, TimeoutError):
                logger.debug(
                    "eval shutdown not acked",
                    run_id=self._run_id,
                    sample_idx=sample_idx,
                    job_id=captured_job_id,
                )

        async def _on_output(out: AgentOutput) -> None:
            nonlocal last_usage, last_result_text, result_event_count, safety_block
            if out.status == "tool_use":
                meta = out.metadata or {}
                tool_name = meta.get("tool")
                if isinstance(tool_name, str) and tool_name:
                    observed_tool_calls.append(tool_name)
                return
            if out.status == "safety_blocked":
                meta = out.metadata or {}
                safety_block = {
                    "stage": meta.get("stage"),
                    "rule_id": meta.get("rule_id"),
                    "reason": out.result,
                }
                usage = (out.metadata or {}).get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
                # Safety block is terminal for the turn; ask the
                # container to wind down.
                # Fire-and-forget; the executor's timeout watcher is the
                # eventual backstop if the request hangs. Pinned in
                # ``shutdown_tasks`` so the task isn't garbage-collected
                # mid-flight (RUF006).
                shutdown_tasks.add(asyncio.create_task(_request_shutdown()))
                return
            if out.status == "success":
                # The wire format produces two success events per turn:
                # backend ``ResultEvent`` (carries the assistant text,
                # ``is_final=False``) and the agent_runner batch-final
                # marker (``result=None``, ``is_final=True``). Capture
                # text from any non-empty ``result`` and treat
                # ``is_final=True`` purely as the "send shutdown" trigger.
                if isinstance(out.result, str) and out.result:
                    raw = out.result
                    last_result_text = _INTERNAL_RE.sub("", raw).strip()
                    result_event_count += 1
                usage = (out.metadata or {}).get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
                if out.is_final:
                    # Fire-and-forget; pinned in ``shutdown_tasks`` so
                    # the task isn't garbage-collected mid-flight
                    # (RUF006). The executor's timeout watcher is the
                    # eventual backstop if the request hangs.
                    shutdown_tasks.add(asyncio.create_task(_request_shutdown()))
                return
            if out.status in ("error", "stopped"):
                usage = (out.metadata or {}).get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
                # Either status is terminal — ask the container to exit
                # so the next sample doesn't sit waiting for the timeout
                # watcher to fire.
                # Fire-and-forget; the executor's timeout watcher is the
                # eventual backstop if the request hangs. Pinned in
                # ``shutdown_tasks`` so the task isn't garbage-collected
                # mid-flight (RUF006).
                shutdown_tasks.add(asyncio.create_task(_request_shutdown()))
                return

        backend = _backend_for_coworker(coworker)
        executor = self._executor(backend)

        start = time.monotonic()
        try:
            final = await executor.execute(
                agent_input,
                on_process=_on_process,
                on_output=_on_output,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.exception(
                "eval sample raised", run_id=self._run_id, sample_idx=sample_idx
            )
            return SampleExecution(
                output_text="",
                observed_tool_calls=observed_tool_calls,
                usage=last_usage,
                latency_ms=latency_ms,
                status="error",
                error=f"executor raised: {exc!r}",
                result_event_count=result_event_count,
            )
        latency_ms = int((time.monotonic() - start) * 1000)

        # Translate executor terminal status into eval status. The
        # executor returns "success" for graceful exits even when no
        # ResultEvent was streamed; eval treats "no final reply" as
        # error so accuracy isn't padded by silent failures.
        status = final.status
        error = final.error
        if status == "success" and last_result_text is None and safety_block is None:
            status = "error"
            error = error or "no final reply from agent"
        if safety_block is not None:
            status = "safety_blocked"

        return SampleExecution(
            output_text=last_result_text or "",
            observed_tool_calls=observed_tool_calls,
            usage=last_usage,
            latency_ms=latency_ms,
            status=status,
            error=error,
            result_event_count=result_event_count,
            metadata={"safety_block": safety_block} if safety_block else {},
        )
