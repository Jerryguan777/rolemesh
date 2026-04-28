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
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.container.erofs_watcher import ErofsWatcher
from rolemesh.container.runner import (
    build_container_spec,
    build_volume_mounts,
)
from rolemesh.container.skill_projection import (
    cleanup_spawn_skills,
    materialize_skills_for_spawn,
)
from rolemesh.container.runtime import CONTAINER_HOST_GATEWAY
from rolemesh.core.config import (
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_NETWORK_NAME,
    CONTAINER_TIMEOUT,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    EGRESS_GATEWAY_CONTAINER_NAME,
    IDLE_TIMEOUT,
    MCP_PROXY_PREFIX,
)
from rolemesh.core.logger import get_logger
from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerRuntime
    from rolemesh.core.types import Coworker, McpServerConfig
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


def rewrite_mcp_url_for_container(
    mcp_config: McpServerConfig,
    proxy_host: str = "host.docker.internal",
    proxy_port: int = 3001,
    proxy_prefix: str = "mcp-proxy",
) -> McpServerSpec:
    """Rewrite a host-side MCP URL to point at the credential proxy.

    Example:
      input:  McpServerConfig(name="my-mcp-server", type="sse", url="http://localhost:9100/mcp/")
      output: McpServerSpec(name="my-mcp-server", type="sse",
                            url="http://host.docker.internal:3001/mcp-proxy/my-mcp-server/mcp/")

    The proxy strips the /mcp-proxy/{name} prefix and forwards to the actual URL.
    The trailing path after the host:port is preserved.
    """
    from urllib.parse import urlparse

    parsed = urlparse(mcp_config.url)
    original_path = parsed.path
    proxy_url = f"http://{proxy_host}:{proxy_port}/{proxy_prefix}/{mcp_config.name}{original_path}"
    return McpServerSpec(
        name=mcp_config.name,
        type=mcp_config.type,
        url=proxy_url,
        tool_reversibility=dict(mcp_config.tool_reversibility),
    )


def _parse_container_output(raw: dict[str, object]) -> AgentOutput:
    """Parse a raw JSON dict into an AgentOutput, handling camelCase keys."""
    result_val = raw.get("result")
    new_sid = raw.get("newSessionId")
    err_val = raw.get("error")
    meta_val = raw.get("metadata")
    # isFinal is only emitted when the container has something non-default
    # (False) to say; absence means True (legacy single-reply-per-turn).
    is_final_val = raw.get("isFinal")
    is_final = bool(is_final_val) if isinstance(is_final_val, bool) else True
    return AgentOutput(
        status=str(raw.get("status", "error")),  # type: ignore[arg-type]
        result=str(result_val) if result_val is not None else None,
        new_session_id=str(new_sid) if new_sid is not None else None,
        error=str(err_val) if err_val is not None else None,
        metadata=meta_val if isinstance(meta_val, dict) else None,
        is_final=is_final,
    )


class ContainerAgentExecutor:
    """Runs agents in containers via ContainerRuntime."""

    def __init__(
        self,
        config: AgentBackendConfig,
        runtime: ContainerRuntime,
        transport: NatsTransport,
        get_coworker: Callable[[str], Coworker | None],
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._transport = transport
        self._get_coworker = get_coworker

    @property
    def name(self) -> str:
        return self._config.name

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """Run an agent in a container."""
        start_time = time.monotonic()
        start_epoch_ms = int(time.time() * 1000)

        job_id = f"{inp.group_folder}-{uuid.uuid4().hex[:12]}"

        coworker = self._get_coworker(inp.coworker_id) if inp.coworker_id else None
        if coworker is None:
            return AgentOutput(
                status="error",
                result=None,
                error=f"Coworker not found: {inp.coworker_id}",
            )

        tenant_id = inp.tenant_id or coworker.tenant_id
        conversation_id = inp.conversation_id or ""

        # Ensure coworker directory exists
        coworker_dir = DATA_DIR / "tenants" / tenant_id / "coworkers" / coworker.folder
        coworker_dir.mkdir(parents=True, exist_ok=True)

        permissions = AgentPermissions.from_dict(inp.permissions)
        mounts = build_volume_mounts(
            coworker, tenant_id, conversation_id,
            permissions=permissions, backend_config=self._config,
        )

        # Materialize per-coworker skills to a per-spawn build dir
        # and bind-mount it read-only at the backend's skill path.
        # Returns None if there are no enabled skills, in which case
        # we skip the mount entirely. Cleanup happens in the finally
        # below — including on exceptions raised before the
        # container is even started.
        try:
            skill_mount = await materialize_skills_for_spawn(
                coworker, job_id, backend=self._config.name,
            )
        except Exception as exc:  # noqa: BLE001 — projection bugs must not crash spawn
            logger.warning(
                "Skill projection failed; spawning without skills",
                coworker=coworker.name,
                job_id=job_id,
                error=str(exc),
            )
            skill_mount = None
        if skill_mount is not None:
            mounts.append(skill_mount)
        safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", inp.group_folder)
        container_name = f"rolemesh-{safe_name}-{start_epoch_ms}"

        spec = build_container_spec(
            mounts, container_name, job_id, self._config, coworker=coworker,
        )

        logger.info(
            "Spawning container agent",
            coworker=coworker.name,
            container_name=container_name,
            job_id=job_id,
            mount_count=len(mounts),
            agent_permissions=inp.permissions,
            backend=self._config.name,
            tenant_id=tenant_id,
        )

        logs_dir = coworker_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Build MCP server specs from coworker tools config.
        # proxy_host branches on EC: egress-gateway service name when
        # EC is active, host.docker.internal for the pre-EC rollback
        # path — matches build_container_spec's env routing so a
        # coworker's MCP proxy URL hits the same endpoint agents use
        # for LLM calls.
        mcp_proxy_host = (
            EGRESS_GATEWAY_CONTAINER_NAME
            if CONTAINER_NETWORK_NAME
            else CONTAINER_HOST_GATEWAY
        )
        mcp_specs: list[McpServerSpec] | None = None
        if coworker.tools:
            mcp_specs = [
                rewrite_mcp_url_for_container(
                    tool_cfg,
                    proxy_host=mcp_proxy_host,
                    proxy_port=CREDENTIAL_PROXY_PORT,
                    proxy_prefix=MCP_PROXY_PREFIX,
                )
                for tool_cfg in coworker.tools
            ]

        # Load per-coworker approval policies. Passed to the container
        # as plain dicts so agent_runner.approval.policy (pure, stdlib-
        # only) can evaluate them without a DB import. None when no
        # policies exist, which keeps ApprovalHookHandler off the hook
        # chain in zero-impact deployments.
        approval_policies_dicts: list[dict[str, object]] | None = None
        try:
            from rolemesh.db.pg import get_enabled_policies_for_coworker

            enabled = await get_enabled_policies_for_coworker(
                tenant_id, inp.coworker_id
            )
            if enabled:
                approval_policies_dicts = [p.to_dict() for p in enabled]
        except Exception as exc:
            # The DB is unreachable at job-start. Two operator-selectable
            # responses:
            #   APPROVAL_FAIL_MODE=closed (default) — refuse to start.
            #     A DB outage must not silently let every tool call run
            #     unsupervised; this matches the fail-close posture of
            #     the hook layer itself.
            #   APPROVAL_FAIL_MODE=open — start without approvals.
            #     Legacy behaviour for deployments that prioritize agent
            #     availability over approval coverage during incidents.
            from rolemesh.core.config import APPROVAL_FAIL_MODE

            if APPROVAL_FAIL_MODE == "open":
                logger.warning(
                    "approval: DB unreachable — starting agent in "
                    "fail-open mode (APPROVAL_FAIL_MODE=open). All tool "
                    "calls will run without approval checks until the "
                    "DB recovers and the container restarts.",
                    coworker_id=inp.coworker_id,
                    error=str(exc),
                )
            else:
                # Fail-closed but ALSO silent: the orchestrator does not
                # actively notify the tenant owner/admin. Users see "agent
                # not responding"; operators must have external log alerts
                # wired up to this ERROR line to notice. Acceptable for
                # self-hosted / small-team deployments; for multi-tenant
                # SaaS add a health endpoint + active push (Prometheus
                # counter + in-chat notice to the tenant owner).
                # See docs/approval-architecture.md §Known Gaps
                # "Silent fail-closed on DB outage".
                logger.error(
                    "approval: DB unreachable at job start — refusing "
                    "to start agent (APPROVAL_FAIL_MODE=closed). Set "
                    "APPROVAL_FAIL_MODE=open to permit fail-open "
                    "startup.",
                    coworker_id=inp.coworker_id,
                    error=str(exc),
                )
                raise

        # Load per-coworker safety rules. Same fail-mode contract as
        # approval above (SAFETY_FAIL_MODE closed default refuses
        # startup; open logs and starts with no rules). None when no
        # rules exist, so SafetyHookHandler stays off the hook chain
        # in zero-config deployments. Implementation lives in
        # rolemesh.safety.loader so the fail-mode branch is testable
        # without standing up a Docker container.
        from rolemesh.safety.loader import load_safety_rules_snapshot

        safety_rules_dicts = await load_safety_rules_snapshot(
            tenant_id, inp.coworker_id
        )

        # V2 P0.3: ship slow-check metadata so the container can
        # register RemoteCheck proxies for them. Only emit specs when
        # safety rules are also present — a deployment with rules
        # pointing only at cheap checks would receive a non-empty spec
        # list it couldn't use, wasting memory and printing warnings
        # when the pipeline sees unknown check_ids. The "slow specs
        # only when rules exist" discipline keeps zero-rule deployments
        # bit-identical to pre-V2.
        slow_check_specs: list[dict[str, object]] | None = None
        if safety_rules_dicts:
            from rolemesh.safety.registry import get_orchestrator_registry

            reg = get_orchestrator_registry()
            specs: list[dict[str, object]] = []
            for check in reg.all():
                if getattr(check, "cost_class", "cheap") != "slow":
                    continue
                specs.append(
                    {
                        "check_id": check.id,
                        "version": check.version,
                        "stages": sorted(s.value for s in check.stages),
                        "cost_class": check.cost_class,
                        "supported_codes": sorted(check.supported_codes),
                    }
                )
            if specs:
                slow_check_specs = specs

        # Channel 1: Write initial input to KV before starting container
        kv_init = await self._transport.js.key_value("agent-init")
        agent_init = AgentInitData(
            prompt=inp.prompt,
            group_folder=inp.group_folder,
            chat_jid=inp.chat_jid,
            permissions=inp.permissions,
            tenant_id=tenant_id,
            coworker_id=inp.coworker_id,
            conversation_id=conversation_id,
            user_id=inp.user_id,
            session_id=inp.session_id,
            is_scheduled_task=inp.is_scheduled_task,
            assistant_name=inp.assistant_name,
            system_prompt=inp.system_prompt,
            role_config=inp.role_config,
            mcp_servers=mcp_specs,
            approval_policies=approval_policies_dicts,
            safety_rules=safety_rules_dicts,
            slow_check_specs=slow_check_specs,
        )
        await kv_init.put(job_id, agent_init.serialize())

        # Start container via ContainerRuntime
        handle = await self._runtime.run(spec)
        on_process(container_name, job_id)

        # EC-2: publish agent-started lifecycle event so the egress
        # gateway can map this container's bridge IP to its identity.
        # Wrapped in ``try`` because lifecycle publish is best-effort —
        # the gateway snapshots its identity map on its own schedule
        # and a missed event only widens the unknown-IP window briefly.
        with contextlib.suppress(Exception):
            await _publish_agent_started(
                runtime=self._runtime,
                transport=self._transport,
                container_name=container_name,
                tenant_id=tenant_id,
                coworker_id=inp.coworker_id,
                user_id=inp.user_id,
                conversation_id=inp.conversation_id,
                job_id=job_id,
            )

        stderr_buf = ""
        stderr_truncated = False
        new_session_id: str | None = None
        had_streaming_output = False
        timed_out = False

        config_timeout = (
            coworker.container_config.timeout if coworker.container_config else CONTAINER_TIMEOUT
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
                        coworker=coworker.name,
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
                        coworker=coworker.name,
                        error=str(err),
                    )
                    await msg.ack()

        # Layer-2 hardening defense: surface readonly-rootfs misses so
        # missing tmpfs entries get detected without waiting for user
        # reports. Scoped to EROFS only; mount-security EACCES is tracked
        # elsewhere.
        erofs_watcher = ErofsWatcher(
            coworker_name=coworker.name,
            container_name=container_name,
        )

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
                            erofs_watcher.observe(line)
                    if stderr_truncated:
                        continue
                    remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stderr_buf)
                    if len(chunk) > remaining:
                        stderr_buf += chunk[:remaining]
                        stderr_truncated = True
                        logger.warning(
                            "Container stderr truncated due to size limit",
                            coworker=coworker.name,
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

        # EC-2: publish agent-stopped so the gateway's identity map
        # drops this container. Idempotent on the gateway side; a
        # missed publish widens the stale-IP window until the next
        # identity snapshot.
        with contextlib.suppress(Exception):
            from rolemesh.egress.orch_glue import publish_lifecycle_stopped

            await publish_lifecycle_stopped(
                self._transport.nc, container_name=container_name
            )

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
                        f"Coworker: {coworker.name}",
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
                    coworker=coworker.name,
                    container_name=container_name,
                    duration=duration_ms,
                    code=code,
                )
                cleanup_spawn_skills(job_id)
                return AgentOutput(
                    status="success",
                    result=None,
                    new_session_id=new_session_id,
                )

            logger.error(
                "Container timed out with no output",
                coworker=coworker.name,
                container_name=container_name,
                duration=duration_ms,
                code=code,
            )
            cleanup_spawn_skills(job_id)
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
            f"Coworker: {coworker.name}",
            f"Permissions: {inp.permissions}",
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
                coworker=coworker.name,
                code=code,
                duration=duration_ms,
                stderr=stderr_buf,
                log_file=str(log_file),
            )
            cleanup_spawn_skills(job_id)
            return AgentOutput(
                status="error",
                result=None,
                error=f"Container exited with code {code}: {stderr_buf[-200:]}",
            )

        logger.info(
            "Container completed",
            coworker=coworker.name,
            duration=duration_ms,
            new_session_id=new_session_id,
        )
        cleanup_spawn_skills(job_id)
        return AgentOutput(
            status="success",
            result=None,
            new_session_id=new_session_id,
        )


async def _publish_agent_started(
    *,
    runtime: ContainerRuntime,
    transport: NatsTransport,
    container_name: str,
    tenant_id: str,
    coworker_id: str,
    user_id: str,
    conversation_id: str,
    job_id: str,
) -> None:
    """Inspect the container for its agent-net IP and emit a started event.

    Fails quietly on any error — lifecycle publish is best-effort from
    the orchestrator's perspective. A missed event widens the gateway's
    unknown-source-IP window until the next snapshot, which is a
    brief-block failure mode, not a cross-tenant leak.
    """
    ensure_client = getattr(runtime, "_ensure_client", None)
    if ensure_client is None:
        return
    try:
        client = ensure_client()
        info = await client.containers.container(container_name).show()
    except Exception:  # noqa: BLE001 — publish is optional
        return

    network_settings = info.get("NetworkSettings", {}) or {}
    networks = network_settings.get("Networks", {}) or {}
    network_info = networks.get(CONTAINER_NETWORK_NAME, {}) or {}
    ip = network_info.get("IPAddress", "")
    if not ip:
        return

    from rolemesh.egress.orch_glue import publish_lifecycle_started

    await publish_lifecycle_started(
        transport.nc,
        container_name=container_name,
        ip=ip,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        user_id=user_id,
        conversation_id=conversation_id,
        job_id=job_id,
    )

