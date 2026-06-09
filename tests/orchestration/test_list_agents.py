"""Integration tests for the ``list_agents`` core-NATS responder.

Drives ``handle_list_agents_request`` (the orchestrator-side handler)
end-to-end with a stub NATS message so the JSON wire shape, the
filter set, and the failure paths are all exercised together.

The same FakeNats / FakeMsg pattern is used elsewhere in the suite
(see tests/egress/test_mcp_glue.py).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.core.types import Coworker
from rolemesh.orchestration.catalog import handle_list_agents_request


@dataclass
class FakeMsg:
    """Duck-type the NATS Msg surface ``msg.data`` + ``msg.respond``."""

    data: bytes

    def __post_init__(self) -> None:
        self.replies: list[bytes] = []

    async def respond(self, body: bytes) -> None:
        self.replies.append(body)


def _cw(**kw: object) -> Coworker:
    defaults: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "tenant_id": kw.pop("tenant_id"),
        "name": "Coworker",
        "folder": "coworker",
    }
    defaults.update(kw)
    return Coworker(**defaults)  # type: ignore[arg-type]


def _state_with(*cws: Coworker) -> OrchestratorState:
    state = OrchestratorState()
    for cw in cws:
        state.coworkers[cw.id] = CoworkerState.from_coworker(cw)
    return state


async def test_responder_returns_catalog_for_caller_tenant() -> None:
    tenant = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant,
        name="Frontdesk",
        folder="frontdesk",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    tr = _cw(
        tenant_id=tenant,
        name="Trading",
        folder="trading",
        routing_description="Trading ops.",
    )
    state = _state_with(fd, tr)

    msg = FakeMsg(
        data=json.dumps(
            {"tenantId": tenant, "fromCoworkerId": fd.id}
        ).encode()
    )
    await handle_list_agents_request(msg, state=state)

    assert len(msg.replies) == 1
    payload = json.loads(msg.replies[0])
    assert "error" not in payload
    assert "Trading" in payload["text"]
    assert "(id: trading)" in payload["text"]


async def test_responder_filters_paused_cross_tenant_frontdesk_and_self() -> None:
    tenant = str(uuid.uuid4())
    other = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant,
        name="Frontdesk",
        folder="frontdesk",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    paused = _cw(
        tenant_id=tenant,
        name="PausedAgent",
        folder="paused",
        status="paused",
    )
    other_tenant_agent = _cw(
        tenant_id=other,
        name="Cross",
        folder="cross",
    )
    other_fd = _cw(
        tenant_id=tenant,
        name="OtherFrontdesk",
        folder="other-fd",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    tr = _cw(
        tenant_id=tenant,
        name="Trading",
        folder="trading",
    )
    state = _state_with(fd, paused, other_tenant_agent, other_fd, tr)

    msg = FakeMsg(
        data=json.dumps(
            {"tenantId": tenant, "fromCoworkerId": fd.id}
        ).encode()
    )
    await handle_list_agents_request(msg, state=state)

    text = json.loads(msg.replies[0])["text"]
    assert "Trading" in text
    assert "PausedAgent" not in text
    assert "Cross" not in text
    assert "OtherFrontdesk" not in text
    assert "Frontdesk" not in text


async def test_responder_returns_empty_directive_when_no_specialists() -> None:
    tenant = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant,
        name="Frontdesk",
        folder="frontdesk",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    state = _state_with(fd)
    msg = FakeMsg(
        data=json.dumps(
            {"tenantId": tenant, "fromCoworkerId": fd.id}
        ).encode()
    )
    await handle_list_agents_request(msg, state=state)
    payload = json.loads(msg.replies[0])
    assert payload["text"] == (
        "No specialists available. Answer the user directly."
    )


async def test_responder_excludes_caller_even_when_caller_is_an_agent() -> None:
    """A future PR could relax the ``is_frontdesk`` filter so non-frontdesk
    agents call list_agents too. ``exclude`` is the invariant that
    keeps a coworker out of its own catalog regardless of the role
    filter — pin it explicitly so a relaxation doesn't accidentally
    let an agent see itself in its own delegation roster."""
    tenant = str(uuid.uuid4())
    me = _cw(tenant_id=tenant, name="Me", folder="me")
    other = _cw(tenant_id=tenant, name="Other", folder="other")
    state = _state_with(me, other)

    msg = FakeMsg(
        data=json.dumps(
            {"tenantId": tenant, "fromCoworkerId": me.id}
        ).encode()
    )
    await handle_list_agents_request(msg, state=state)

    text = json.loads(msg.replies[0])["text"]
    assert "Other" in text
    assert "Me" not in text


async def test_responder_replies_with_error_on_malformed_payload() -> None:
    """Malformed request: handler must NOT raise — the agent runner's
    ctx.request would otherwise pop a timeout to the LLM which gives
    no diagnostic clue. We require an explicit reply with an
    ``error`` field so the tool surface can surface it intelligibly.
    """
    state = _state_with()
    msg = FakeMsg(data=b"this is not valid json")
    await handle_list_agents_request(msg, state=state)
    assert len(msg.replies) == 1
    payload = json.loads(msg.replies[0])
    assert payload["text"] == ""
    assert "error" in payload


async def test_responder_replies_with_error_on_missing_tenant_field() -> None:
    state = _state_with()
    msg = FakeMsg(data=json.dumps({"fromCoworkerId": "anything"}).encode())
    await handle_list_agents_request(msg, state=state)
    payload = json.loads(msg.replies[0])
    assert payload["text"] == ""
    assert "error" in payload
