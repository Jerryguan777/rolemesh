"""Per-user conversation ownership on the ``/api/v1`` surface.

Chat privacy is per-user: a conversation belongs to the member who
opened it (``conversations.user_id``), and every entrance must agree —
the per-coworker LIST filter, the id-addressed detail / messages /
approval-requests / DELETE handlers, and the WS-ticket mint. These
tests are deliberately adversarial in the same spirit as
``test_v1_visibility``: the valuable cases are the negative ones.

Bug-bait focus:

* One entrance forgotten — the boundary only exists if ALL entrances
  enforce it. The WS-ticket mint is the classic miss: every REST path
  404s but the ticket still signs, and the WS stream (which trusts the
  ticket payload) hands the foreign conversation over live. Each
  entrance gets its own assertion so a single regression names itself.
* 404 not 403 — "exists but not yours" must be indistinguishable from
  "does not exist" (no existence oracle).
* Admin escape — ``coworker.manage`` holders reach members' rows by id
  (moderation/cleanup) but their LIST is still just their own.
* total/items agreement — the count must apply the same owner +
  child-exclusion predicate as the page rows.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

# The deny path under test fires before signing, but the allow path
# (minting your OWN ticket) needs a secret to sign with.
os.environ.setdefault(
    "WS_TICKET_SECRET", "v1-ws-ticket-secret-only-for-tests"
)

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


def _authed(tenant_id: str, user_id: str, role: str = "member") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role=role,  # type: ignore[arg-type]
        email="x@x.com", name="X",
    )


async def _make_tenant_with_shared_coworker() -> tuple[str, str]:
    """Tenant + a SHARED coworker every member may use.

    Shared visibility is load-bearing: with the default (private,
    unattributed) coworker the ``_get_coworker_or_404`` gate would 404
    members before the ownership rule under test is ever reached.
    """
    t = await create_tenant(
        name="T-own", slug=f"own-{uuid.uuid4().hex[:8]}",
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name=f"Coworker-{uuid.uuid4().hex[:6]}",
        folder=f"f-{uuid.uuid4().hex[:8]}",
        agent_backend="claude",
        visibility="shared",
    )
    return t.id, cw.id


async def _make_member(tenant_id: str, role: str = "member") -> str:
    u = await create_user(
        tenant_id=tenant_id,
        name=f"U-{uuid.uuid4().hex[:6]}",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
        role=role,
    )
    return u.id


async def _post_conversation(
    tenant_id: str, user_id: str, coworker_id: str, *, role: str = "member",
    name: str = "chat",
) -> dict:
    app = _build_app(_authed(tenant_id, user_id, role))
    async with _client(app) as c:
        resp = await c.post(
            f"/api/v1/coworkers/{coworker_id}/conversations",
            json={"name": name},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# LIST — per-user filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shows_only_callers_own_conversations() -> None:
    """Two members × one shared coworker: each list contains only its owner's rows."""
    tid, cw_id = await _make_tenant_with_shared_coworker()
    a = await _make_member(tid)
    b = await _make_member(tid)
    conv_a = await _post_conversation(tid, a, cw_id, name="A's")
    conv_b = await _post_conversation(tid, b, cw_id, name="B's")

    app_a = _build_app(_authed(tid, a))
    async with _client(app_a) as c:
        page = (await c.get(f"/api/v1/coworkers/{cw_id}/conversations")).json()
    assert [x["id"] for x in page["items"]] == [conv_a["id"]]
    assert page["total"] == 1, "total must apply the same owner filter as items"

    app_b = _build_app(_authed(tid, b))
    async with _client(app_b) as c:
        page = (await c.get(f"/api/v1/coworkers/{cw_id}/conversations")).json()
    assert [x["id"] for x in page["items"]] == [conv_b["id"]]
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_admin_list_is_also_own_only() -> None:
    """``coworker.manage`` reaches rows BY ID, but the list stays personal."""
    tid, cw_id = await _make_tenant_with_shared_coworker()
    member = await _make_member(tid)
    admin = await _make_member(tid, role="admin")
    await _post_conversation(tid, member, cw_id, name="member's")

    app = _build_app(_authed(tid, admin, role="admin"))
    async with _client(app) as c:
        page = (await c.get(f"/api/v1/coworkers/{cw_id}/conversations")).json()
    assert page["items"] == [] and page["total"] == 0


@pytest.mark.asyncio
async def test_total_excludes_delegation_children() -> None:
    """A delegation child under the same coworker must not inflate total.

    The child even carries the SAME owner, proving the exclusion rides
    on ``parent_conversation_id IS NULL`` and not on ownership.
    """
    tid, cw_id = await _make_tenant_with_shared_coworker()
    a = await _make_member(tid)
    parent = await _post_conversation(tid, a, cw_id)
    await create_conversation(
        tenant_id=tid,
        coworker_id=cw_id,
        channel_binding_id=parent["channel_binding_id"],
        channel_chat_id=str(uuid.uuid4()),
        name="delegation child",
        user_id=a,
        parent_conversation_id=parent["id"],
    )

    app = _build_app(_authed(tid, a))
    async with _client(app) as c:
        page = (await c.get(f"/api/v1/coworkers/{cw_id}/conversations")).json()
    assert [x["id"] for x in page["items"]] == [parent["id"]]
    assert page["total"] == 1


# ---------------------------------------------------------------------------
# Id-addressed entrances — foreign conversation collapses to 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_reach_foreign_conversation_via_any_entrance() -> None:
    """GET / messages / approval-requests / DELETE all 404 for B on A's row."""
    tid, cw_id = await _make_tenant_with_shared_coworker()
    a = await _make_member(tid)
    b = await _make_member(tid)
    conv_id = (await _post_conversation(tid, a, cw_id))["id"]

    app_b = _build_app(_authed(tid, b))
    async with _client(app_b) as c:
        for method, path in (
            ("GET", f"/api/v1/conversations/{conv_id}"),
            ("GET", f"/api/v1/conversations/{conv_id}/messages"),
            ("GET", f"/api/v1/conversations/{conv_id}/approval-requests"),
            ("DELETE", f"/api/v1/conversations/{conv_id}"),
        ):
            resp = await c.request(method, path)
            assert resp.status_code == 404, f"{method} {path}: {resp.text}"
            assert resp.json()["code"] == "NOT_FOUND"

    # The attempted DELETE must not have removed A's row.
    app_a = _build_app(_authed(tid, a))
    async with _client(app_a) as c:
        assert (await c.get(f"/api/v1/conversations/{conv_id}")).status_code == 200


@pytest.mark.asyncio
async def test_admin_reaches_member_conversation_by_id() -> None:
    tid, cw_id = await _make_tenant_with_shared_coworker()
    member = await _make_member(tid)
    admin = await _make_member(tid, role="admin")
    conv_id = (await _post_conversation(tid, member, cw_id))["id"]

    app = _build_app(_authed(tid, admin, role="admin"))
    async with _client(app) as c:
        assert (await c.get(f"/api/v1/conversations/{conv_id}")).status_code == 200
        assert (
            await c.get(f"/api/v1/conversations/{conv_id}/messages")
        ).status_code == 200


@pytest.mark.asyncio
async def test_ownerless_conversation_member_404_admin_200() -> None:
    """``user_id IS NULL`` is nobody's: members 404, manage-holders reach it.

    Three-valued logic bait — a naive ``conv.user_id != caller → deny``
    inverted to ``is None → allow`` would quietly make unowned rows
    tenant-public.
    """
    tid, cw_id = await _make_tenant_with_shared_coworker()
    member = await _make_member(tid)
    owner = await _make_member(tid, role="owner")
    seeded = await _post_conversation(tid, member, cw_id)
    orphan = await create_conversation(
        tenant_id=tid,
        coworker_id=cw_id,
        channel_binding_id=seeded["channel_binding_id"],
        channel_chat_id=str(uuid.uuid4()),
        name="orphan",
        user_id=None,
    )

    app_m = _build_app(_authed(tid, member))
    async with _client(app_m) as c:
        assert (await c.get(f"/api/v1/conversations/{orphan.id}")).status_code == 404
        page = (await c.get(f"/api/v1/coworkers/{cw_id}/conversations")).json()
        assert orphan.id not in [x["id"] for x in page["items"]]

    app_o = _build_app(_authed(tid, owner, role="owner"))
    async with _client(app_o) as c:
        assert (await c.get(f"/api/v1/conversations/{orphan.id}")).status_code == 200


# ---------------------------------------------------------------------------
# WS-ticket mint — the non-REST entrance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_ticket_denied_for_foreign_conversation() -> None:
    """B must not mint a ticket for A's conversation; A still can.

    The handshake trusts the ticket payload without re-checking, so a
    mint-side miss silently reopens everything the REST 404s closed:
    live streaming AND sending into the foreign conversation.
    """
    tid, cw_id = await _make_tenant_with_shared_coworker()
    a = await _make_member(tid)
    b = await _make_member(tid)
    conv_id = (await _post_conversation(tid, a, cw_id))["id"]

    app_b = _build_app(_authed(tid, b))
    async with _client(app_b) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket", json={"conversation_id": conv_id}
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"

    app_a = _build_app(_authed(tid, a))
    async with _client(app_a) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket", json={"conversation_id": conv_id}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ticket"]
