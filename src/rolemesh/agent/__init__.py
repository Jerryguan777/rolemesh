"""Agent execution layer -- protocols, data types, and executor implementations."""

from rolemesh.agent.container_executor import ContainerAgentExecutor
from rolemesh.agent.executor import (
    CLAUDE_CODE_BACKEND,
    PIMONO_BACKEND,
    AgentBackendConfig,
    AgentExecutor,
    AgentInput,
    AgentOutput,
)

__all__ = [
    "CLAUDE_CODE_BACKEND",
    "PIMONO_BACKEND",
    "AgentBackendConfig",
    "AgentExecutor",
    "AgentInput",
    "AgentOutput",
    "ContainerAgentExecutor",
]
