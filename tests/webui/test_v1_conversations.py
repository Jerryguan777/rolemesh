"""Integration tests for the ``/api/v1`` conversations + messages surface.

Real Postgres testcontainer (no DB mock). Each test seeds its own
tenant / user / coworker so cross-tenant isolation can be exercised
without polluting another test's fixture state.

Bug-bait focus:

* Tenant isolation — a conversation owned by tenant A must not be
  visible / deletable from a tenant-B session even when the
  attacker knows the UUID. INV-1 (RLS + explicit predicate) is
  the load-bearing invariant here; the test asserts the wire
  behaviour rather than the SQL shape so a regression in either
  layer surfaces.
* DELETE cascade — `messages` rows hanging off a conversation
  must vanish when the conversation is removed (FK ON DELETE
  CASCADE). A missing cascade would leak past-tenant data after
  re-using the conversation UUID space.
* Message role projection — `is_from_me=True` rows must surface as
  `assistant`; `is_bot_message=True` (legacy bot post) likewise.
  Mirror tests would just re-state the SQL CASE; we instead seed
  rows with the two FALSE/TRUE combinations and assert the wire
  enum.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_approval_request,
    create_coworker,
    create_tenant,
    create_user,
    resolve_approval_request,
    store_message,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


def _authed(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role="owner",
        email="x@x.com", name="X",
    )


async def _make_tenant_user_coworker(
    slug_prefix: str = "v1conv",
) -> tuple[str, str, str]:
    t = await create_tenant(
        name=f"T-{slug_prefix}",
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id,
        name="Owner",
        email=f"owner-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name=f"Coworker-{slug_prefix}",
        folder=f"f-{uuid.uuid4().hex[:8]}",
        agent_backend="claude",
    )
    return t.id, u.id, cw.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_then_list_conversation() -> None:
    """POST then GET round-trips the conversation row through the v1 surface."""
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        # Create
        resp = await c.post(
            f"/api/v1/coworkers/{cw_id}/conversations",
            json={"name": "First chat"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["coworker_id"] == cw_id
        assert body["name"] == "First chat"
        # Server-generated channel_chat_id must be a UUID
        uuid.UUID(body["channel_chat_id"])
        conv_id = body["id"]

        # List
        resp = await c.get(f"/api/v1/coworkers/{cw_id}/conversations")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == conv_id

        # GET single
        resp = await c.get(f"/api/v1/conversations/{conv_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == conv_id


@pytest.mark.asyncio
async def test_create_conversation_reuses_existing_web_binding() -> None:
    """Two POSTs in a row must share the auto-provisioned binding.

    The handler is idempotent on the binding side — creating a
    second conversation should not spawn a second binding row.
    """
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r1 = await c.post(
            f"/api/v1/coworkers/{cw_id}/conversations", json={}
        )
        r2 = await c.post(
            f"/api/v1/coworkers/{cw_id}/conversations", json={}
        )
    assert r1.status_code == 201 and r2.status_code == 201
    b1 = r1.json()["channel_binding_id"]
    b2 = r2.json()["channel_binding_id"]
    assert b1 == b2, "expected the auto-created web binding to be reused"
    # Distinct chat_ids though
    assert r1.json()["channel_chat_id"] != r2.json()["channel_chat_id"]


# ---------------------------------------------------------------------------
# Tenant isolation (INV-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_b_cannot_read_tenant_a_conversation() -> None:
    """Cross-tenant GET surfaces 404, not the row."""
    tid_a, uid_a, cw_a = await _make_tenant_user_coworker("ta")
    tid_b, uid_b, _ = await _make_tenant_user_coworker("tb")
    app_a = _build_app(_authed(tid_a, uid_a))
    app_b = _build_app(_authed(tid_b, uid_b))

    async with _client(app_a) as ca:
        r = await ca.post(
            f"/api/v1/coworkers/{cw_a}/conversations", json={"name": "A"}
        )
        assert r.status_code == 201
        conv_id = r.json()["id"]

    async with _client(app_b) as cb:
        r = await cb.get(f"/api/v1/conversations/{conv_id}")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_tenant_b_cannot_delete_tenant_a_conversation() -> None:
    """Cross-tenant DELETE must 404, not silently succeed.

    A silent success on the wrong tenant's connection would still
    look like 204 but leave the row alone — RLS prevents the
    DELETE but the handler must surface the "not yours" outcome
    explicitly. The post-condition GET (from the rightful tenant)
    proves the row is still present.
    """
    tid_a, uid_a, cw_a = await _make_tenant_user_coworker("ta2")
    tid_b, uid_b, _ = await _make_tenant_user_coworker("tb2")
    app_a = _build_app(_authed(tid_a, uid_a))
    app_b = _build_app(_authed(tid_b, uid_b))

    async with _client(app_a) as ca:
        r = await ca.post(
            f"/api/v1/coworkers/{cw_a}/conversations", json={"name": "A"}
        )
        conv_id = r.json()["id"]

    async with _client(app_b) as cb:
        r = await cb.delete(f"/api/v1/conversations/{conv_id}")
    assert r.status_code == 404, "tenant B must NOT be able to delete"

    async with _client(app_a) as ca:
        r = await ca.get(f"/api/v1/conversations/{conv_id}")
    assert r.status_code == 200, "row must still exist in tenant A"


# ---------------------------------------------------------------------------
# DELETE cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_conversation_cascades_messages() -> None:
    """Messages hanging off a conversation must vanish on DELETE.

    The FK on ``messages.conversation_id`` is ON DELETE CASCADE in
    the schema; the wire-level assertion proves the cascade is
    intact (no orphaned rows visible afterwards).
    """
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(
            f"/api/v1/coworkers/{cw_id}/conversations", json={}
        )
        conv_id = r.json()["id"]

    ts = datetime.now(UTC).isoformat()
    await store_message(
        tenant_id=tid,
        conversation_id=conv_id,
        msg_id="m-1",
        sender="user",
        sender_name="User",
        content="hi",
        timestamp=ts,
        is_from_me=False,
    )
    await store_message(
        tenant_id=tid,
        conversation_id=conv_id,
        msg_id="m-2",
        sender="bot",
        sender_name="Bot",
        content="hello",
        timestamp=ts,
        is_from_me=True,
    )

    # Pre-delete: messages visible via wire
    async with _client(app) as c:
        r = await c.get(f"/api/v1/conversations/{conv_id}/messages")
    assert r.status_code == 200
    msgs = r.json()["items"]
    assert len(msgs) == 2
    roles = {m["role"] for m in msgs}
    assert roles == {"user", "assistant"}, msgs

    # Delete
    async with _client(app) as c:
        r = await c.delete(f"/api/v1/conversations/{conv_id}")
    assert r.status_code == 204

    # Post-delete: GET 404, messages gone
    async with _client(app) as c:
        r = await c.get(f"/api/v1/conversations/{conv_id}")
        assert r.status_code == 404
        r = await c.get(f"/api/v1/conversations/{conv_id}/messages")
        assert r.status_code == 404, "messages endpoint must 404 on missing conv"


@pytest.mark.asyncio
async def test_bot_message_with_only_is_bot_message_surfaces_as_assistant() -> None:
    """Legacy bot post with ``is_bot_message=True`` but ``is_from_me=False``.

    Forces the CASE projection — without ``is_bot_message`` in the
    projection a legacy bot reply would surface as ``user``, which
    the SPA would render with the wrong bubble.
    """
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(
            f"/api/v1/coworkers/{cw_id}/conversations", json={}
        )
        conv_id = r.json()["id"]
    await store_message(
        tenant_id=tid,
        conversation_id=conv_id,
        msg_id="legacy-bot",
        sender="legacy-bot",
        sender_name="Bot",
        content="legacy reply",
        timestamp=datetime.now(UTC).isoformat(),
        is_from_me=False,
        is_bot_message=True,
    )
    async with _client(app) as c:
        r = await c.get(f"/api/v1/conversations/{conv_id}/messages")
    assert r.status_code == 200
    msgs = r.json()["items"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_get_conversation_with_invalid_uuid_returns_404_not_500() -> None:
    """A non-UUID string for ``conversation_id`` must not bubble asyncpg's DataError.

    The handler catches the ``DataError`` so callers cannot
    distinguish "bad UUID syntax" from "valid UUID not found" —
    leaking the parser hint would be an information disclosure.
    """
    tid, uid, _ = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.get("/api/v1/conversations/not-a-uuid")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Conversation approval record — GET /conversations/{id}/approval-requests
#
# This sub-resource is the chat surface's source of truth for re-rendering the
# full approval history inline (pending + resolved), so a reload doesn't drop
# the resolved ✅/❌ cards. Bug-bait: it must (a) include resolved rows, not
# just pending; (b) order by requested_at (where the card belongs) not
# decided_at; (c) scope to the one conversation; (d) hold the tenant edge.
# ---------------------------------------------------------------------------


async def _seed_request(
    *, tenant_id: str, coworker_id: str, conversation_id: str, summary: str
) -> str:
    req = await create_approval_request(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        conversation_id=conversation_id,
        job_id="job-x",
        mcp_server_name="stripe",
        action={"tool_name": "charge", "params": {"amount": 500}},
        action_summary=summary,
        rationale="why",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return req.id


@pytest.mark.asyncio
async def test_conversation_approvals_include_resolved_oldest_first() -> None:
    """Pending AND resolved rows come back, oldest-first by requested_at.

    The pending-only inbox read would drop the resolved card on reload — the
    whole reason this endpoint exists. We seed two requests, resolve the
    *first*, and assert both surface with their real status in request order.
    """
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(f"/api/v1/coworkers/{cw_id}/conversations", json={})
        conv_id = r.json()["id"]

    first = await _seed_request(
        tenant_id=tid, coworker_id=cw_id, conversation_id=conv_id, summary="first"
    )
    second = await _seed_request(
        tenant_id=tid, coworker_id=cw_id, conversation_id=conv_id, summary="second"
    )
    resolved = await resolve_approval_request(
        first, tenant_id=tid, status="approved", decided_by=uid
    )
    assert resolved is not None, "seed precondition: first request must resolve"

    async with _client(app) as c:
        r = await c.get(f"/api/v1/conversations/{conv_id}/approval-requests")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["request_id"] for x in rows] == [first, second], "oldest-first"
    assert rows[0]["status"] == "approved"
    assert rows[0]["decided_at"] is not None
    assert rows[1]["status"] == "pending"
    assert rows[1]["decided_at"] is None


@pytest.mark.asyncio
async def test_conversation_approvals_ordered_by_request_not_decision_time() -> None:
    """A request raised first but decided last still sorts first.

    The card belongs where the agent raised it (right after the user turn),
    not where the human happened to click — so ordering keys on requested_at.
    Resolving the *older* request last (so decided_at order is the reverse of
    requested_at order) would expose an accidental ORDER BY decided_at.
    """
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(f"/api/v1/coworkers/{cw_id}/conversations", json={})
        conv_id = r.json()["id"]

    older = await _seed_request(
        tenant_id=tid, coworker_id=cw_id, conversation_id=conv_id, summary="older"
    )
    newer = await _seed_request(
        tenant_id=tid, coworker_id=cw_id, conversation_id=conv_id, summary="newer"
    )
    # Decide the NEWER one first, the OLDER one second — decided_at order is
    # now the opposite of requested_at order.
    await resolve_approval_request(newer, tenant_id=tid, status="approved")
    await resolve_approval_request(older, tenant_id=tid, status="rejected")

    async with _client(app) as c:
        r = await c.get(f"/api/v1/conversations/{conv_id}/approval-requests")
    assert r.status_code == 200, r.text
    assert [x["request_id"] for x in r.json()] == [older, newer]


@pytest.mark.asyncio
async def test_conversation_approvals_scoped_to_the_one_conversation() -> None:
    """A request on a *sibling* conversation (same tenant) is not returned."""
    tid, uid, cw_id = await _make_tenant_user_coworker()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        a = await c.post(f"/api/v1/coworkers/{cw_id}/conversations", json={})
        conv_a = a.json()["id"]
        b = await c.post(f"/api/v1/coworkers/{cw_id}/conversations", json={})
        conv_b = b.json()["id"]

    mine = await _seed_request(
        tenant_id=tid, coworker_id=cw_id, conversation_id=conv_a, summary="mine"
    )
    await _seed_request(
        tenant_id=tid, coworker_id=cw_id, conversation_id=conv_b, summary="sibling"
    )

    async with _client(app) as c:
        r = await c.get(f"/api/v1/conversations/{conv_a}/approval-requests")
    assert r.status_code == 200, r.text
    assert [x["request_id"] for x in r.json()] == [mine]


@pytest.mark.asyncio
async def test_conversation_approvals_cross_tenant_is_404() -> None:
    """Tenant A asking for tenant B's conversation gets a flat 404.

    Same isolation contract as the conversation GET: a guessed/leaked UUID
    from another tenant must collapse to the same 404 a non-existent id gets
    — no read of B's approvals, no existence oracle.
    """
    a_tid, a_uid, _ = await _make_tenant_user_coworker("convapr-a")
    b_tid, _b_uid, b_cw = await _make_tenant_user_coworker("convapr-b")
    b_app = _build_app(_authed(b_tid, _b_uid))
    async with _client(b_app) as c:
        r = await c.post(f"/api/v1/coworkers/{b_cw}/conversations", json={})
        b_conv = r.json()["id"]
    await _seed_request(
        tenant_id=b_tid, coworker_id=b_cw, conversation_id=b_conv, summary="victim"
    )

    a_app = _build_app(_authed(a_tid, a_uid))
    async with _client(a_app) as c:
        r = await c.get(f"/api/v1/conversations/{b_conv}/approval-requests")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_messages_cursor_pagination_walks_older() -> None:
    """Cursor paging returns the newest page first, then walks older via
    next_cursor. Items come back oldest-first (display order); the cursor
    is opaque and seeks on (timestamp, id)."""
    tid, uid, cw_id = await _make_tenant_user_coworker("curs")
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        conv_id = (
            await c.post(f"/api/v1/coworkers/{cw_id}/conversations", json={})
        ).json()["id"]

    base = datetime.now(UTC)
    for i in range(3):  # m0 < m1 < m2 in time
        await store_message(
            tenant_id=tid,
            conversation_id=conv_id,
            msg_id=f"m-{i}",
            sender="user",
            sender_name="U",
            content=f"msg{i}",
            timestamp=(base + timedelta(seconds=i)).isoformat(),
            is_from_me=False,
        )

    async with _client(app) as c:
        # Page 1: newest 2 (m1, m2), oldest-first; more remain.
        p1 = (
            await c.get(f"/api/v1/conversations/{conv_id}/messages?limit=2")
        ).json()
        assert [m["content"] for m in p1["items"]] == ["msg1", "msg2"]
        assert p1["has_more"] is True
        assert p1["next_cursor"]
        # Page 2: the one older message (m0); no more.
        p2 = (
            await c.get(
                f"/api/v1/conversations/{conv_id}/messages"
                f"?limit=2&before={p1['next_cursor']}"
            )
        ).json()
        assert [m["content"] for m in p2["items"]] == ["msg0"]
        assert p2["has_more"] is False
        assert p2["next_cursor"] is None


@pytest.mark.asyncio
async def test_messages_malformed_cursor_is_400() -> None:
    tid, uid, cw_id = await _make_tenant_user_coworker("badc")
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        conv_id = (
            await c.post(f"/api/v1/coworkers/{cw_id}/conversations", json={})
        ).json()["id"]
        resp = await c.get(
            f"/api/v1/conversations/{conv_id}/messages?before=not-a-cursor"
        )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_CURSOR"
