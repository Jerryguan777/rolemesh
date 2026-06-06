"""Visibility (private/shared) enforcement on the /api/v1 surface.

feat/roles PR3. These tests assert the AUTHORIZATION SPEC, not handler
internals, and are deliberately adversarial: the valuable case is the
negative one — a member must NOT see/use another member's private
resource, on the LIST, FETCH, USE (conversation-create), and skill-bind
paths. Owner/admin see everything. Sharing flips the boundary.

Auth is injected by overriding ``get_current_user`` with a fixed role
(the same dependency ``require_action`` resolves through), so the role
under test is exactly the one the gates see.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.types import SkillFile as SkillFileDataclass
from rolemesh.db import (
    create_coworker,
    create_skill,
    create_tenant,
    create_user,
    get_coworker,
    get_skill,
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


def _authed(tenant_id: str, user_id: str, role: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role=role,
        email="x@x.com", name="X",
    )


async def _tenant() -> str:
    t = await create_tenant(name="T", slug=f"vis-{uuid.uuid4().hex[:8]}")
    return t.id


async def _user(tenant_id: str, role: str) -> str:
    u = await create_user(
        tenant_id=tenant_id, name="U",
        email=f"u-{uuid.uuid4().hex[:8]}@x.com", role=role,
    )
    return u.id


async def _seed_coworker(
    tenant_id: str, *, created_by: str | None, visibility: str,
) -> str:
    cw = await create_coworker(
        tenant_id=tenant_id, name=f"CW {uuid.uuid4().hex[:6]}",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
        created_by_user_id=created_by,
        visibility=visibility,
    )
    return cw.id


async def _seed_skill(
    tenant_id: str, *, created_by: str | None, visibility: str,
) -> str:
    skill = await create_skill(
        tenant_id=tenant_id,
        name=f"skill-{uuid.uuid4().hex[:6]}",
        frontmatter_common={"description": "x" * 40},
        frontmatter_backend={},
        files={"SKILL.md": SkillFileDataclass(path="SKILL.md", content="body")},
        enabled=True,
        created_by_user_id=created_by,
        visibility=visibility,
    )
    return skill.id


# ---------------------------------------------------------------------------
# Default-on-create: a new coworker / skill is PRIVATE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_coworker_defaults_private_and_hidden_from_others() -> None:
    """A coworker created via v1 defaults to private and is invisible to
    another member until shared. Guards against a 'shared'-by-default
    regression (which would leak every draft tenant-wide)."""
    tid = await _tenant()
    creator = await _user(tid, "member")
    other = await _user(tid, "member")

    app = _build_app(_authed(tid, creator, "member"))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "Draft",
                "folder": f"f-{uuid.uuid4().hex[:8]}",
                "agent_backend": "claude",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["visibility"] == "private", body
    cw_id = body["id"]

    # Another member can neither list nor fetch it.
    other_app = _build_app(_authed(tid, other, "member"))
    async with _client(other_app) as c:
        listed = await c.get("/api/v1/coworkers")
        assert cw_id not in {r["id"] for r in listed.json()["items"]}
        fetched = await c.get(f"/api/v1/coworkers/{cw_id}")
        assert fetched.status_code == 404, fetched.text


@pytest.mark.asyncio
async def test_created_skill_defaults_private() -> None:
    tid = await _tenant()
    creator = await _user(tid, "member")
    name = f"sk-{uuid.uuid4().hex[:6]}"
    manifest = f"---\nname: {name}\ndescription: " + "y" * 40 + "\n---\nbody"
    app = _build_app(_authed(tid, creator, "member"))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/skills",
            json={"name": name, "files": {"SKILL.md": manifest}},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["visibility"] == "private", resp.text


# ---------------------------------------------------------------------------
# LIST predicate: member sees shared + own private, NOT others' private.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_list_coworkers_scopes_to_visible() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")

    my_private = await _seed_coworker(tid, created_by=me, visibility="private")
    shared = await _seed_coworker(tid, created_by=other, visibility="shared")
    others_private = await _seed_coworker(
        tid, created_by=other, visibility="private"
    )
    orphan_private = await _seed_coworker(
        tid, created_by=None, visibility="private"
    )

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        resp = await c.get("/api/v1/coworkers")
    ids = {r["id"] for r in resp.json()["items"]}
    assert my_private in ids
    assert shared in ids
    assert others_private not in ids
    # NULL three-valued logic: an un-attributed private row is NOT "mine".
    assert orphan_private not in ids


@pytest.mark.asyncio
async def test_admin_list_coworkers_sees_everything() -> None:
    tid = await _tenant()
    member = await _user(tid, "member")
    admin = await _user(tid, "admin")
    a = await _seed_coworker(tid, created_by=member, visibility="private")
    b = await _seed_coworker(tid, created_by=None, visibility="private")

    app = _build_app(_authed(tid, admin, "admin"))
    async with _client(app) as c:
        resp = await c.get("/api/v1/coworkers")
    ids = {r["id"] for r in resp.json()["items"]}
    assert {a, b} <= ids


@pytest.mark.asyncio
async def test_list_coworkers_pagination_total_respects_visibility() -> None:
    # The page envelope's total must reflect the VISIBLE set, not the
    # whole tenant: a member who can see fewer rows gets a smaller total
    # than an admin over the same data (the count mirrors the list's
    # visibility predicate).
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    await _seed_coworker(tid, created_by=me, visibility="private")
    await _seed_coworker(tid, created_by=other, visibility="shared")
    await _seed_coworker(tid, created_by=other, visibility="private")

    async with _client(_build_app(_authed(tid, me, "member"))) as c:
        member_page = (await c.get("/api/v1/coworkers")).json()
    async with _client(_build_app(_authed(tid, me, "admin"))) as c:
        admin_page = (await c.get("/api/v1/coworkers")).json()

    # Member sees own-private + shared (2); admin sees all 3.
    assert member_page["total"] == 2
    assert admin_page["total"] == 3
    assert member_page["limit"] == 50 and member_page["offset"] == 0


@pytest.mark.asyncio
async def test_member_list_skills_scopes_to_visible() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    mine = await _seed_skill(tid, created_by=me, visibility="private")
    shared = await _seed_skill(tid, created_by=other, visibility="shared")
    foreign = await _seed_skill(tid, created_by=other, visibility="private")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        resp = await c.get("/api/v1/skills")
    ids = {r["id"] for r in resp.json()["items"]}
    assert mine in ids
    assert shared in ids
    assert foreign not in ids


# ---------------------------------------------------------------------------
# FETCH: member 404s on another member's private resource (existence hidden).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_fetch_foreign_private_coworker_404() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    foreign = await _seed_coworker(tid, created_by=other, visibility="private")
    shared = await _seed_coworker(tid, created_by=other, visibility="shared")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        assert (await c.get(f"/api/v1/coworkers/{foreign}")).status_code == 404
        assert (await c.get(f"/api/v1/coworkers/{shared}")).status_code == 200


@pytest.mark.asyncio
async def test_member_fetch_orphan_private_coworker_404_admin_200() -> None:
    """NULL three-valued logic on the FETCH path: a coworker with
    created_by_user_id IS NULL + private is invisible to a member (404)
    but visible to an admin (200). No error is raised either way."""
    tid = await _tenant()
    me = await _user(tid, "member")
    admin = await _user(tid, "admin")
    orphan = await _seed_coworker(tid, created_by=None, visibility="private")

    member_app = _build_app(_authed(tid, me, "member"))
    async with _client(member_app) as c:
        assert (await c.get(f"/api/v1/coworkers/{orphan}")).status_code == 404

    admin_app = _build_app(_authed(tid, admin, "admin"))
    async with _client(admin_app) as c:
        assert (await c.get(f"/api/v1/coworkers/{orphan}")).status_code == 200


@pytest.mark.asyncio
async def test_member_fetch_foreign_private_skill_404() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    foreign = await _seed_skill(tid, created_by=other, visibility="private")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        assert (await c.get(f"/api/v1/skills/{foreign}")).status_code == 404


# ---------------------------------------------------------------------------
# USE path (the feed-forward gap): conversation-create against a private
# coworker the member doesn't own must 404 — not silently succeed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_open_conversation_on_foreign_private_coworker() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    foreign = await _seed_coworker(tid, created_by=other, visibility="private")
    shared = await _seed_coworker(tid, created_by=other, visibility="shared")
    mine = await _seed_coworker(tid, created_by=me, visibility="private")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        blocked = await c.post(
            f"/api/v1/coworkers/{foreign}/conversations", json={"name": "x"}
        )
        assert blocked.status_code == 404, blocked.text
        # A shared coworker and the member's own private one are usable.
        ok_shared = await c.post(
            f"/api/v1/coworkers/{shared}/conversations", json={"name": "x"}
        )
        assert ok_shared.status_code == 201, ok_shared.text
        ok_own = await c.post(
            f"/api/v1/coworkers/{mine}/conversations", json={"name": "x"}
        )
        assert ok_own.status_code == 201, ok_own.text


@pytest.mark.asyncio
async def test_member_cannot_list_conversations_on_foreign_private_coworker() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    foreign = await _seed_coworker(tid, created_by=other, visibility="private")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        resp = await c.get(f"/api/v1/coworkers/{foreign}/conversations")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Skill-bind path: member may only attach a skill they can see.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_bind_foreign_private_skill() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    my_cw = await _seed_coworker(tid, created_by=me, visibility="private")
    foreign_skill = await _seed_skill(
        tid, created_by=other, visibility="private"
    )
    shared_skill = await _seed_skill(
        tid, created_by=other, visibility="shared"
    )

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        blocked = await c.post(
            f"/api/v1/coworkers/{my_cw}/skills/{foreign_skill}"
        )
        assert blocked.status_code == 404, blocked.text
        ok = await c.post(f"/api/v1/coworkers/{my_cw}/skills/{shared_skill}")
        assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------------------
# Share / unshare: own-or-manage gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_share_own_coworker_then_others_see_it() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    cw = await _seed_coworker(tid, created_by=me, visibility="private")

    me_app = _build_app(_authed(tid, me, "member"))
    async with _client(me_app) as c:
        shared = await c.post(f"/api/v1/coworkers/{cw}/share")
        assert shared.status_code == 200, shared.text
        assert shared.json()["visibility"] == "shared"

    # Now visible + usable to another member.
    other_app = _build_app(_authed(tid, other, "member"))
    async with _client(other_app) as c:
        assert (await c.get(f"/api/v1/coworkers/{cw}")).status_code == 200
        conv = await c.post(
            f"/api/v1/coworkers/{cw}/conversations", json={"name": "x"}
        )
        assert conv.status_code == 201, conv.text


@pytest.mark.asyncio
async def test_member_cannot_share_foreign_coworker() -> None:
    """A member sharing someone ELSE's private coworker is a MANAGE
    attempt → 403 from the ownership gate (not 404), matching PATCH/DELETE."""
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    foreign = await _seed_coworker(tid, created_by=other, visibility="private")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        resp = await c.post(f"/api/v1/coworkers/{foreign}/share")
    assert resp.status_code == 403, resp.text
    # And the DB row is unchanged.
    row = await get_coworker(foreign, tenant_id=tid)
    assert row is not None and row.visibility == "private"


@pytest.mark.asyncio
async def test_admin_can_share_any_coworker() -> None:
    tid = await _tenant()
    member = await _user(tid, "member")
    admin = await _user(tid, "admin")
    cw = await _seed_coworker(tid, created_by=member, visibility="private")

    app = _build_app(_authed(tid, admin, "admin"))
    async with _client(app) as c:
        resp = await c.post(f"/api/v1/coworkers/{cw}/share")
    assert resp.status_code == 200, resp.text
    assert resp.json()["visibility"] == "shared"


@pytest.mark.asyncio
async def test_unshare_makes_coworker_private_again() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    cw = await _seed_coworker(tid, created_by=me, visibility="shared")

    me_app = _build_app(_authed(tid, me, "member"))
    async with _client(me_app) as c:
        resp = await c.post(f"/api/v1/coworkers/{cw}/unshare")
        assert resp.status_code == 200, resp.text
        assert resp.json()["visibility"] == "private"

    other_app = _build_app(_authed(tid, other, "member"))
    async with _client(other_app) as c:
        assert (await c.get(f"/api/v1/coworkers/{cw}")).status_code == 404


@pytest.mark.asyncio
async def test_member_can_share_own_skill_member_cannot_share_foreign() -> None:
    tid = await _tenant()
    me = await _user(tid, "member")
    other = await _user(tid, "member")
    mine = await _seed_skill(tid, created_by=me, visibility="private")
    foreign = await _seed_skill(tid, created_by=other, visibility="private")

    app = _build_app(_authed(tid, me, "member"))
    async with _client(app) as c:
        ok = await c.post(f"/api/v1/skills/{mine}/share")
        assert ok.status_code == 200, ok.text
        assert ok.json()["visibility"] == "shared"
        denied = await c.post(f"/api/v1/skills/{foreign}/share")
        assert denied.status_code == 403, denied.text
    row = await get_skill(foreign, tenant_id=tid)
    assert row is not None and row.visibility == "private"
