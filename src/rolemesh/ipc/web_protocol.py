"""NATS message types for web channel communication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime


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
        d = json.loads(data)
        return cls(
            chat_id=d["chat_id"],
            sender_id=d["sender_id"],
            sender_name=d["sender_name"],
            text=d["text"],
            timestamp=d["timestamp"],
            msg_id=d["msg_id"],
        )


@dataclass(frozen=True, slots=True)
class WebStreamChunk:
    """Streaming text chunk from Orchestrator to FastAPI (web.stream.{binding_id}.{chat_id}).

    type="text" carries a content fragment; type="done" signals end of stream.
    """

    type: str  # "text" | "done"
    content: str = ""

    def to_bytes(self) -> bytes:
        d: dict[str, str] = {"type": self.type}
        if self.type == "text":
            d["content"] = self.content
        return json.dumps(d).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> WebStreamChunk:
        d = json.loads(data)
        return cls(type=d["type"], content=d.get("content", ""))


@dataclass(frozen=True, slots=True)
class WebTypingMessage:
    """Typing indicator from Orchestrator to FastAPI (web.typing.{binding_id}.{chat_id})."""

    is_typing: bool

    def to_bytes(self) -> bytes:
        return json.dumps({"is_typing": self.is_typing}).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> WebTypingMessage:
        d = json.loads(data)
        return cls(is_typing=d["is_typing"])


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
        d = json.loads(data)
        return cls(text=d["text"], timestamp=d.get("timestamp", ""))
