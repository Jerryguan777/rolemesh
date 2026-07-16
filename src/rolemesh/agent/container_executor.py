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
    compute_egress_routing,
)
from rolemesh.container.skill_projection import (
    cleanup_spawn_skills,
    materialize_skills_for_spawn,
)
from rolemesh.core.config import (
    APPROVAL_TIMEOUT,
    CONTAINER_MAX_OUTPUT_SIZE,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    MCP_PROXY_PREFIX,
    TURN_INACTIVITY_TIMEOUT,
)
from rolemesh.core.logger import get_logger
from rolemesh.egress.token_identity import Identity
from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerRuntime
    from rolemesh.core.types import Coworker, McpServerConfig
    from rolemesh.egress.token_identity import TokenAuthority
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


def rewrite_mcp_url_for_container(
    mcp_config: McpServerConfig,
    proxy_host: str,
    proxy_port: int = 3001,
    proxy_prefix: str = "mcp-proxy",
    egress_token: str | None = None,
) -> McpServerSpec:
    """Rewrite an MCP URL to point at the credential proxy.

    ``proxy_host`` has no default on purpose: it is a routing decision
    and must come from ``EgressRouting.mcp_proxy_host`` (the gateway
    service name), keeping every caller on the single spawn-path
    topology source of truth.

    Example:
      input:  McpServerConfig(name="my-mcp-server", type="sse", url="http://mcp-host:9100/mcp/")
      output: McpServerSpec(name="my-mcp-server", type="sse",
                            url="http://egress-gateway:3001/mcp-proxy/my-mcp-server/mcp/")

    The proxy strips the /mcp-proxy/{name} prefix and forwards to the actual URL.
    The trailing path after the host:port is preserved.

    ``egress_token`` (token-identity): when set, inserted as a leading
    path segment — ``/mcp-proxy/<token>/<name>/...`` — so the gateway
    recovers identity from the path the same way it does for the LLM
    reverse-proxy routes. ``None`` keeps the token-free shape for the
    IP-fallback path.
    """
    from urllib.parse import urlparse

    parsed = urlparse(mcp_config.url)
    original_path = parsed.path
    token_seg = f"{egress_token}/" if egress_token else ""
    proxy_url = (
        f"http://{proxy_host}:{proxy_port}/{proxy_prefix}/"
        f"{token_seg}{mcp_config.name}{original_path}"
    )
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
    # Same wire convention as isFinal: only an explicit False means
    # non-retryable; absence (older containers) defaults to retryable so
    # the classification can never accidentally suppress a retry.
    retryable_val = raw.get("retryable")
    retryable = bool(retryable_val) if isinstance(retryable_val, bool) else True
    # Run attribution echo — absent on events from older containers.
    run_id_val = raw.get("runId")
    return AgentOutput(
        status=str(raw.get("status", "error")),  # type: ignore[arg-type]
        result=str(result_val) if result_val is not None else None,
        new_session_id=str(new_sid) if new_sid is not None else None,
        error=str(err_val) if err_val is not None else None,
        metadata=meta_val if isinstance(meta_val, dict) else None,
        is_final=is_final,
        retryable=retryable,
        run_id=str(run_id_val) if isinstance(run_id_val, str) and run_id_val else None,
    )


class ContainerAgentExecutor:
    """Runs agents in containers via ContainerRuntime."""

    def __init__(
        self,
        config: AgentBackendConfig,
        runtime: ContainerRuntime,
        transport: NatsTransport,
        get_coworker: Callable[[str], Coworker | None],
        *,
        get_mcp_configs: Callable[[str], list[McpServerConfig]] | None = None,
        render_catalog: Callable[[str, str], str] | None = None,
        token_authority: TokenAuthority | None = None,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._transport = transport
        self._get_coworker = get_coworker
        # Token-identity refactor: mints the per-spawn signed identity
        # token embedded in the agent's proxy env. The orchestrator
        # wires a real authority from env at startup; tests and the eval
        # CLI leave it None, so spawns produce token-free proxy URLs and
        # the gateway falls back to source-IP identity (dual-run window).
        self._token_authority = token_authority
        # Frontdesk v1.2: optional callback rendering the delegatable-
        # specialist catalog for a tenant. Signature:
        # ``(tenant_id, exclude_coworker_id) -> str``. Invoked at spawn
        # time when ``coworker.is_frontdesk`` is True so the catalog +
        # FRONTDESK_RULES land on the target's effective system prompt
        # (handbook §6 Step 6). None keeps non-frontdesk deployments and
        # tests from having to wire it through.
        self._render_catalog = render_catalog
        # 02b: MCP configs no longer live on ``Coworker``. The executor
        # asks the orchestrator (or eval CLI) for the per-coworker
        # binding list via this callable. Default returns an empty
        # list so call sites that build an executor without wiring it
        # up gracefully degrade to "no MCP servers" rather than
        # raising AttributeError on the missing field.
        self._get_mcp_configs: Callable[[str], list[McpServerConfig]] = (
            get_mcp_configs or (lambda _cid: [])
        )

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

        try:
            return await self._execute_after_setup(
                inp,
                on_process,
                on_output,
                coworker=coworker,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                job_id=job_id,
                start_time=start_time,
                start_epoch_ms=start_epoch_ms,
            )
        finally:
            # Outer safety net for the per-spawn skills directory.
            # The inner ``_execute_after_setup`` explicitly cleans up
            # at each ``return`` for prompt disk reuse on the happy
            # paths, but exceptions raised by ``build_container_spec``,
            # ``self._runtime.run``, the safety loader, or
            # any other line bypass those returns. ``cleanup_spawn_skills``
            # is idempotent, so duplicating with the inner calls is
            # harmless — this finally just guarantees no orphan dir
            # is left behind regardless of how the function exits.
            cleanup_spawn_skills(job_id)

    async def _execute_after_setup(
        self,
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None,
        *,
        coworker: Coworker,
        tenant_id: str,
        conversation_id: str,
        job_id: str,
        start_time: float,
        start_epoch_ms: int,
    ) -> AgentOutput:
        """Spawn the container and drive the conversation.

        Split out from ``execute`` so the outer ``try/finally`` in the
        public method can guarantee ``cleanup_spawn_skills(job_id)``
        runs on every exit path — including exceptions raised by
        ``build_container_spec``, ``self._runtime.run``, the
        safety loader, or any other line in this body.
        The explicit ``cleanup_spawn_skills`` calls below remain so
        disk is reclaimed promptly on the happy paths; the outer
        finally only kicks in when something raises.
        """
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

        # Token-identity: mint the signed token this container will carry
        # in its proxy env; the gateway verifies it with the shared
        # secret. None only when no authority is wired (eval CLI /
        # tests), which yields token-free URLs.
        egress_token: str | None = None
        if self._token_authority is not None:
            egress_token = self._token_authority.mint(
                Identity(
                    tenant_id=tenant_id,
                    coworker_id=inp.coworker_id or "",
                    user_id=inp.user_id or "",
                    conversation_id=conversation_id,
                    job_id=job_id,
                    container_name=container_name,
                )
            )

        # Resolve coworker.model_id → Pi-format string. Falls back to
        # host .env PI_MODEL_ID on any failure (no model_id set,
        # orphan reference, DB blip) — best-effort, never blocks the
        # spawn.
        pi_model_override: str | None = None
        if self._config.name == "pi" and coworker.model_id:
            try:
                from rolemesh.agent.executor import _DB_TO_PI_PROVIDER
                from rolemesh.db import get_model_by_id

                model_row = await get_model_by_id(coworker.model_id)
                if model_row is not None:
                    pi_provider = _DB_TO_PI_PROVIDER.get(
                        model_row.provider, model_row.provider,
                    )
                    pi_model_override = f"{pi_provider}/{model_row.model_id}"
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Pi model_id resolution failed; falling back to "
                    "host PI_MODEL_ID",
                    coworker_id=coworker.id,
                    model_id=coworker.model_id,
                )

        spec = build_container_spec(
            mounts,
            container_name,
            job_id,
            self._config,
            coworker=coworker,
            pi_model_id_override=pi_model_override,
            egress_token=egress_token,
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

        # Build MCP server specs from the coworker's projected bindings.
        # The MCP proxy host comes from the same single source of truth
        # as build_container_spec's LLM env routing (EgressRouting), so a
        # coworker's MCP proxy URL always hits the same endpoint agents
        # use for LLM calls — the gateway service name.
        mcp_proxy_host = compute_egress_routing(egress_token).mcp_proxy_host
        mcp_specs: list[McpServerSpec] | None = None
        coworker_mcp_configs = self._get_mcp_configs(coworker.id)
        if coworker_mcp_configs:
            mcp_specs = [
                rewrite_mcp_url_for_container(
                    tool_cfg,
                    proxy_host=mcp_proxy_host,
                    proxy_port=CREDENTIAL_PROXY_PORT,
                    proxy_prefix=MCP_PROXY_PREFIX,
                    egress_token=egress_token,
                )
                for tool_cfg in coworker_mcp_configs
            ]

        # Load per-coworker safety rules. SAFETY_FAIL_MODE closed
        # default refuses startup; open logs and starts with no rules.
        # None when no
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

        # HITL approval policy snapshot (docs/12-hitl-approval-architecture.md §4).
        # Ship the tenant's enabled policies so the container's approval hook can
        # match a gated MCP call locally without a DB round-trip. None when the
        # tenant has no enabled policies, so the hook stays off the chain
        # (mirrors safety_rules / slow_check_specs zero-cost-when-inactive).
        # ``updated_at`` is serialised to ISO since AgentInitData.serialize is
        # plain json.dumps; ``policies_from_snapshot`` parses it back.
        from rolemesh.db.approval import list_approval_policies

        approval_policies_snapshot: list[dict[str, object]] | None = None
        enabled_policies = await list_approval_policies(tenant_id, enabled_only=True)
        if enabled_policies:
            approval_policies_snapshot = [
                {
                    "id": p.id,
                    "tenant_id": p.tenant_id,
                    "mcp_server_name": p.mcp_server_name,
                    "tool_name": p.tool_name,
                    "condition_expr": p.condition_expr,
                    "enabled": p.enabled,
                    "priority": p.priority,
                    "updated_at": p.updated_at.isoformat(),
                }
                for p in enabled_policies
            ]

        # Frontdesk v1.2: append the delegation catalog + FRONTDESK_RULES to
        # the effective system prompt at spawn time when this coworker is the
        # tenant's frontdesk. The catalog is read from OrchestratorState via
        # the injected ``render_catalog`` callback (handbook §6 Step 6).
        # Specialists and non-frontdesk agents are unaffected. Depends on the
        # ``_coworker_from_state`` fix returning the full config — without it
        # ``coworker.is_frontdesk`` is always False and this never fires.
        effective_system_prompt = inp.system_prompt
        if coworker.is_frontdesk and self._render_catalog is not None:
            from rolemesh.orchestration.catalog import (
                compose_frontdesk_system_prompt,
            )

            catalog_body = self._render_catalog(coworker.tenant_id, coworker.id)
            effective_system_prompt = compose_frontdesk_system_prompt(
                is_frontdesk=True,
                base_system_prompt=inp.system_prompt,
                catalog_body=catalog_body,
            )

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
            run_id=inp.run_id,
            is_scheduled_task=inp.is_scheduled_task,
            assistant_name=inp.assistant_name,
            system_prompt=effective_system_prompt,
            role_config=inp.role_config,
            mcp_servers=mcp_specs,
            safety_rules=safety_rules_dicts,
            slow_check_specs=slow_check_specs,
            approval_policies=approval_policies_snapshot,
        )
        await kv_init.put(job_id, agent_init.serialize())

        # Start container via ContainerRuntime
        handle = await self._runtime.run(spec)
        on_process(container_name, job_id)

        stderr_buf = ""
        stderr_truncated = False
        new_session_id: str | None = None
        had_streaming_output = False
        timed_out = False

        # Per-turn inactivity watchdog (slot-follows-turn rework). The bound is
        # the per-coworker container_config.timeout override when set, else the
        # global TURN_INACTIVITY_TIMEOUT. It is an *inactivity* timer (reset by
        # streamed output below), not a total-runtime cap — a turn that keeps
        # streaming never trips it. Floored at APPROVAL_TIMEOUT + 30s so it can
        # never pre-empt a pending HITL approval (the container emits no output
        # while blocked on a decision); this runtime floor replaces the former
        # APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000 startup invariant.
        base_timeout = (
            coworker.container_config.timeout
            if (coworker.container_config and coworker.container_config.timeout)
            else TURN_INACTIVITY_TIMEOUT
        )
        timeout_ms = max(base_timeout, APPROVAL_TIMEOUT + 30_000)
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
                error=f"Container timed out after {timeout_ms}ms of inactivity",
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

