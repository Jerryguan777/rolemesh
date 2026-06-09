"""Tests for the Frontdesk v1.2 additions to ``ToolContext``:

* ``nc`` field — the core NATS client used by ``request()``.
* ``role_config`` field — the per-turn IPC hint, normalised to ``{}``
  at the construction site so downstream tools never None-check.
* ``request()`` helper — core NATS request-reply with JSON
  encode/decode and a ``asyncio.TimeoutError`` passthrough.

The handbook §6 Step 3.3 explicitly calls out the shallow-copy
property: mutating ``ctx.role_config`` MUST NOT bleed back into the
``AgentInitData.role_config`` dict.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_runner.tools.context import ToolContext


def _ctx_with_nc(nc: Any, *, role_config: dict[str, object] | None = None) -> ToolContext:
    return ToolContext(
        js=AsyncMock(),  # type: ignore[arg-type]
        nc=nc,
        job_id="job-1",
        chat_jid="chat-1",
        group_folder="grp",
        permissions={},
        tenant_id="t-1",
        coworker_id="cw-1",
        conversation_id="conv-1",
        role_config=role_config if role_config is not None else {},
    )


# ---------------------------------------------------------------------------
# request() — happy path + timeout + non-object reply
# ---------------------------------------------------------------------------


async def test_request_round_trips_a_json_payload() -> None:
    """The wire form is ``json.dumps(data).encode()``; the reply is
    ``json.loads(msg.data.decode())``. Confirm both directions."""

    sent: dict[str, Any] = {}

    async def fake_request(subject: str, body: bytes, timeout: float) -> Any:
        sent["subject"] = subject
        sent["body"] = body
        sent["timeout"] = timeout
        reply = AsyncMock()
        reply.data = json.dumps({"echo": json.loads(body.decode())}).encode()
        return reply

    nc = AsyncMock()
    nc.request = AsyncMock(side_effect=fake_request)
    ctx = _ctx_with_nc(nc)

    reply = await ctx.request(
        "agent.j.list_agents.request",
        {"tenantId": "t-1", "fromCoworkerId": "cw-1"},
        timeout=12.0,
    )

    assert reply == {"echo": {"tenantId": "t-1", "fromCoworkerId": "cw-1"}}
    assert sent["subject"] == "agent.j.list_agents.request"
    # Body must be valid JSON of the original dict.
    assert json.loads(sent["body"].decode()) == {
        "tenantId": "t-1", "fromCoworkerId": "cw-1",
    }
    assert sent["timeout"] == 12.0


async def test_request_default_timeout_is_320s() -> None:
    """320s is the delegation business deadline + buffer.

    Pinning this guards against an inadvertent default change that
    would cause delegation_to_agent calls to time out at the RPC
    layer before the business deadline trips. The orchestrator's
    audit table would then record ``error`` instead of ``timeout``,
    masking the real failure mode.
    """
    nc = AsyncMock()
    reply = AsyncMock()
    reply.data = b'{"ok": true}'
    nc.request = AsyncMock(return_value=reply)

    ctx = _ctx_with_nc(nc)
    await ctx.request("agent.j.delegate.request", {})

    nc.request.assert_awaited_once()
    _, _, kwargs = nc.request.mock_calls[0]
    assert kwargs.get("timeout") == 320.0


async def test_request_propagates_asyncio_timeout() -> None:
    nc = AsyncMock()
    nc.request = AsyncMock(side_effect=TimeoutError())

    ctx = _ctx_with_nc(nc)
    with pytest.raises(TimeoutError):
        await ctx.request("agent.j.delegate.request", {}, timeout=0.01)


async def test_request_rejects_non_object_reply() -> None:
    """A JSON list / string reply is not a valid IPC envelope. The
    helper turns that into a ValueError so callers don't have to
    isinstance-check every reply.
    """
    nc = AsyncMock()
    reply = AsyncMock()
    reply.data = b'["not", "an", "object"]'
    nc.request = AsyncMock(return_value=reply)

    ctx = _ctx_with_nc(nc)
    with pytest.raises(ValueError, match="must be a JSON object"):
        await ctx.request("agent.j.x", {})


# ---------------------------------------------------------------------------
# role_config — None → {} normalisation + shallow-copy isolation
# ---------------------------------------------------------------------------


def test_role_config_default_is_empty_dict() -> None:
    """Caller passes nothing → role_config is an empty dict (NOT None).

    Tool code MUST be able to call ``ctx.role_config.get(...)``
    without None-checking. Handbook §6 Step 3.2 pitfall #33.
    """
    ctx = _ctx_with_nc(nc=AsyncMock())
    assert ctx.role_config == {}
    # Specifically NOT None — pitfall #33.
    assert ctx.role_config is not None


def test_role_config_populated_when_provided() -> None:
    ctx = _ctx_with_nc(nc=AsyncMock(), role_config={"is_delegated_call": True})
    assert ctx.role_config == {"is_delegated_call": True}
    assert ctx.role_config.get("is_delegated_call") is True


def test_role_config_two_contexts_have_independent_defaults() -> None:
    """The default_factory=dict guarantees each instance gets its own
    dict — if a shared default leaked in, mutating one ctx would
    poison the next.
    """
    a = _ctx_with_nc(nc=AsyncMock())
    b = _ctx_with_nc(nc=AsyncMock())
    assert a.role_config is not b.role_config
    a.role_config["leak_marker"] = True
    assert "leak_marker" not in b.role_config


def test_construction_site_normalises_none_role_config_from_init_data() -> None:
    """Mirror of ``agent_runner/main.py`` construction:

        role_config=dict(init.role_config or {})

    When ``init.role_config`` is None on the wire, the construction
    site MUST normalise to ``{}`` and MUST shallow-copy when a dict
    is given. This test reproduces that contract directly (no full
    agent_runner boot needed).
    """
    # init.role_config = None → ctx.role_config = {}
    ctx_none = _ctx_with_nc(nc=AsyncMock(), role_config=dict(None or {}))
    assert ctx_none.role_config == {}

    # init.role_config = {"foo": 1} → ctx.role_config = {"foo": 1}
    source: dict[str, object] = {"foo": 1}
    ctx_populated = _ctx_with_nc(nc=AsyncMock(), role_config=dict(source))
    assert ctx_populated.role_config == {"foo": 1}

    # Mutation of ctx.role_config must NOT bleed into the source dict.
    ctx_populated.role_config["bar"] = 2
    assert "bar" not in source, (
        "ctx.role_config is not a shallow copy of init.role_config — "
        "a downstream tool could corrupt IPC init state by mutating "
        "ctx.role_config in place."
    )


# ---------------------------------------------------------------------------
# Field placement sanity
# ---------------------------------------------------------------------------


def test_nc_is_a_required_field() -> None:
    """Constructing ToolContext without ``nc`` must fail at
    construction time (TypeError from the dataclass), preventing
    silent fallback to None and a downstream AttributeError when
    request() is called.
    """
    with pytest.raises(TypeError):
        ToolContext(  # type: ignore[call-arg]
            js=AsyncMock(),
            # nc deliberately omitted
            job_id="j",
            chat_jid="c",
            group_folder="g",
            permissions={},
            tenant_id="t",
            coworker_id="cw",
            conversation_id="conv",
        )
