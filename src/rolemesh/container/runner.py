"""Container specification helpers.

Pure functions for building volume mounts, container specs, and NATS KV
snapshots.  Orchestration logic has moved to agent/container_executor.py.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.container.runtime import (
    CONTAINER_HOST_GATEWAY,
    ContainerSpec,
    VolumeMount,
    get_host_gateway_extra_hosts,
)
from rolemesh.core.config import (
    CONTAINER_IMAGE,
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


def build_container_spec(
    mounts: list[VolumeMount],
    container_name: str,
    job_id: str,
    backend_config: AgentBackendConfig | None = None,
) -> ContainerSpec:
    """Build a ContainerSpec from mounts and config. Replaces build_container_args()."""
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
        env.update(backend_config.extra_env)

    # Run as host user so bind-mounted files are accessible.
    user: str | None = None
    if hasattr(os, "getuid"):
        host_uid = os.getuid()
        host_gid = os.getgid() if hasattr(os, "getgid") else host_uid
        if host_uid != 0 and host_uid != 1000:
            user = f"{host_uid}:{host_gid}"
            env["HOME"] = "/home/agent"

    return ContainerSpec(
        name=container_name,
        image=image,
        mounts=mounts,
        env=env,
        user=user,
        extra_hosts=get_host_gateway_extra_hosts(),
        entrypoint=backend_config.entrypoint if backend_config else None,
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
