"""Container specification helpers.

Pure functions for building volume mounts, container specs, and NATS KV
snapshots.  Orchestration logic has moved to agent/container_executor.py.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rolemesh.container.runtime import (
    CONTAINER_HOST_GATEWAY,
    ContainerSpec,
    VolumeMount,
    get_host_gateway_extra_hosts,
)
from rolemesh.core.config import (
    CONTAINER_IMAGE,
    CREDENTIAL_PROXY_PORT,
    GROUPS_DIR,
    NATS_URL,
    PROJECT_ROOT,
    TIMEZONE,
)
from rolemesh.core.group_folder import resolve_group_folder_path
from rolemesh.core.logger import get_logger
from rolemesh.security.credential_proxy import detect_auth_mode
from rolemesh.security.mount_security import validate_additional_mounts

if TYPE_CHECKING:
    from rolemesh.agent.executor import AgentBackendConfig
    from rolemesh.core.types import RegisteredGroup
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
    "write_groups_snapshot",
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
    group: RegisteredGroup,
    is_main: bool,
    backend_config: AgentBackendConfig | None = None,
) -> list[VolumeMount]:
    """Build volume mounts for a container invocation.

    Note: creates group session directory and default settings if missing.
    """
    mounts: list[VolumeMount] = []
    project_root = PROJECT_ROOT
    group_dir = resolve_group_folder_path(group.folder)

    if is_main:
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
                host_path=str(group_dir),
                container_path="/workspace/group",
                readonly=False,
            )
        )
    else:
        mounts.append(
            VolumeMount(
                host_path=str(group_dir),
                container_path="/workspace/group",
                readonly=False,
            )
        )

        global_dir = GROUPS_DIR / "global"
        if global_dir.exists():
            mounts.append(
                VolumeMount(
                    host_path=str(global_dir),
                    container_path="/workspace/global",
                    readonly=True,
                )
            )

    # Per-group Claude sessions directory
    from rolemesh.core.config import DATA_DIR

    group_sessions_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    group_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = group_sessions_dir / "settings.json"
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

    # Sync skills from container/skills/ into each group's .claude/skills/
    skills_src = project_root / "container" / "skills"
    skills_dst = group_sessions_dir / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            dst_dir = skills_dst / skill_dir.name
            shutil.copytree(str(skill_dir), str(dst_dir), dirs_exist_ok=True)

    mounts.append(
        VolumeMount(
            host_path=str(group_sessions_dir),
            container_path="/home/agent/.claude",
            readonly=False,
        )
    )

    # Additional mounts validated against external allowlist
    if group.container_config and group.container_config.additional_mounts:
        validated_mounts = validate_additional_mounts(
            group.container_config.additional_mounts,
            group.name,
            is_main,
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

    env: dict[str, str] = {
        "TZ": TIMEZONE,
        "NATS_URL": nats_url,
        "JOB_ID": job_id,
        "ANTHROPIC_BASE_URL": f"http://{CONTAINER_HOST_GATEWAY}:{CREDENTIAL_PROXY_PORT}",
    }

    # Mirror the host's auth method with a placeholder value.
    auth_mode = detect_auth_mode()
    if auth_mode == "api-key":
        env["ANTHROPIC_API_KEY"] = "placeholder"
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "placeholder"

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
    group_folder: str,
    is_main: bool,
    tasks: list[dict[str, object]],
) -> None:
    """Write filtered tasks to NATS KV for the agent to read.

    Main sees all tasks, others only see their own.
    """
    filtered_tasks: list[dict[str, object]]
    filtered_tasks = tasks if is_main else [t for t in tasks if t.get("groupFolder") == group_folder]

    kv = await transport.js.key_value("snapshots")
    await kv.put(f"{group_folder}.tasks", json.dumps(filtered_tasks).encode())


async def write_groups_snapshot(
    transport: NatsTransport,
    group_folder: str,
    is_main: bool,
    groups: list[AvailableGroup],
    _registered_jids: set[str],
) -> None:
    """Write available groups snapshot to NATS KV for the agent to read.

    Only main group can see all available groups (for activation).
    """
    visible_groups: list[dict[str, object]]
    if is_main:
        visible_groups = [
            {
                "jid": g.jid,
                "name": g.name,
                "lastActivity": g.last_activity,
                "isRegistered": g.is_registered,
            }
            for g in groups
        ]
    else:
        visible_groups = []

    kv = await transport.js.key_value("snapshots")
    await kv.put(
        f"{group_folder}.groups",
        json.dumps(
            {
                "groups": visible_groups,
                "lastSync": datetime.now(UTC).isoformat(),
            }
        ).encode(),
    )
