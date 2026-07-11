"""Container specification helpers.

Pure functions for building volume mounts, container specs, and NATS KV
snapshots.  Orchestration logic has moved to agent/container_executor.py.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.container.docker_runtime import _parse_memory
from rolemesh.container.runtime import (
    ContainerSpec,
    VolumeMount,
)
from rolemesh.core.config import (
    CONTAINER_CPU_LIMIT,
    CONTAINER_ENV_ALLOWLIST,
    CONTAINER_IMAGE,
    CONTAINER_MAX_CPU,
    CONTAINER_MAX_MEMORY,
    CONTAINER_MEMORY_LIMIT,
    CONTAINER_NETWORK_NAME,
    CONTAINER_OCI_RUNTIME,
    CONTAINER_PIDS_LIMIT,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    EGRESS_GATEWAY_CONTAINER_NAME,
    EGRESS_GATEWAY_DNS_IP,
    EGRESS_GATEWAY_FORWARD_PORT,
    NATS_URL,
    TIMEZONE,
)
from rolemesh.core.logger import get_logger
from rolemesh.security.credential_proxy import detect_auth_mode
from rolemesh.security.mount_security import validate_additional_mounts

if TYPE_CHECKING:
    from rolemesh.agent.executor import AgentBackendConfig
    from rolemesh.agent.executor import AgentInput as ContainerInput
    from rolemesh.agent.executor import AgentOutput as ContainerOutput
    from rolemesh.core.types import Coworker
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


def __getattr__(name: str) -> object:
    """Lazy backward-compat aliases (PEP 562).

    ``ContainerInput``/``ContainerOutput`` moved to
    ``rolemesh.agent.executor`` (AgentInput/AgentOutput). Resolving them
    eagerly created an import cycle: this module imported
    ``rolemesh.agent.executor``, whose package ``__init__`` imports
    ``container_executor``, which imports back into this module —
    so ``import rolemesh.container.runner`` as the FIRST rolemesh.agent
    touch crashed with "partially initialized module" (caught by the
    container contract-test conftest, which is exactly such a first
    importer). Production only survived by import-order luck.
    """
    if name == "ContainerInput":
        from rolemesh.agent.executor import AgentInput

        return AgentInput
    if name == "ContainerOutput":
        from rolemesh.agent.executor import AgentOutput

        return AgentOutput
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)

# Re-export VolumeMount from runtime (it used to live here)
__all__ = [
    "AvailableGroup",
    "ContainerInput",
    "ContainerOutput",
    "ContainerSpec",
    "VolumeMount",
    "build_container_spec",
    "build_volume_mounts",
    "write_tasks_snapshot",
]


@dataclass(frozen=True)
class AvailableGroup:
    """A group visible to containers for activation."""

    jid: str
    name: str
    last_activity: str
    is_registered: bool


def build_volume_mounts(
    coworker: Coworker,
    tenant_id: str,
    conversation_id: str,
    permissions: AgentPermissions | None = None,
    backend_config: AgentBackendConfig | None = None,
) -> list[VolumeMount]:
    """Build volume mounts for a container invocation.

    Paths: data/tenants/{tid}/coworkers/{folder}/
    """
    if permissions is None:
        permissions = AgentPermissions()

    mounts: list[VolumeMount] = []
    tenant_dir = DATA_DIR / "tenants" / tenant_id
    coworker_dir = tenant_dir / "coworkers" / coworker.folder
    shared_dir = tenant_dir / "shared"
    session_dir = coworker_dir / "sessions" / conversation_id

    # Workspace: per-coworker, shared across conversations
    workspace_dir = coworker_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    mounts.append(
        VolumeMount(
            host_path=str(workspace_dir),
            container_path="/workspace/group",
            readonly=False,
        )
    )

    # Shared knowledge (read-only)
    if shared_dir.exists():
        mounts.append(
            VolumeMount(
                host_path=str(shared_dir),
                container_path="/workspace/shared",
                readonly=True,
            )
        )

    # Per-conversation session directory
    session_dir.mkdir(parents=True, exist_ok=True)
    mounts.append(
        VolumeMount(
            host_path=str(session_dir),
            container_path="/workspace/sessions",
            readonly=False,
        )
    )

    # Logs directory
    logs_dir = coworker_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    mounts.append(
        VolumeMount(
            host_path=str(logs_dir),
            container_path="/workspace/logs",
            readonly=False,
        )
    )

    # Per-coworker Claude sessions directory
    claude_sessions_dir = coworker_dir / ".claude"
    claude_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = claude_sessions_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text(
            json.dumps(
                {
                    "env": {
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    mounts.append(
        VolumeMount(
            host_path=str(claude_sessions_dir),
            container_path="/home/agent/.claude",
            readonly=False,
        )
    )

    # Additional mounts validated against external allowlist
    if coworker.container_config and coworker.container_config.additional_mounts:
        validated_mounts = validate_additional_mounts(
            coworker.container_config.additional_mounts,
            coworker.name,
        )
        for m in validated_mounts:
            mounts.append(
                VolumeMount(
                    host_path=str(m["host_path"]),
                    container_path=str(m["container_path"]),
                    readonly=bool(m["readonly"]),
                )
            )

    # Apply backend config adjustments
    if backend_config:
        if backend_config.skip_claude_session:
            mounts = [m for m in mounts if ".claude" not in m.container_path]
        for host, container, ro in backend_config.extra_mounts:
            mounts.append(VolumeMount(host_path=host, container_path=container, readonly=ro))

    return mounts


# UID of the `agent` user baked into the image (see container/Dockerfile
# `useradd -u 1000`). Used as a fallback in two cases:
#   1. The host platform has no os.getuid() (Windows dev machine).
#   2. The orchestrator is running as root (host_uid == 0). We deliberately
#      DON'T propagate root into the agent container — that would undo
#      the CapDrop/readonly-rootfs hardening for any operator who hasn't
#      also configured userns-remap at the daemon level.
# In every other case we resolve the runtime UID from the host at spawn
# time and hand both the User field and the tmpfs options the same value.
AGENT_UID = 1000
AGENT_GID = 1000


def _default_security_opt() -> list[str]:
    """Baseline SecurityOpt entries applied to every agent container.

    - no-new-privileges:true  always
    - apparmor=docker-default Linux only (AppArmor is a Linux LSM)

    seccomp is deliberately NOT set here — unset means Docker applies its
    embedded default seccomp profile, which is what we want. Setting
    seccomp=unconfined would DISABLE seccomp; we never emit that value.
    """
    opts = ["no-new-privileges:true"]
    if platform.system() == "Linux":
        opts.append("apparmor=docker-default")
    return opts


def _default_tmpfs(uid: int, gid: int) -> dict[str, str]:
    """Writable tmpfs mounts for a readonly-rootfs container.

    `uid`/`gid` MUST match the UID:GID the container process actually
    runs as (see the `user` field built in build_container_spec). If
    they drift apart, Linux owns the tmpfs by `uid:gid` at mount time
    and the running process hits EACCES on every write — most visibly
    in Pi's first call to mkdir(~/.pi) and Claude CLI's attempt to
    write ~/.claude.json. Manual acceptance on 2026-04-21 caught a
    macOS case where host UID=502 but tmpfs was hardcoded uid=1000;
    hence the parameterization.

    Contents:
      /tmp                        — 64MB, typical scratch space
      /home/agent/.cache          — 64MB, XDG cache (pip/http/etc.)
      /home/agent/.config         — 8MB,  XDG config (some SDKs write defaults)
      /home/agent/.pi             — 32MB, Pi backend global config + sessions
                                    (per-conversation sessions still go to
                                    /workspace/sessions via bind mount; this
                                    only holds ~/.pi/agent settings.json +
                                    in-process runtime state that is
                                    deliberately ephemeral across restarts).

    The real persistent data is on bind mounts (/workspace/*, /home/agent/.claude).
    """
    _uid_opt = f"uid={uid},gid={gid}"
    return {
        "/tmp": "rw,size=64m,mode=1777",
        "/home/agent/.cache": f"rw,size=64m,{_uid_opt},mode=700",
        "/home/agent/.config": f"rw,size=8m,{_uid_opt},mode=700",
        "/home/agent/.pi": f"rw,size=32m,{_uid_opt},mode=700",
    }


def _default_ulimits() -> list[dict[str, object]]:
    return [{"Name": "nofile", "Soft": 1024, "Hard": 2048}]


# Cloud-instance-metadata services. Resolving these to 127.0.0.1 inside the
# container means a compromised agent that tries to exfil IAM creds via
# SSRF-style metadata access gets connection-refused instead of real
# credentials. Covered endpoints:
#   - 169.254.169.254         AWS / GCE / Azure / OpenStack / DO IMDS
#   - metadata.google.internal GCE DNS alias for IMDS
# See: https://cloud.google.com/compute/docs/metadata/overview
_METADATA_BLACKHOLE: dict[str, str] = {
    "metadata.google.internal": "127.0.0.1",
    "169.254.169.254": "127.0.0.1",
}


@dataclass(frozen=True)
class EgressRouting:
    """Per-spawn agent network routing.

    THE single place "where do my bytes go" is decided for the spawn
    path. Everything an agent spawn needs to know about its network
    topology is a field here, computed by
    :func:`compute_egress_routing` — adding a behaviour means adding a
    field, and every consumer is forced through the same decision.

    This is also the staging ground for the planned ContainerPlatform
    abstraction (docker|k8s runtime decoupling): the whole of
    ``compute_egress_routing`` becomes the Docker implementation of
    ``platform.agent_routing()`` when that lands.
    """

    nats_url: str
    proxy_base: str            # http://host:port — no path
    provider_prefix: str       # "/proxy/<token>" or "/proxy"
    proxy_env: dict[str, str]  # HTTP_PROXY/HTTPS_PROXY/NO_PROXY
    network_name: str | None   # named EC bridge, or None = default bridge
    dns_servers: list[str]
    extra_hosts: dict[str, str]
    mcp_proxy_host: str        # host part of agent-facing MCP proxy URLs


def compute_egress_routing(egress_token: str | None) -> EgressRouting:
    """Derive the agent's network routing for one spawn.

    Pure and side-effect free: a single-path read of deployment-layer
    configuration (docs/21 §4.3).

    Agent sits on the Internal=true bridge. NATS is reached by the
    service name the deployment layer injects via ``NATS_URL``
    (compose / Helm declare it; the URL is passed through verbatim);
    LLM/MCP calls go to the gateway container by service name;
    HTTP(S)_PROXY points every client at the forward proxy; DNS is
    pinned to the gateway's resolver — a static address declared by
    the deployment layer (``EGRESS_GATEWAY_DNS_IP``, verified against
    the running gateway at startup by ``verify_infrastructure``); only
    the metadata blackhole rides in extra_hosts.

    The signed token rides in two places (spike-verified): the
    forward-proxy userinfo (clients emit Proxy-Authorization
    automatically) and a leading path segment on each reverse-proxy
    base URL (SDKs preserve the prefix before appending /v1/messages).
    """
    provider_prefix = f"/proxy/{egress_token}" if egress_token else "/proxy"
    extra_hosts: dict[str, str] = dict(_METADATA_BLACKHOLE)

    forward_authority = (
        f"job:{egress_token}@{EGRESS_GATEWAY_CONTAINER_NAME}"
        if egress_token
        else EGRESS_GATEWAY_CONTAINER_NAME
    )
    forward_proxy_url = (
        f"http://{forward_authority}:{EGRESS_GATEWAY_FORWARD_PORT}"
    )
    return EgressRouting(
        nats_url=NATS_URL,
        proxy_base=f"http://{EGRESS_GATEWAY_CONTAINER_NAME}:{CREDENTIAL_PROXY_PORT}",
        provider_prefix=provider_prefix,
        proxy_env={
            "HTTP_PROXY": forward_proxy_url,
            "HTTPS_PROXY": forward_proxy_url,
            "NO_PROXY": f"{EGRESS_GATEWAY_CONTAINER_NAME},localhost,127.0.0.1",
        },
        # ``or None`` keeps an empty bridge name mapping to the
        # default bridge instead of an empty NetworkMode string.
        network_name=CONTAINER_NETWORK_NAME or None,
        dns_servers=[EGRESS_GATEWAY_DNS_IP],
        extra_hosts=extra_hosts,
        mcp_proxy_host=EGRESS_GATEWAY_CONTAINER_NAME,
    )


def _clamp_memory(value: str, max_value: str, *, coworker_name: str) -> str:
    """Return value if within cap, else max_value with a structured warning."""
    req = _parse_memory(value)
    cap = _parse_memory(max_value)
    if req > cap:
        logger.warning(
            "Container memory_limit exceeds global cap — clamping",
            coworker=coworker_name,
            requested=value,
            cap=max_value,
        )
        return max_value
    return value


def _clamp_cpu(value: float, max_value: float, *, coworker_name: str) -> float:
    if value > max_value:
        logger.warning(
            "Container cpu_limit exceeds global cap — clamping",
            coworker=coworker_name,
            requested=value,
            cap=max_value,
        )
        return max_value
    return value


def _filter_env_allowlist(env: dict[str, str], *, source: str) -> dict[str, str]:
    """Drop env keys not in CONTAINER_ENV_ALLOWLIST. Values are never logged."""
    allowed: dict[str, str] = {}
    rejected: list[str] = []
    for k, v in env.items():
        if k in CONTAINER_ENV_ALLOWLIST:
            allowed[k] = v
        else:
            rejected.append(k)
    if rejected:
        logger.warning(
            "Dropping env keys not in allowlist",
            source=source,
            rejected=sorted(rejected),
        )
    return allowed


def build_container_spec(
    mounts: list[VolumeMount],
    container_name: str,
    job_id: str,
    backend_config: AgentBackendConfig | None = None,
    coworker: Coworker | None = None,
    pi_model_id_override: str | None = None,
    egress_token: str | None = None,
) -> ContainerSpec:
    """Build a ContainerSpec from mounts and config.

    Merge order for resource limits: global default ← coworker override ← clamp to max.

    ``egress_token`` (token-identity refactor): a signed identity token
    the orchestrator minted for this spawn. When set, it
    is embedded in the proxy env so the gateway can recover the agent's
    identity in-band — in the forward-proxy URL's userinfo and as a path
    segment on every reverse-proxy base URL. ``None`` (eval CLI and other
    callers without an identity context) keeps the token-free URLs; the
    gateway falls back to source-IP identity for those during the
    dual-run migration window.

    ``pi_model_id_override`` (PR30): the Pi-format model string
    (``<provider>/<model_id>``) the caller resolved from
    ``coworker.model_id`` via the ``models`` table. When set, it
    overrides whatever ``PI_MODEL_ID`` came from the host's .env at
    module-load time. The override path also recomputes any Bedrock-
    specific env (``AWS_BEARER_TOKEN_BEDROCK``, ``AWS_REGION``)
    because switching providers means switching boto3 config too.
    ``None`` keeps the legacy .env-default behavior for callers that
    don't have a coworker context (evaluation CLI, etc.).
    """
    # CONTAINER_IMAGE (deployment-layer env) is the single source of
    # truth for the agent image; a backend_config.image (None — or, per
    # the `or`, empty — by default) only overrides it for backends that
    # genuinely need a different image. The same CONTAINER_IMAGE feeds
    # the orphan-cleanup whitelist in main.py, so the two stay in sync.
    image = (backend_config.image if backend_config else None) or CONTAINER_IMAGE

    # All topology decisions (NATS URL, proxy base, token placement,
    # proxy env, bridge, DNS, extra_hosts) are made in one place —
    # see EgressRouting / compute_egress_routing above.
    routing = compute_egress_routing(egress_token)

    env: dict[str, str] = {
        "TZ": TIMEZONE,
        "NATS_URL": routing.nats_url,
        "JOB_ID": job_id,
        # Per-provider proxy URL. The reverse proxy routes everything
        # through ``/proxy/{provider}/`` since the legacy catch-all was
        # deleted in the chore/config-db-truth credential refactor —
        # without the suffix the SDK hits 404. Both Pi and Claude SDKs
        # honour ANTHROPIC_BASE_URL and append ``/v1/messages`` etc., so
        # the suffix flows through cleanly.
        "ANTHROPIC_BASE_URL": f"{routing.proxy_base}{routing.provider_prefix}/anthropic",
        # Multi-provider proxy URLs for Pi backend (each SDK reads its own env var)
        "OPENAI_BASE_URL": f"{routing.proxy_base}{routing.provider_prefix}/openai",
        # Bedrock — boto3 honours ``BEDROCK_BASE_URL`` as ``endpoint_url``.
        # Same per-spawn ``proxy_base`` as Anthropic/OpenAI so the agent
        # resolves a reachable address. Setting this here rather
        # than in ``_pi_extra_env`` is deliberate — that helper runs at
        # module load time, before the per-spawn routing is decided.
        "BEDROCK_BASE_URL": f"{routing.proxy_base}{routing.provider_prefix}/bedrock",
        # Redirect Claude Code CLI's .claude.json writes into the per-coworker
        # writable bind mount at /home/agent/.claude. Without this, the CLI
        # tries to write /home/agent/.claude.json on the readonly rootfs and
        # the Claude backend fails its 30s initialize handshake.
        # Pi backend ignores this env, so shipping it unconditionally is safe.
        "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
        **routing.proxy_env,
    }

    # Mirror the host's auth method with a placeholder value.
    auth_mode = detect_auth_mode()
    if auth_mode == "api-key":
        env["ANTHROPIC_API_KEY"] = "placeholder"
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "placeholder"
    # Placeholder for OpenAI (Pi reads OPENAI_API_KEY from env)
    env["OPENAI_API_KEY"] = "placeholder"

    if backend_config:
        # Pre-filter backend extra_env to catch misconfigured backends early
        # with a clear attribution to which source emitted the bad key.
        filtered_backend_env = _filter_env_allowlist(
            backend_config.extra_env, source=f"backend_config:{backend_config.name}",
        )
        env.update(filtered_backend_env)

    # Per-coworker model override. backend_config.extra_env has the
    # .env-derived default PI_MODEL_ID baked in at module-load time;
    # recompute it (plus Bedrock boto3 placeholders if the override
    # switches providers) so the UI's model picker actually reaches
    # the container.
    if (
        pi_model_id_override is not None
        and backend_config
        and backend_config.name == "pi"
    ):
        from rolemesh.core.config import BEDROCK_DEFAULT_REGION

        override_env: dict[str, str] = {"PI_MODEL_ID": pi_model_id_override}
        if pi_model_id_override.startswith("amazon-bedrock/"):
            override_env["AWS_BEARER_TOKEN_BEDROCK"] = (
                "placeholder-proxy-replaces-this"
            )
            override_env["AWS_REGION"] = (
                os.environ.get("AWS_REGION", "") or BEDROCK_DEFAULT_REGION
            )
        env.update(
            _filter_env_allowlist(
                override_env,
                source=f"coworker_model:{pi_model_id_override}",
            )
        )

    # Resolve runtime UID/GID. The same pair drives both the `user` field
    # handed to Docker and the tmpfs owner in _default_tmpfs below; they
    # must never drift apart (see tmpfs docstring).
    #
    # Policy:
    #   * Default (no os.getuid, or orchestrator running as root):
    #     fall back to AGENT_UID/GID — image-baked user. Root is
    #     deliberately refused because propagating it into the agent
    #     container would undo CapDrop/readonly-rootfs for operators
    #     who did not configure userns-remap at the daemon level.
    #   * Normal case: run as the host user so bind-mounted host
    #     directories (sessions, workspace, .claude) retain matching
    #     UID ownership. HOME is set explicitly — the runtime UID may
    #     not exist in /etc/passwd, so any SDK that calls
    #     os.path.expanduser('~') needs an env fallback.
    run_uid = AGENT_UID
    run_gid = AGENT_GID
    user: str | None = None
    if hasattr(os, "getuid"):
        host_uid = os.getuid()
        host_gid = os.getgid() if hasattr(os, "getgid") else host_uid
        if host_uid != 0:
            run_uid = host_uid
            run_gid = host_gid
        user = f"{run_uid}:{run_gid}"
        env["HOME"] = "/home/agent"

    # Final allowlist filter across the merged env dict. This is defense in
    # depth — anything added via future code paths will hit this gate too.
    env = _filter_env_allowlist(env, source="orchestrator")

    # Structured log of only env keys (never values); the caller can still
    # surface what went into the container for audit without leaking secrets.
    logger.info(
        "Container env composed",
        container_name=container_name,
        env_keys=sorted(env.keys()),
    )

    # Resource limits: global default ← coworker override ← clamp to max.
    cfg = coworker.container_config if coworker and coworker.container_config else None
    coworker_name = coworker.name if coworker else "<unknown>"
    memory_limit = (cfg.memory_limit if cfg and cfg.memory_limit else CONTAINER_MEMORY_LIMIT)
    memory_limit = _clamp_memory(memory_limit, CONTAINER_MAX_MEMORY, coworker_name=coworker_name)
    cpu_limit = (cfg.cpu_limit if cfg and cfg.cpu_limit else CONTAINER_CPU_LIMIT)
    cpu_limit = _clamp_cpu(cpu_limit, CONTAINER_MAX_CPU, coworker_name=coworker_name)

    # OCI runtime: global default ← coworker override. No "max" clamp here —
    # the downgrade path (coworker flagged as incompatible with runsc) is
    # the whole point, and Docker itself will reject an unregistered runtime.
    oci_runtime = (cfg.runtime if cfg and cfg.runtime else CONTAINER_OCI_RUNTIME)

    return ContainerSpec(
        name=container_name,
        image=image,
        mounts=mounts,
        env=env,
        user=user,
        extra_hosts=routing.extra_hosts,
        entrypoint=backend_config.entrypoint if backend_config else None,
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        security_opt=_default_security_opt(),
        readonly_rootfs=True,
        tmpfs=_default_tmpfs(run_uid, run_gid),
        pids_limit=CONTAINER_PIDS_LIMIT,
        dns=routing.dns_servers,
        ulimits=_default_ulimits(),
        network_name=routing.network_name,
        runtime=oci_runtime,
    )


async def write_tasks_snapshot(
    transport: NatsTransport,
    tenant_id: str,
    coworker_folder: str,
    permissions: AgentPermissions | None = None,
    tasks: list[dict[str, object]] | None = None,
) -> None:
    """Write filtered tasks to NATS KV for the agent to read.

    Agents with ``task_manage_others`` see all tenant tasks; everyone else
    only sees their own (manage requires visibility).
    Key: snapshots.{tenant_id}.{coworker_folder}.tasks
    """
    if tasks is None:
        tasks = []

    if permissions is None:
        permissions = AgentPermissions()

    can_manage_others = permissions.task_manage_others

    filtered_tasks: list[dict[str, object]]
    filtered_tasks = tasks if can_manage_others else [t for t in tasks if t.get("coworkerFolder") == coworker_folder]

    kv = await transport.js.key_value("snapshots")
    key = f"{tenant_id}.{coworker_folder}.tasks"
    await kv.put(key, json.dumps(filtered_tasks).encode())
