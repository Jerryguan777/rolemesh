"""Integration tests for ``/api/v1/platform/tenants`` (tenant lifecycle).

Pins the platform tenant-lifecycle surface:

- ``platform.tenant.manage`` gating: a tenant ``owner`` is denied 403 on
  every route; only ``platform_admin`` reaches them.
- provision → list/get round-trip; new tenants start ``active``.
- suspend / resume flip ``status`` and are reversible + idempotent.
- the reserved sentinel ``__platform__`` can be neither provisioned nor
  suspended/resumed.

The suspend *enforcement* (auth deny / scheduler skip) is covered in
``test_v1_tenant_suspend_enforcement.py`` and the scheduler tests; here we
only assert the CRUD/state-flip behaviour of the surface itself.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.admin.core import PLATFORM_TENANT_SLUG
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import create_tenant, create_user
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


async def _make_user(role: str, slug: str = "plat") -> AuthenticatedUser:
    t = await create_tenant(name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id,
        name="Op",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role=role, email="x@x.com", name="X"
    )


_H = {"Authorization": "Bearer x"}


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------


async def test_owner_is_forbidden_on_all_platform_tenant_routes():
    """A tenant owner lacks ``platform.tenant.manage`` → 403 everywhere."""
    user = await _make_user("owner")
    target = await create_tenant(name="victim", slug=f"v-{uuid.uuid4().hex[:8]}")
    async with _client(_build_app(user)) as ac:
        assert (await ac.get("/api/v1/platform/tenants", headers=_H)).status_code == 403
        assert (
            await ac.post(
                "/api/v1/platform/tenants", json={"name": "n"}, headers=_H
            )
        ).status_code == 403
        assert (
            await ac.get(f"/api/v1/platform/tenants/{target.id}", headers=_H)
        ).status_code == 403
        assert (
            await ac.post(
                f"/api/v1/platform/tenants/{target.id}/suspend", headers=_H
            )
        ).status_code == 403
        assert (
            await ac.post(
                f"/api/v1/platform/tenants/{target.id}/resume", headers=_H
            )
        ).status_code == 403


# ---------------------------------------------------------------------------
# Provision / list / get
# ---------------------------------------------------------------------------


async def test_provision_creates_active_tenant_and_appears_in_list_and_get():
    admin = await _make_user("platform_admin")
    async with _client(_build_app(admin)) as ac:
        slug = f"acme-{uuid.uuid4().hex[:8]}"
        created = await ac.post(
            "/api/v1/platform/tenants",
            json={"name": "Acme", "slug": slug},
            headers=_H,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "Acme"
        assert body["slug"] == slug
        assert body["status"] == "active"
        new_id = body["id"]

        got = await ac.get(f"/api/v1/platform/tenants/{new_id}", headers=_H)
        assert got.status_code == 200
        assert got.json()["id"] == new_id

        listing = await ac.get("/api/v1/platform/tenants", headers=_H)
        assert listing.status_code == 200
        assert new_id in {t["id"] for t in listing.json()}


async def test_provision_without_slug_succeeds():
    admin = await _make_user("platform_admin")
    async with _client(_build_app(admin)) as ac:
        created = await ac.post(
            "/api/v1/platform/tenants", json={"name": "NoSlug"}, headers=_H
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "active"


async def test_provision_duplicate_slug_conflicts():
    admin = await _make_user("platform_admin")
    slug = f"dup-{uuid.uuid4().hex[:8]}"
    await create_tenant(name="existing", slug=slug)
    async with _client(_build_app(admin)) as ac:
        resp = await ac.post(
            "/api/v1/platform/tenants",
            json={"name": "again", "slug": slug},
            headers=_H,
        )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


async def test_get_unknown_tenant_404():
    admin = await _make_user("platform_admin")
    async with _client(_build_app(admin)) as ac:
        resp = await ac.get(
            f"/api/v1/platform/tenants/{uuid.uuid4()}", headers=_H
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Suspend / resume
# ---------------------------------------------------------------------------


async def test_suspend_then_resume_flips_status_and_is_idempotent():
    admin = await _make_user("platform_admin")
    target = await create_tenant(name="cust", slug=f"c-{uuid.uuid4().hex[:8]}")
    async with _client(_build_app(admin)) as ac:
        s1 = await ac.post(
            f"/api/v1/platform/tenants/{target.id}/suspend", headers=_H
        )
        assert s1.status_code == 200, s1.text
        assert s1.json()["status"] == "suspended"

        # Idempotent: suspending again still returns suspended.
        s2 = await ac.post(
            f"/api/v1/platform/tenants/{target.id}/suspend", headers=_H
        )
        assert s2.status_code == 200
        assert s2.json()["status"] == "suspended"

        r1 = await ac.post(
            f"/api/v1/platform/tenants/{target.id}/resume", headers=_H
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "active"

        # The state flip persists through a fresh GET.
        got = await ac.get(f"/api/v1/platform/tenants/{target.id}", headers=_H)
        assert got.json()["status"] == "active"


async def test_suspend_unknown_tenant_404():
    admin = await _make_user("platform_admin")
    async with _client(_build_app(admin)) as ac:
        resp = await ac.post(
            f"/api/v1/platform/tenants/{uuid.uuid4()}/suspend", headers=_H
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sentinel guard
# ---------------------------------------------------------------------------


async def test_sentinel_tenant_cannot_be_suspended_or_resumed():
    admin = await _make_user("platform_admin")
    sentinel = await create_tenant(name="platform", slug=PLATFORM_TENANT_SLUG)
    async with _client(_build_app(admin)) as ac:
        suspend = await ac.post(
            f"/api/v1/platform/tenants/{sentinel.id}/suspend", headers=_H
        )
        assert suspend.status_code == 403
        assert suspend.json()["code"] == "FORBIDDEN"

        resume = await ac.post(
            f"/api/v1/platform/tenants/{sentinel.id}/resume", headers=_H
        )
        assert resume.status_code == 403

    # Still active in the DB — the refusal was a no-op, not a silent flip.
    from rolemesh.db import get_tenant

    again = await get_tenant(sentinel.id)
    assert again is not None
    assert again.status == "active"


async def test_sentinel_slug_cannot_be_provisioned():
    admin = await _make_user("platform_admin")
    async with _client(_build_app(admin)) as ac:
        resp = await ac.post(
            "/api/v1/platform/tenants",
            json={"name": "evil", "slug": PLATFORM_TENANT_SLUG},
            headers=_H,
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"
