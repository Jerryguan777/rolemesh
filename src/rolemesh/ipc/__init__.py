"""IPC layer -- NATS-based messaging between Orchestrator and Agent."""

from rolemesh.ipc.nats_transport import NatsTransport
from rolemesh.ipc.protocol import AgentInitData
from rolemesh.ipc.task_handler import IpcDeps, process_task_ipc

__all__ = ["AgentInitData", "IpcDeps", "NatsTransport", "process_task_ipc"]
