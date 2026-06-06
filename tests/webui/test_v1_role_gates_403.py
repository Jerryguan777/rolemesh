"""403 role-gate + ownership-escape behavior on the /api/v1 surface.

These tests assert the AUTHORIZATION SPEC (PLAN.md §4), not the handler
internals. For each gated endpoint the valuable assertion is the negative one:
an under-privileged role is rejected with 403 BEFORE any side effect. The
boundary role (lowest role that SHOULD pass) is also checked so the gate isn't
accidentally over-tight.

The ownership-escape cases are the subtle ones: a member CAN update/delete a
coworker/skill they created, and CANNOT touch one created by someone else (the
gate falls back to requiring ``coworker.manage`` / ``skill.manage``).

Auth is injected by overriding ``get_current_user`` with a fixed role, so the
role under test is exactly the one the gate sees (``require_action`` resolves
the user via that same dependency).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.credential_vault import CredentialVault, set_credential_vault
from rolemesh.auth.encryption import derive_fernet_key
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.types import SkillFile as SkillFileDataclass
from rolemesh.db import (
    create_coworker,
    create_skill,
    create_tenant,
    create_user,
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
    t = await create_tenant(
        name="T", slug=f"gate-{uuid.uuid4().hex[:8]}",
    )
    return t.id


async def _user(tenant_id: str, role: str) -> str:
    u = await create_user(
        tenant_id=tenant_id, name="U",
        email=f"u-{uuid.uuid4().hex[:8]}@x.com", role=role,
    )
    return u.id


async def _seed_coworker(tenant_id: str, *, created_by: str | None) -> str:
    cw = await create_coworker(
        tenant_id=tenant_id, name=f"CW {uuid.uuid4().hex[:6]}",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
        created_by_user_id=created_by,
    )
    return cw.id


async def _seed_skill(tenant_id: str, *, created_by: str | None) -> str:
    name = f"skill-{uuid.uuid4().hex[:6]}"
    skill = await create_skill(
        tenant_id=tenant_id,
        name=name,
        frontmatter_common={"description": "x" * 40},
        frontmatter_backend={},
        files={"SKILL.md": SkillFileDataclass(path="SKILL.md", content="body")},
        enabled=True,
        created_by_user_id=created_by,
    )
    return skill.id


# ---------------------------------------------------------------------------
# coworkers: create requires coworker.create (all roles); manage gated for member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_create_coworker() -> None:
    """coworker.create is granted to member — the boundary role passes."""
    tid = await _tenant()
    uid = await _user(tid, "member")
    app = _build_app(_authed(tid, uid, "member"))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "Mine",
                "folder": f"f-{uuid.uuid4().hex[:8]}",
                "agent_backend": "claude",
            },
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_member_can_patch_own_coworker_but_not_others() -> None:
    """Ownership escape: member edits OWN coworker (200) but not another's (403)."""
    tid = await _tenant()
    member_id = await _user(tid, "member")
    other_id = await _user(tid, "member")

    own = await _seed_coworker(tid, created_by=member_id)
    foreign = await _seed_coworker(tid, created_by=other_id)

    app = _build_app(_authed(tid, member_id, "member"))
    async with _client(app) as c:
        ok = await c.patch(f"/api/v1/coworkers/{own}", json={"name": "Renamed"})
        assert ok.status_code == 200, ok.text

        denied = await c.patch(
            f"/api/v1/coworkers/{foreign}", json={"name": "Hijack"}
        )
        assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_member_cannot_manage_unattributed_coworker() -> None:
    """Three-valued logic: created_by_user_id IS NULL is NOT 'mine'.

    A member must not be able to claim an un-attributed (legacy/system)
    coworker as their own; NULL falls through to requiring coworker.manage.
    """
    tid = await _tenant()
    member_id = await _user(tid, "member")
    orphan = await _seed_coworker(tid, created_by=None)

    app = _build_app(_authed(tid, member_id, "member"))
    async with _client(app) as c:
        resp = await c.patch(f"/api/v1/coworkers/{orphan}", json={"name": "X"})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_admin_can_manage_any_coworker() -> None:
    """coworker.manage holder reaches a coworker they did not create."""
    tid = await _tenant()
    creator = await _user(tid, "member")
    admin_id = await _user(tid, "admin")
    cw = await _seed_coworker(tid, created_by=creator)

    app = _build_app(_authed(tid, admin_id, "admin"))
    async with _client(app) as c:
        resp = await c.patch(f"/api/v1/coworkers/{cw}", json={"name": "AdminEdit"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_member_can_delete_own_coworker_not_foreign() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    other_id = await _user(tid, "member")
    own = await _seed_coworker(tid, created_by=member_id)
    foreign = await _seed_coworker(tid, created_by=other_id)

    app = _build_app(_authed(tid, member_id, "member"))
    async with _client(app) as c:
        denied = await c.delete(f"/api/v1/coworkers/{foreign}")
        assert denied.status_code == 403, denied.text
        ok = await c.delete(f"/api/v1/coworkers/{own}")
        assert ok.status_code == 204, ok.text


# ---------------------------------------------------------------------------
# skills: create granted to member; manage gated with ownership escape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_patch_own_skill_but_not_foreign() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    other_id = await _user(tid, "member")
    own = await _seed_skill(tid, created_by=member_id)
    foreign = await _seed_skill(tid, created_by=other_id)

    app = _build_app(_authed(tid, member_id, "member"))
    async with _client(app) as c:
        ok = await c.patch(f"/api/v1/skills/{own}", json={"enabled": False})
        assert ok.status_code == 200, ok.text
        denied = await c.patch(f"/api/v1/skills/{foreign}", json={"enabled": False})
        assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_member_can_delete_own_skill_not_foreign() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    other_id = await _user(tid, "member")
    own = await _seed_skill(tid, created_by=member_id)
    foreign = await _seed_skill(tid, created_by=other_id)

    app = _build_app(_authed(tid, member_id, "member"))
    async with _client(app) as c:
        denied = await c.delete(f"/api/v1/skills/{foreign}")
        assert denied.status_code == 403, denied.text
        ok = await c.delete(f"/api/v1/skills/{own}")
        assert ok.status_code == 204, ok.text


# ---------------------------------------------------------------------------
# mcp.configure: member denied, admin allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_create_mcp_server_admin_can() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    admin_id = await _user(tid, "admin")
    payload = {
        "name": f"mcp-{uuid.uuid4().hex[:6]}",
        "type": "http",
        "url": "https://example.com/mcp",
        "auth_mode": "service",
    }

    member_app = _build_app(_authed(tid, member_id, "member"))
    async with _client(member_app) as c:
        denied = await c.post("/api/v1/mcp-servers", json=payload)
    assert denied.status_code == 403, denied.text

    admin_app = _build_app(_authed(tid, admin_id, "admin"))
    async with _client(admin_app) as c:
        ok = await c.post("/api/v1/mcp-servers", json=payload)
    assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------------------
# approval_policy.manage: member denied, admin allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_write_approval_policy_admin_can() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    admin_id = await _user(tid, "admin")
    payload = {
        "mcp_server_name": "srv",
        "tool_name": "do_thing",
        "condition_expr": {"always": True},
        "enabled": True,
        "priority": 10,
    }

    member_app = _build_app(_authed(tid, member_id, "member"))
    async with _client(member_app) as c:
        denied = await c.post("/api/v1/approval-policies", json=payload)
    assert denied.status_code == 403, denied.text

    admin_app = _build_app(_authed(tid, admin_id, "admin"))
    async with _client(admin_app) as c:
        ok = await c.post("/api/v1/approval-policies", json=payload)
    assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------------------
# credential.byok.manage: owner-only — admin AND member denied, owner allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_owner_can_put_credential() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    admin_id = await _user(tid, "admin")
    owner_id = await _user(tid, "owner")
    body = {"api_key": "sk-test"}

    for uid, role in ((member_id, "member"), (admin_id, "admin")):
        app = _build_app(_authed(tid, uid, role))
        async with _client(app) as c:
            denied = await c.put("/api/v1/tenant/credentials/anthropic", json=body)
        assert denied.status_code == 403, f"{role}: {denied.text}"

    # Owner is the boundary role: the PUT must pass the gate AND succeed.
    # Install a vault so the encrypt step doesn't 500 (the gate, not the
    # vault, is under test — but a clean 200 is the strongest signal).
    set_credential_vault(CredentialVault(derive_fernet_key("test-gate-key")))
    try:
        owner_app = _build_app(_authed(tid, owner_id, "owner"))
        async with _client(owner_app) as c:
            resp = await c.put("/api/v1/tenant/credentials/anthropic", json=body)
        assert resp.status_code == 200, resp.text
    finally:
        set_credential_vault(None)


@pytest.mark.asyncio
async def test_admin_cannot_list_credentials_owner_can() -> None:
    tid = await _tenant()
    admin_id = await _user(tid, "admin")
    owner_id = await _user(tid, "owner")

    admin_app = _build_app(_authed(tid, admin_id, "admin"))
    async with _client(admin_app) as c:
        denied = await c.get("/api/v1/tenant/credentials")
    assert denied.status_code == 403, denied.text

    owner_app = _build_app(_authed(tid, owner_id, "owner"))
    async with _client(owner_app) as c:
        ok = await c.get("/api/v1/tenant/credentials")
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# safety.read: member denied, admin allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_read_safety_rules_admin_can() -> None:
    tid = await _tenant()
    member_id = await _user(tid, "member")
    admin_id = await _user(tid, "admin")

    member_app = _build_app(_authed(tid, member_id, "member"))
    async with _client(member_app) as c:
        denied = await c.get("/api/v1/safety/rules")
    assert denied.status_code == 403, denied.text

    admin_app = _build_app(_authed(tid, admin_id, "admin"))
    async with _client(admin_app) as c:
        ok = await c.get("/api/v1/safety/rules")
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# platform_admin is the superset: passes every gate above
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_admin_passes_owner_only_credential_gate() -> None:
    tid = await _tenant()
    pa_id = await _user(tid, "platform_admin")
    app = _build_app(_authed(tid, pa_id, "platform_admin"))
    async with _client(app) as c:
        ok = await c.get("/api/v1/tenant/credentials")
    assert ok.status_code == 200, ok.text
