"""Integration tests for ``/api/v1/schedules`` (PR24 read-only surface).

Hits the FastAPI app via httpx ASGI transport against the testcontainer
postgres. Catches contract drift between the read-only endpoint and
the underlying ``scheduled_tasks`` table.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.types import ScheduledTask as ScheduledTaskDataclass
from rolemesh.db import (
    create_coworker,
    create_task,
    create_tenant,
    create_user,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")

_HDRS = {"Authorization": "Bearer x"}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


async def _make_user_and_coworker(
    slug: str = "sch",
) -> tuple[AuthenticatedUser, str]:
    t = await create_tenant(
        name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{slug}-{uuid.uuid4().hex[:6]}",
    )
    return (
        AuthenticatedUser(
            user_id=u.id, tenant_id=t.id, role="owner",
            email="x@x.com", name="X",
        ),
        cw.id,
    )


async def _seed_task(*, tenant_id: str, coworker_id: str, prompt: str) -> str:
    tid = str(uuid.uuid4())
    await create_task(
        ScheduledTaskDataclass(
            id=tid,
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            prompt=prompt,
            schedule_type="interval",
            schedule_value="5m",
            context_mode="isolated",
        )
    )
    return tid


async def test_list_schedules_returns_tenant_tasks() -> None:
    user, cw_id = await _make_user_and_coworker("ls")
    a = await _seed_task(
        tenant_id=user.tenant_id, coworker_id=cw_id, prompt="alpha",
    )
    b = await _seed_task(
        tenant_id=user.tenant_id, coworker_id=cw_id, prompt="beta",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/schedules", headers=_HDRS)
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert a in ids and b in ids


async def test_list_schedules_filters_by_coworker() -> None:
    # Two coworkers in the same tenant; the filter must surface only
    # tasks for the chosen coworker, not the tenant's full set.
    user, cw_a = await _make_user_and_coworker("flta")
    cw_b = await create_coworker(
        tenant_id=user.tenant_id,
        name="CWb",
        folder=f"cw-fltb-{uuid.uuid4().hex[:6]}",
    )
    task_a = await _seed_task(
        tenant_id=user.tenant_id, coworker_id=cw_a, prompt="for-a",
    )
    task_b = await _seed_task(
        tenant_id=user.tenant_id, coworker_id=cw_b.id, prompt="for-b",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            f"/api/v1/schedules?coworker_id={cw_a}", headers=_HDRS,
        )
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert task_a in ids
    assert task_b not in ids


async def test_list_schedules_excludes_other_tenants() -> None:
    # Cross-tenant defense: tenant A's listing must not include
    # tenant B's tasks even though both rows live in the same table.
    # Catches a missed WHERE tenant_id at the handler layer (RLS
    # enforces it too, but the test makes the contract explicit).
    user_a, _cw_a = await _make_user_and_coworker("cta")
    user_b, cw_b = await _make_user_and_coworker("ctb")
    other = await _seed_task(
        tenant_id=user_b.tenant_id, coworker_id=cw_b, prompt="other",
    )
    async with _client(_build_app(user_a)) as ac:
        resp = await ac.get("/api/v1/schedules", headers=_HDRS)
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert other not in ids


async def test_get_schedule_returns_one() -> None:
    user, cw = await _make_user_and_coworker("g1")
    tid = await _seed_task(
        tenant_id=user.tenant_id, coworker_id=cw, prompt="probe",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(f"/api/v1/schedules/{tid}", headers=_HDRS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == tid
    assert body["prompt"] == "probe"


async def test_get_schedule_404_on_missing() -> None:
    user, _ = await _make_user_and_coworker("g404")
    bogus = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(f"/api/v1/schedules/{bogus}", headers=_HDRS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_get_schedule_404_on_cross_tenant() -> None:
    # Cross-tenant existence probing: tenant A asking for tenant B's
    # task id must get 404, NOT 200. RLS makes get_task_by_id return
    # None; the handler then 404s. Pinning this so a future "go
    # through admin_conn for read-perf" refactor can't open a leak.
    user_a, _ = await _make_user_and_coworker("ca")
    user_b, cw_b = await _make_user_and_coworker("cb")
    tid_b = await _seed_task(
        tenant_id=user_b.tenant_id, coworker_id=cw_b, prompt="b-only",
    )
    async with _client(_build_app(user_a)) as ac:
        resp = await ac.get(f"/api/v1/schedules/{tid_b}", headers=_HDRS)
    assert resp.status_code == 404


async def test_get_schedule_404_on_malformed_uuid() -> None:
    # asyncpg.DataError on a non-UUID path param should surface as
    # 404, not 500. Pattern matches skills + coworkers handling.
    user, _ = await _make_user_and_coworker("mu")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/schedules/not-a-uuid", headers=_HDRS)
    assert resp.status_code == 404
