"""Agent execution protocol and data types.

Defines AgentInput, AgentOutput, AgentBackendConfig, and the AgentExecutor
protocol.  Concrete implementations (e.g. ContainerAgentExecutor) live in
separate modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerHandle


@dataclass(frozen=True)
class AgentInput:
    """Input to an agent execution."""

    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
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
    name="claude-code",
    image="rolemesh-agent:latest",
)

PIMONO_BACKEND = AgentBackendConfig(
    name="pi-mono",
    image="ppi-agent:latest",
    entrypoint=["python", "-m", "ppi.coding_agent", "--mode", "rolemesh"],
    skip_claude_session=True,
)


class AgentExecutor(Protocol):
    """Protocol for agent execution backends."""

    @property
    def name(self) -> str: ...

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[ContainerHandle, str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput: ...
