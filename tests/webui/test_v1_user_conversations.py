"""``GET /api/v1/conversations`` — the unified per-user chat history.

One list, every coworker's conversations merged, newest first, owned
by the caller. Complements ``test_v1_conversation_ownership`` (which
proves the boundary on the id-addressed entrances); here the bug-bait
is the LIST semantics themselves:

* Merge — rows from MULTIPLE coworkers appear in one page, each
  carrying its ``coworker_id`` (the client's badge/routing key).
* Order — newest-first. The per-coworker endpoint is oldest-first; a
  copy-paste of its ORDER BY would pass every other assertion.
* Filter — ownership only: another member's rows, delegation children,
  and ownerless (``user_id IS NULL``) rows never appear, but the
  caller's rows survive the bound coworker turning private (ownership
  is the filter, not coworker visibility).
* Envelope — ``total`` applies the exact list predicate, and
  offset/limit walks the full set without gaps or duplicates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    tenant_conn,
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


def _authed(tenant_id: str, user_id: str, role: str = "member") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role=role,  # type: ignore[arg-type]
        email="x@x.com", name="X",
    )


async def _make_tenant() -> str:
    t = await create_tenant(name="T-uni", slug=f"uni-{uuid.uuid4().hex[:8]}")
    return t.id


async def _make_member(tenant_id: str, role: str = "member") -> str:
    u = await create_user(
        tenant_id=tenant_id,
        name=f"U-{uuid.uuid4().hex[:6]}",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
        role=role,
    )
    return u.id


async def _make_shared_coworker(tenant_id: str) -> str:
    cw = await create_coworker(
        tenant_id=tenant_id,
        name=f"CW-{uuid.uuid4().hex[:6]}",
        folder=f"f-{uuid.uuid4().hex[:8]}",
        agent_backend="claude",
        visibility="shared",
    )
    return cw.id


async def _post_conversation(
    tenant_id: str, user_id: str, coworker_id: str, *, name: str,
) -> dict:
    app = _build_app(_authed(tenant_id, user_id))
    async with _client(app) as c:
        resp = await c.post(
            f"/api/v1/coworkers/{coworker_id}/conversations",
            json={"name": name},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _stamp_created_at(
    tenant_id: str, conv_id: str, created_at: datetime
) -> None:
    """Pin created_at so ordering assertions can't flake on same-µs inserts."""
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            "UPDATE conversations SET created_at = $1 "
            "WHERE id = $2::uuid AND tenant_id = $3::uuid",
            created_at,
            conv_id,
            tenant_id,
        )


@pytest.mark.asyncio
async def test_merges_coworkers_newest_first_with_coworker_ids() -> None:
    tid = await _make_tenant()
    uid = await _make_member(tid)
    cw1 = await _make_shared_coworker(tid)
    cw2 = await _make_shared_coworker(tid)

    base = datetime.now(UTC)
    conv_old = await _post_conversation(tid, uid, cw1, name="old")
    conv_mid = await _post_conversation(tid, uid, cw2, name="mid")
    conv_new = await _post_conversation(tid, uid, cw1, name="new")
    for i, conv in enumerate((conv_old, conv_mid, conv_new)):
        await _stamp_created_at(tid, conv["id"], base + timedelta(seconds=i))

    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        page = (await c.get("/api/v1/conversations")).json()

    assert [x["id"] for x in page["items"]] == [
        conv_new["id"], conv_mid["id"], conv_old["id"],
    ], "expected newest-first across coworkers"
    assert [x["coworker_id"] for x in page["items"]] == [cw1, cw2, cw1]
    assert page["total"] == 3


@pytest.mark.asyncio
async def test_excludes_foreign_children_and_ownerless_rows() -> None:
    """Only the caller's own top-level rows; total agrees with items.

    Seeds one of each excluded kind — another member's conversation, a
    delegation child OWNED BY THE CALLER (so the exclusion provably
    rides on ``parent_conversation_id``), and an ownerless row — plus
    exactly one legitimate row.
    """
    tid = await _make_tenant()
    a = await _make_member(tid)
    b = await _make_member(tid)
    cw = await _make_shared_coworker(tid)

    mine = await _post_conversation(tid, a, cw, name="mine")
    await _post_conversation(tid, b, cw, name="b's")
    await create_conversation(
        tenant_id=tid,
        coworker_id=cw,
        channel_binding_id=mine["channel_binding_id"],
        channel_chat_id=str(uuid.uuid4()),
        name="delegation child",
        user_id=a,
        parent_conversation_id=mine["id"],
    )
    await create_conversation(
        tenant_id=tid,
        coworker_id=cw,
        channel_binding_id=mine["channel_binding_id"],
        channel_chat_id=str(uuid.uuid4()),
        name="ownerless",
        user_id=None,
    )

    app = _build_app(_authed(tid, a))
    async with _client(app) as c:
        page = (await c.get("/api/v1/conversations")).json()
    assert [x["id"] for x in page["items"]] == [mine["id"]]
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_owned_row_survives_coworker_turning_private() -> None:
    """Ownership is the filter — NOT coworker visibility.

    A coworker-visibility JOIN (the per-coworker endpoint's gate)
    would silently drop the caller's own history the moment an admin
    unshares the coworker.
    """
    tid = await _make_tenant()
    uid = await _make_member(tid)
    cw = await _make_shared_coworker(tid)
    conv = await _post_conversation(tid, uid, cw, name="mine")

    async with tenant_conn(tid) as conn:
        await conn.execute(
            "UPDATE coworkers SET visibility = 'private' "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            cw,
            tid,
        )

    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        page = (await c.get("/api/v1/conversations")).json()
    assert [x["id"] for x in page["items"]] == [conv["id"]]


@pytest.mark.asyncio
async def test_offset_limit_walks_full_set_without_gaps() -> None:
    tid = await _make_tenant()
    uid = await _make_member(tid)
    cw = await _make_shared_coworker(tid)

    base = datetime.now(UTC)
    ids: list[str] = []
    for i in range(5):
        conv = await _post_conversation(tid, uid, cw, name=f"c{i}")
        await _stamp_created_at(tid, conv["id"], base + timedelta(seconds=i))
        ids.append(conv["id"])
    expected = list(reversed(ids))  # newest first

    app = _build_app(_authed(tid, uid))
    walked: list[str] = []
    async with _client(app) as c:
        for offset in range(0, 6, 2):
            page = (
                await c.get(f"/api/v1/conversations?limit=2&offset={offset}")
            ).json()
            assert page["total"] == 5
            assert page["limit"] == 2 and page["offset"] == offset
            walked.extend(x["id"] for x in page["items"])
    assert walked == expected
