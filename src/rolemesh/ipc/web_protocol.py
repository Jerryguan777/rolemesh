"""NATS message types for web channel communication.

Deserialization (``from_bytes``) routes every payload through
``from_dict_filter_unknown`` so unknown-but-future fields are
silently dropped — see ``ipc/_unknown_filter.py`` for the contract
and the INV-2 pinned test for the guarantee.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from rolemesh.ipc._unknown_filter import from_dict_filter_unknown


@dataclass(frozen=True, slots=True)
class WebInboundMessage:
    """User message from FastAPI to Orchestrator (web.inbound.{binding_id})."""

    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: str
    msg_id: str

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "chat_id": self.chat_id,
                "sender_id": self.sender_id,
                "sender_name": self.sender_name,
                "text": self.text,
                "timestamp": self.timestamp,
                "msg_id": self.msg_id,
            }
        ).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> WebInboundMessage:
        return from_dict_filter_unknown(cls, json.loads(data))


@dataclass(frozen=True, slots=True)
class WebStreamChunk:
    """Streaming chunk from Orchestrator to FastAPI (web.stream.{binding_id}.{chat_id}).

    type="text"           — content carries a text fragment
    type="done"           — end-of-stream marker (content ignored)
    type="status"         — content carries a JSON-encoded progress payload
    type="safety_blocked" — content carries a JSON-encoded safety block
                            payload with keys {reason, stage, rule_id?}
    """

    type: str  # "text" | "done" | "status" | "safety_blocked"
    content: str = ""

    def to_bytes(self) -> bytes:
        d: dict[str, str] = {"type": self.type}
        if self.type != "done":
            d["content"] = self.content
        return json.dumps(d).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> WebStreamChunk:
        return from_dict_filter_unknown(cls, json.loads(data))


@dataclass(frozen=True, slots=True)
class WebTypingMessage:
    """Typing indicator from Orchestrator to FastAPI (web.typing.{binding_id}.{chat_id})."""

    is_typing: bool

    def to_bytes(self) -> bytes:
        return json.dumps({"is_typing": self.is_typing}).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> WebTypingMessage:
        return from_dict_filter_unknown(cls, json.loads(data))


@dataclass(frozen=True, slots=True)
class WebOutboundMessage:
    """Complete agent reply from Orchestrator to FastAPI (web.outbound.{binding_id}.{chat_id})."""

    text: str
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            # frozen=True requires object.__setattr__
            object.__setattr__(
                self,
                "timestamp",
                datetime.now(UTC).isoformat(),
            )

    def to_bytes(self) -> bytes:
        return json.dumps({"text": self.text, "timestamp": self.timestamp}).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> WebOutboundMessage:
        return from_dict_filter_unknown(cls, json.loads(data))
