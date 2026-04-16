"""Agent execution data types and backend configs.

Defines AgentInput, AgentOutput, and AgentBackendConfig.
The concrete executor (ContainerAgentExecutor) lives in container_executor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AgentInput:
    """Input to an agent execution."""

    prompt: str
    group_folder: str
    chat_jid: str
    permissions: dict[str, object]
    tenant_id: str = ""
    coworker_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    system_prompt: str | None = None
    role_config: dict[str, object] | None = None


@dataclass(frozen=True)
class AgentOutput:
    """Output from an agent execution."""

    status: Literal["success", "error"]
    result: str | None
    new_session_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentBackendConfig:
    """Distinguishes different Agent backends.

    A single ContainerAgentExecutor uses this config to select the right
    image, entrypoint, and volume mounts for each backend.
    """

    name: str
    image: str
    entrypoint: list[str] | None = None
    extra_mounts: list[tuple[str, str, bool]] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    skip_claude_session: bool = False


CLAUDE_CODE_BACKEND = AgentBackendConfig(
    name="claude",
    image="rolemesh-agent:latest",
    extra_env={"AGENT_BACKEND": "claude"},
)

PI_BACKEND = AgentBackendConfig(
    name="pi",
    image="rolemesh-agent:latest",
    extra_env={"AGENT_BACKEND": "pi"},
    skip_claude_session=True,
)

# Map backend names to configs for dispatch.
BACKEND_CONFIGS: dict[str, AgentBackendConfig] = {
    "claude": CLAUDE_CODE_BACKEND,
    "claude-code": CLAUDE_CODE_BACKEND,  # legacy alias
    "pi": PI_BACKEND,
}
