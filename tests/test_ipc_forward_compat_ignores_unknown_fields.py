"""INV-2 pinned test: IPC dataclass deserialization is forward-compat.

The orchestrator and the container roll independently. When the
orchestrator gains a new field, an older container must still be able
to parse the payload — and the missing-required guarantee must NOT
regress (a payload missing ``chat_id`` must blow up, not silently
default).

Anti-mirror discipline:
- We build the JSON by hand, never by ``asdict(known_instance)`` — a
  mirror test would happily pass even if the filter were stubbed out.
- We use real JSON bytes through the real ``from_bytes`` /
  ``deserialize`` path. No mocks: the contract is the wire format.
"""

from __future__ import annotations

import json

import pytest

from rolemesh.ipc._unknown_filter import from_dict_filter_unknown
from rolemesh.ipc.protocol import AgentInitData, McpServerSpec
from rolemesh.ipc.web_protocol import (
    WebInboundMessage,
    WebOutboundMessage,
    WebStreamChunk,
    WebTypingMessage,
)


# ---------------------------------------------------------------------------
# Forward-compat: unknown keys are dropped
# ---------------------------------------------------------------------------


def _to_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def test_web_inbound_drops_unknown_field() -> None:
    payload = {
        "chat_id": "c1",
        "sender_id": "u1",
        "sender_name": "alice",
        "text": "hi",
        "timestamp": "2026-05-20T00:00:00Z",
        "msg_id": "m1",
        "future_field": "ignored",
        "another_future": {"nested": 1},
    }
    out = WebInboundMessage.from_bytes(_to_bytes(payload))
    assert out.chat_id == "c1"
    assert out.text == "hi"
    # The filter must not invent any attribute for the dropped key.
    assert not hasattr(out, "future_field")


def test_web_stream_chunk_drops_unknown_field_and_uses_content_default() -> None:
    payload = {"type": "done", "future_flag": True}
    out = WebStreamChunk.from_bytes(_to_bytes(payload))
    assert out.type == "done"
    assert out.content == ""  # default kicks in, not stomped by filter


def test_web_typing_drops_unknown_field() -> None:
    payload = {"is_typing": True, "ttl_ms": 5000}
    out = WebTypingMessage.from_bytes(_to_bytes(payload))
    assert out.is_typing is True


def test_web_outbound_drops_unknown_field_and_uses_timestamp_default() -> None:
    payload = {"text": "hello", "future_field": 1}
    out = WebOutboundMessage.from_bytes(_to_bytes(payload))
    assert out.text == "hello"
    # __post_init__ stamps a timestamp when the field is empty; we
    # only assert it is a non-empty ISO-like string.
    assert isinstance(out.timestamp, str) and out.timestamp != ""


def test_agent_init_drops_unknown_field_at_top_level() -> None:
    payload = {
        "prompt": "do x",
        "group_folder": "/grp",
        "chat_jid": "jid",
        "tenant_id": "t1",
        "future_top_level": "ignore me",
    }
    out = AgentInitData.deserialize(_to_bytes(payload))
    assert out.prompt == "do x"
    assert out.tenant_id == "t1"


def test_agent_init_drops_unknown_field_inside_mcp_server_spec() -> None:
    payload = {
        "prompt": "p",
        "group_folder": "g",
        "chat_jid": "j",
        "mcp_servers": [
            {
                "name": "srv-a",
                "type": "sse",
                "url": "http://proxy/srv-a",
                # field introduced by a future orchestrator version
                "experimental_caps": ["batch"],
            }
        ],
    }
    out = AgentInitData.deserialize(_to_bytes(payload))
    assert out.mcp_servers is not None
    assert len(out.mcp_servers) == 1
    assert out.mcp_servers[0].name == "srv-a"
    assert out.mcp_servers[0].url == "http://proxy/srv-a"


def test_agent_init_falls_back_to_default_permissions_when_empty() -> None:
    # Pre-refactor semantics: an explicitly empty dict triggers the
    # default-role fallback, not the empty dict itself.
    payload = {
        "prompt": "p",
        "group_folder": "g",
        "chat_jid": "j",
        "permissions": {},
    }
    out = AgentInitData.deserialize(_to_bytes(payload))
    assert out.permissions["data_scope"] == "self"
    assert out.permissions["task_schedule"] is False


def test_agent_init_legacy_is_main_translates_to_permissions() -> None:
    payload = {
        "prompt": "p",
        "group_folder": "g",
        "chat_jid": "j",
        "is_main": True,
    }
    out = AgentInitData.deserialize(_to_bytes(payload))
    assert out.permissions["data_scope"] == "tenant"
    assert out.permissions["agent_delegate"] is True


# ---------------------------------------------------------------------------
# Required-field invariant: missing required field MUST raise.
# This is the half that fails if anyone silently fills defaults.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "complete_payload", "drop_key"),
    [
        (
            WebInboundMessage,
            {
                "chat_id": "c",
                "sender_id": "u",
                "sender_name": "n",
                "text": "t",
                "timestamp": "ts",
                "msg_id": "m",
            },
            "chat_id",
        ),
        (
            WebInboundMessage,
            {
                "chat_id": "c",
                "sender_id": "u",
                "sender_name": "n",
                "text": "t",
                "timestamp": "ts",
                "msg_id": "m",
            },
            "msg_id",
        ),
        (
            WebStreamChunk,
            {"type": "text", "content": "x"},
            "type",
        ),
        (
            WebTypingMessage,
            {"is_typing": True},
            "is_typing",
        ),
        (
            WebOutboundMessage,
            {"text": "x", "timestamp": "ts"},
            "text",
        ),
    ],
)
def test_missing_required_field_raises_keyerror(
    cls: type, complete_payload: dict, drop_key: str
) -> None:
    payload = dict(complete_payload)
    payload.pop(drop_key)
    with pytest.raises(KeyError) as excinfo:
        cls.from_bytes(_to_bytes(payload))
    # The KeyError carries the missing field name so logs are useful.
    assert drop_key in str(excinfo.value)


def test_agent_init_missing_required_field_raises_keyerror() -> None:
    # ``prompt`` / ``group_folder`` / ``chat_jid`` are the three
    # without defaults on AgentInitData; the others have defaults
    # and must not trigger this branch.
    payload = {"group_folder": "g", "chat_jid": "j"}
    with pytest.raises(KeyError) as excinfo:
        AgentInitData.deserialize(_to_bytes(payload))
    assert "prompt" in str(excinfo.value)


def test_mcp_server_spec_missing_required_field_raises_keyerror() -> None:
    # McpServerSpec requires (name, type, url); tool_reversibility has
    # a default_factory. Drop ``url`` from a nested spec and the
    # deserializer must surface a KeyError, not silently coerce to "".
    payload = {
        "prompt": "p",
        "group_folder": "g",
        "chat_jid": "j",
        "mcp_servers": [{"name": "srv", "type": "sse"}],
    }
    with pytest.raises(KeyError) as excinfo:
        AgentInitData.deserialize(_to_bytes(payload))
    assert "url" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Mixin-level direct contract: independent of any caller dataclass.
# ---------------------------------------------------------------------------


def test_mixin_filters_only_unknown_keys() -> None:
    out = from_dict_filter_unknown(
        McpServerSpec,
        {
            "name": "x",
            "type": "http",
            "url": "u",
            "tool_reversibility": {"a": True},
            "garbage": [1, 2],
        },
    )
    assert out.name == "x"
    assert out.tool_reversibility == {"a": True}


def test_mixin_default_factory_field_is_not_required() -> None:
    out = from_dict_filter_unknown(
        McpServerSpec, {"name": "x", "type": "http", "url": "u"}
    )
    assert out.tool_reversibility == {}


def test_mixin_raises_keyerror_with_field_name() -> None:
    with pytest.raises(KeyError) as excinfo:
        from_dict_filter_unknown(
            McpServerSpec, {"name": "x", "type": "http"}
        )
    assert excinfo.value.args == ("url",)
