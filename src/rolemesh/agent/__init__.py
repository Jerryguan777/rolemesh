"""Agent execution layer -- protocols, data types, and executor implementations."""

from rolemesh.agent.container_executor import ContainerAgentExecutor
from rolemesh.agent.executor import (
    BACKEND_CONFIGS,
    CLAUDE_CODE_BACKEND,
    PI_BACKEND,
    AgentBackendConfig,
    AgentExecutor,
    AgentInput,
    AgentOutput,
)

__all__ = [
    "BACKEND_CONFIGS",
    "CLAUDE_CODE_BACKEND",
    "PI_BACKEND",
    "AgentBackendConfig",
    "AgentExecutor",
    "AgentInput",
    "AgentOutput",
    "ContainerAgentExecutor",
]
