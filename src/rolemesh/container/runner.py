"""Container specification helpers.

Pure functions for building volume mounts, container specs, and NATS KV
snapshots.  Orchestration logic has moved to agent/container_executor.py.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.container.docker_runtime import _parse_memory
from rolemesh.container.runtime import (
    CONTAINER_HOST_GATEWAY,
    ContainerSpec,
    VolumeMount,
    get_host_gateway_extra_hosts,
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
    NATS_URL,
    PROJECT_ROOT,
    TIMEZONE,
)
from rolemesh.core.logger import get_logger
from rolemesh.security.credential_proxy import detect_auth_mode
from rolemesh.security.mount_security import validate_additional_mounts

if TYPE_CHECKING:
    from rolemesh.agent.executor import AgentBackendConfig
    from rolemesh.core.types import Coworker
    from rolemesh.ipc.nats_transport import NatsTransport

# Backward-compat aliases
from rolemesh.agent.executor import AgentInput as ContainerInput
from rolemesh.agent.executor import AgentOutput as ContainerOutput

logger = get_logger()

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
    # Legacy parameter — ignored if permissions is set
    is_main: bool = False,
) -> list[VolumeMount]:
    """Build volume mounts for a container invocation.

    Paths: data/tenants/{tid}/coworkers/{folder}/
    """
    # Resolve effective permissions
    if permissions is None:
        permissions = AgentPermissions.for_role("super_agent" if is_main else "agent")

    has_tenant_scope = permissions.data_scope == "tenant"
    # Mount r/w policy uses agent_role (not data_scope) to avoid granting
    # write access when only data_scope is overridden to "tenant".
    is_super_agent = coworker.agent_role == "super_agent"

    mounts: list[VolumeMount] = []
    project_root = PROJECT_ROOT
    tenant_dir = DATA_DIR / "tenants" / tenant_id
    coworker_dir = tenant_dir / "coworkers" / coworker.folder
    shared_dir = tenant_dir / "shared"
    session_dir = coworker_dir / "sessions" / conversation_id

    # Workspace: per-coworker, shared across conversations
    workspace_dir = coworker_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    if has_tenant_scope:
        mounts.append(
            VolumeMount(
                host_path=str(project_root),
                container_path="/workspace/project",
                readonly=True,
            )
        )
        # Shadow .env so the agent cannot read secrets
        env_file = project_root / ".env"
        if env_file.exists():
            mounts.append(
                VolumeMount(
                    host_path="/dev/null",
                    container_path="/workspace/project/.env",
                    readonly=True,
                )
            )

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

    # Sync skills
    skills_src = project_root / "container" / "skills"
    skills_dst = claude_sessions_dir / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            dst_dir = skills_dst / skill_dir.name
            shutil.copytree(str(skill_dir), str(dst_dir), dirs_exist_ok=True)

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
            is_super_agent=is_super_agent,
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


def _build_extra_hosts() -> dict[str, str]:
    """Merge host-gateway (Linux) with metadata blackhole entries."""
    hosts = dict(get_host_gateway_extra_hosts())
    hosts.update(_METADATA_BLACKHOLE)
    return hosts


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
) -> ContainerSpec:
    """Build a ContainerSpec from mounts and config.

    Merge order for resource limits: global default ← coworker override ← clamp to max.
    """
    image = backend_config.image if backend_config else CONTAINER_IMAGE
    nats_url = NATS_URL.replace("localhost", CONTAINER_HOST_GATEWAY)

    proxy_base = f"http://{CONTAINER_HOST_GATEWAY}:{CREDENTIAL_PROXY_PORT}"

    env: dict[str, str] = {
        "TZ": TIMEZONE,
        "NATS_URL": nats_url,
        "JOB_ID": job_id,
        # Legacy: Claude backend reads ANTHROPIC_BASE_URL directly (no /proxy prefix)
        "ANTHROPIC_BASE_URL": proxy_base,
        # Multi-provider proxy URLs for Pi backend (each SDK reads its own env var)
        "OPENAI_BASE_URL": f"{proxy_base}/proxy/openai",
        # Redirect Claude Code CLI's .claude.json writes into the per-coworker
        # writable bind mount at /home/agent/.claude. Without this, the CLI
        # tries to write /home/agent/.claude.json on the readonly rootfs and
        # the Claude backend fails its 30s initialize handshake.
        # Pi backend ignores this env, so shipping it unconditionally is safe.
        "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
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
        extra_hosts=_build_extra_hosts(),
        entrypoint=backend_config.entrypoint if backend_config else None,
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        security_opt=_default_security_opt(),
        readonly_rootfs=True,
        tmpfs=_default_tmpfs(run_uid, run_gid),
        pids_limit=CONTAINER_PIDS_LIMIT,
        ulimits=_default_ulimits(),
        network_name=CONTAINER_NETWORK_NAME or None,
        runtime=oci_runtime,
    )


async def write_tasks_snapshot(
    transport: NatsTransport,
    tenant_id: str,
    coworker_folder: str,
    permissions: AgentPermissions | None = None,
    tasks: list[dict[str, object]] | None = None,
    # Legacy parameter — ignored if permissions is set
    is_main: bool = False,
) -> None:
    """Write filtered tasks to NATS KV for the agent to read.

    Tenant-scope sees all tasks, self-scope only sees own.
    Key: snapshots.{tenant_id}.{coworker_folder}.tasks
    """
    if tasks is None:
        tasks = []

    # Resolve effective permissions
    if permissions is None:
        permissions = AgentPermissions.for_role("super_agent" if is_main else "agent")

    has_tenant_scope = permissions.data_scope == "tenant"

    filtered_tasks: list[dict[str, object]]
    filtered_tasks = tasks if has_tenant_scope else [t for t in tasks if t.get("coworkerFolder") == coworker_folder]

    kv = await transport.js.key_value("snapshots")
    key = f"{tenant_id}.{coworker_folder}.tasks"
    await kv.put(key, json.dumps(filtered_tasks).encode())
