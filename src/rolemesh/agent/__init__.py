"""Agent execution layer -- data types, backend configs, and executor."""

from rolemesh.agent.container_executor import ContainerAgentExecutor
from rolemesh.agent.executor import (
    BACKEND_CONFIGS,
    CLAUDE_CODE_BACKEND,
    PI_BACKEND,
    AgentBackendConfig,
    AgentInput,
    AgentOutput,
)

__all__ = [
    "BACKEND_CONFIGS",
    "CLAUDE_CODE_BACKEND",
    "PI_BACKEND",
    "AgentBackendConfig",
    "AgentInput",
    "AgentOutput",
    "ContainerAgentExecutor",
]
