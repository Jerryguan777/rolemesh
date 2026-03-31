"""IPC message types for NATS-based communication between Orchestrator and Agent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AgentInitData:
    """Channel 1: initial input written to KV before container starts.

    Shared between orchestrator (serialize) and agent runner (deserialize).
    """

    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    tenant_id: str = ""
    coworker_id: str = ""
    conversation_id: str = ""
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    system_prompt: str | None = None
    role_config: dict[str, object] | None = None

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> AgentInitData:
        raw = json.loads(data)
        return cls(
            prompt=raw["prompt"],
            group_folder=raw["group_folder"],
            chat_jid=raw["chat_jid"],
            is_main=raw["is_main"],
            tenant_id=raw.get("tenant_id", ""),
            coworker_id=raw.get("coworker_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            session_id=raw.get("session_id"),
            is_scheduled_task=raw.get("is_scheduled_task", False),
            assistant_name=raw.get("assistant_name"),
            system_prompt=raw.get("system_prompt"),
            role_config=raw.get("role_config"),
        )
