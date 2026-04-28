"""Tests for the WebUI admin → MCP-broadcast publisher.

PR-2 of the MCP-registry sync. PR-1 wired the gateway to listen for
``egress.mcp.changed`` events; PR-2 wires the WebUI admin endpoints
to *emit* those events when an operator edits a coworker's tools.

These tests exercise ``_publish_mcp_for_coworker`` directly rather
than spinning up a FastAPI test client — the helper carries the logic
that matters (origin computation, one-event-per-tool, no-op when
publisher unset). The route handlers are 1-line wrappers that just
forward the result of ``pg.create_coworker`` / ``pg.update_coworker``
through the helper, so the FastAPI integration adds runtime cost
without finding a different class of bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from rolemesh.core.types import Coworker, McpServerConfig
from rolemesh.egress.mcp_cache import MCP_CHANGED_SUBJECT
from webui import admin as admin_mod


@dataclass
class _Captured:
    subject: str
    body: bytes


class _FakeNats:
    def __init__(self) -> None:
        self.published: list[_Captured] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append(_Captured(subject, data))


def _coworker_with_tools(tools: list[McpServerConfig]) -> Coworker:
    return Coworker(
        id="cw-id",
        tenant_id="tenant-id",
        name="bot",
        folder="bot-folder",
        agent_backend="claude-code",
        system_prompt=None,
        tools=tools,
        container_config=None,
        max_concurrent=2,
        status="active",
        created_at="",
        agent_role="agent",
        permissions=None,
    )


@pytest.fixture(autouse=True)
def _reset_publisher() -> None:
    admin_mod.set_mcp_publisher(None)


# ---------------------------------------------------------------------------
# No-op when publisher unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_is_noop_without_publisher() -> None:
    # Default fixture state: ``_mcp_publisher`` is None. Function must
    # return without raising / without trying to call into a missing
    # NATS handle. Lets dev/test stand the WebUI up without NATS for
    # admin smoke tests.
    cw = _coworker_with_tools(
        [
            McpServerConfig(
                name="github", type="http", url="https://api.github.com"
            )
        ]
    )
    await admin_mod._publish_mcp_for_coworker("created", cw)
    # No exception. Nothing else to assert — no nats handle to inspect.


# ---------------------------------------------------------------------------
# One event per tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_emits_one_event_per_tool() -> None:
    nc = _FakeNats()
    admin_mod.set_mcp_publisher(nc)

    cw = _coworker_with_tools(
        [
            McpServerConfig(
                name="github",
                type="http",
                url="https://api.github.com/mcp",
                auth_mode="user",
            ),
            McpServerConfig(
                name="internal",
                type="sse",
                url="http://localhost:9100/mcp/",
                headers={"X-Tenant": "t1"},
                auth_mode="service",
            ),
        ]
    )
    await admin_mod._publish_mcp_for_coworker("updated", cw)

    assert len(nc.published) == 2
    subjects = {p.subject for p in nc.published}
    assert subjects == {MCP_CHANGED_SUBJECT}


@pytest.mark.asyncio
async def test_publish_strips_path_to_origin() -> None:
    """Tool URL ``https://api.github.com/mcp`` becomes
    ``https://api.github.com``. The gateway's ``_mcp_registry`` keys on
    origin and rewrites paths client-side; passing the full URL would
    cause the proxy to issue requests against the wrong path."""
    nc = _FakeNats()
    admin_mod.set_mcp_publisher(nc)

    cw = _coworker_with_tools(
        [
            McpServerConfig(
                name="x",
                type="http",
                url="https://api.example.com/some/long/path?q=1",
            )
        ]
    )
    await admin_mod._publish_mcp_for_coworker("updated", cw)

    payload = json.loads(nc.published[0].body)
    assert payload["url"] == "https://api.example.com"
    assert payload["name"] == "x"
    assert payload["action"] == "updated"


@pytest.mark.asyncio
async def test_publish_action_propagates() -> None:
    nc = _FakeNats()
    admin_mod.set_mcp_publisher(nc)

    cw = _coworker_with_tools(
        [McpServerConfig(name="x", type="http", url="https://x.example")]
    )
    await admin_mod._publish_mcp_for_coworker("created", cw)
    await admin_mod._publish_mcp_for_coworker("updated", cw)

    actions = [json.loads(p.body)["action"] for p in nc.published]
    assert actions == ["created", "updated"]


@pytest.mark.asyncio
async def test_publish_serialises_headers_and_auth_mode() -> None:
    nc = _FakeNats()
    admin_mod.set_mcp_publisher(nc)

    cw = _coworker_with_tools(
        [
            McpServerConfig(
                name="x",
                type="http",
                url="https://x.example",
                headers={"X-A": "1", "X-B": "2"},
                auth_mode="service",
            )
        ]
    )
    await admin_mod._publish_mcp_for_coworker("created", cw)

    payload = json.loads(nc.published[0].body)
    assert payload["headers"] == {"X-A": "1", "X-B": "2"}
    assert payload["auth_mode"] == "service"


@pytest.mark.asyncio
async def test_publish_with_zero_tools_emits_nothing() -> None:
    nc = _FakeNats()
    admin_mod.set_mcp_publisher(nc)
    cw = _coworker_with_tools([])
    await admin_mod._publish_mcp_for_coworker("updated", cw)
    assert nc.published == []


@pytest.mark.asyncio
async def test_publish_swallows_nats_errors() -> None:
    # Best-effort contract from the helper docstring — admin REST
    # response should NOT 500 because the broadcast couldn't go out.
    class _Boom:
        async def publish(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("nats down")

    admin_mod.set_mcp_publisher(_Boom())
    cw = _coworker_with_tools(
        [McpServerConfig(name="x", type="http", url="https://x.example")]
    )
    # Must not raise.
    await admin_mod._publish_mcp_for_coworker("updated", cw)


# ---------------------------------------------------------------------------
# Bug 5 (2026-04-26): publish must rewrite localhost; in-process
# register must NOT (orchestrator's rollback proxy still needs to
# dial the host's loopback)
# ---------------------------------------------------------------------------


class TestLoopbackRewriteAtPublishBoundary:
    @pytest.mark.asyncio
    async def test_localhost_url_is_rewritten_in_published_event(self) -> None:
        nc = _FakeNats()
        admin_mod.set_mcp_publisher(nc)

        cw = _coworker_with_tools(
            [
                McpServerConfig(
                    name="tropos-mcp",
                    type="http",
                    url="https://localhost:8509/mcp",
                )
            ]
        )
        await admin_mod._publish_mcp_for_coworker("updated", cw)

        payload = json.loads(nc.published[0].body)
        # The exact regression that caused Bug 5: gateway-bound event
        # must carry host.docker.internal, not the literal localhost.
        assert payload["url"] == "https://host.docker.internal:8509"

    @pytest.mark.asyncio
    async def test_127_0_0_1_url_is_rewritten_in_published_event(self) -> None:
        nc = _FakeNats()
        admin_mod.set_mcp_publisher(nc)

        cw = _coworker_with_tools(
            [
                McpServerConfig(
                    name="local-mcp",
                    type="http",
                    url="http://127.0.0.1:9100/mcp/",
                )
            ]
        )
        await admin_mod._publish_mcp_for_coworker("updated", cw)
        payload = json.loads(nc.published[0].body)
        assert payload["url"] == "http://host.docker.internal:9100"

    @pytest.mark.asyncio
    async def test_external_host_url_is_not_rewritten(self) -> None:
        # Negative case: only loopback gets rewritten. An operator
        # pointing at a remote MCP cluster sees their hostname intact.
        nc = _FakeNats()
        admin_mod.set_mcp_publisher(nc)

        cw = _coworker_with_tools(
            [
                McpServerConfig(
                    name="github",
                    type="http",
                    url="https://api.github.com/mcp",
                )
            ]
        )
        await admin_mod._publish_mcp_for_coworker("updated", cw)
        payload = json.loads(nc.published[0].body)
        assert payload["url"] == "https://api.github.com"
