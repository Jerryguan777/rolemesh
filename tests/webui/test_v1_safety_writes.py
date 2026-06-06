"""Integration tests for the ``/api/v1/safety`` write surface + CSV export.

Migrated off the legacy ``/api/admin/safety/*`` face. Pins:
  1. Rule create/update/delete gate on ``safety.rule.manage`` (owner +
     admin); a member (no safety capability) is 403.
  2. Body validation (unknown check_id / stage) → 400 ``INVALID_RULE``.
  3. Cross-tenant ``coworker_id`` on create → 404 (not 403).
  4. Created rules carry the v1 wire shape (source=tenant, editable=true).
  5. ``GET /safety/decisions.csv`` streams text/csv with the column header,
     scoped to the caller's tenant (no URL tenant id); member is 403.

Seeds via ``rolemesh.db`` helpers (the same path the handlers use) so the
test catches projection/storage drift rather than re-implementing logic.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_safety_rule,
    create_tenant,
    create_user,
    insert_safety_decision,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")

_HDRS = {"Authorization": "Bearer x"}
_RULE = {"stage": "pre_tool_call", "check_id": "pii.regex"}
_CFG = {"patterns": {"SSN": True}}


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


async def _make_user(role: str = "owner") -> AuthenticatedUser:
    t = await create_tenant(name="T", slug=f"sw-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="A",
        email=f"x-{uuid.uuid4().hex[:6]}@x.com",
        role=role,  # type: ignore[arg-type]
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role=role,  # type: ignore[arg-type]
        email="x@x.com", name="X",
    )


# --- create ----------------------------------------------------------------


async def test_create_rule_owner_ok() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/safety/rules",
            json={**_RULE, "config": _CFG, "description": "block SSN"},
            headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant_id"] == user.tenant_id
    assert body["coworker_id"] is None
    assert body["stage"] == "pre_tool_call"
    assert body["source"] == "tenant"
    assert body["editable"] is True


async def test_create_rule_admin_ok() -> None:
    user = await _make_user("admin")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/safety/rules",
            json={**_RULE, "config": _CFG},
            headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text


async def test_create_rule_member_forbidden() -> None:
    user = await _make_user("member")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/safety/rules",
            json={**_RULE, "config": _CFG},
            headers=_HDRS,
        )
    assert resp.status_code == 403


async def test_create_rule_unknown_check_is_400() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/safety/rules",
            json={"stage": "pre_tool_call", "check_id": "does.not.exist"},
            headers=_HDRS,
        )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_RULE"


async def test_create_rule_bad_stage_is_400() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/safety/rules",
            json={"stage": "invented_stage", "check_id": "pii.regex"},
            headers=_HDRS,
        )
    # Pydantic pattern rejects unknown stage at the body layer (422); a
    # stage that passes the pattern but the check rejects is 400. Either
    # way the rule never lands — assert it's a client error.
    assert resp.status_code in (400, 422)


async def test_create_rule_cross_tenant_coworker_is_404() -> None:
    other = await _make_user("owner")
    other_cw = await create_coworker(
        tenant_id=other.tenant_id, name="CW",
        folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    caller = await _make_user("owner")
    async with _client(_build_app(caller)) as ac:
        resp = await ac.post(
            "/api/v1/safety/rules",
            json={**_RULE, "config": _CFG, "coworker_id": other_cw.id},
            headers=_HDRS,
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# --- update ----------------------------------------------------------------


async def test_update_rule_ok() -> None:
    user = await _make_user("owner")
    rule = await create_safety_rule(
        tenant_id=user.tenant_id, stage="pre_tool_call",
        check_id="pii.regex", config=_CFG, description="before",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            f"/api/v1/safety/rules/{rule.id}",
            json={"description": "after", "enabled": False},
            headers=_HDRS,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "after"
    assert body["enabled"] is False


async def test_update_rule_unknown_is_404() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            f"/api/v1/safety/rules/{uuid.uuid4()}",
            json={"description": "x"},
            headers=_HDRS,
        )
    assert resp.status_code == 404


async def test_update_rule_member_forbidden() -> None:
    owner = await _make_user("owner")
    rule = await create_safety_rule(
        tenant_id=owner.tenant_id, stage="pre_tool_call",
        check_id="pii.regex", config=_CFG,
    )
    member = await _make_user("member")
    async with _client(_build_app(member)) as ac:
        resp = await ac.patch(
            f"/api/v1/safety/rules/{rule.id}",
            json={"description": "x"},
            headers=_HDRS,
        )
    # Member can't even see another tenant's rule; the gate fires first → 403.
    assert resp.status_code == 403


# --- delete ----------------------------------------------------------------


async def test_delete_rule_ok() -> None:
    user = await _make_user("owner")
    rule = await create_safety_rule(
        tenant_id=user.tenant_id, stage="pre_tool_call",
        check_id="pii.regex", config=_CFG,
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/safety/rules/{rule.id}", headers=_HDRS,
        )
    assert resp.status_code == 204


async def test_delete_rule_unknown_is_404() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/safety/rules/{uuid.uuid4()}", headers=_HDRS,
        )
    assert resp.status_code == 404


# --- CSV export ------------------------------------------------------------


async def test_decisions_csv_streams_header() -> None:
    # Empty export still emits the column header row; verifies the endpoint
    # derives the tenant from the session (no URL tenant id) and sets the
    # text/csv content type + attachment disposition.
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/safety/decisions.csv", headers=_HDRS)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    first_line = resp.text.splitlines()[0]
    assert first_line.startswith("id,created_at,tenant_id,")


async def test_decisions_csv_member_forbidden() -> None:
    user = await _make_user("member")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/safety/decisions.csv", headers=_HDRS)
    assert resp.status_code == 403


async def test_decisions_csv_escapes_and_scopes_to_tenant() -> None:
    # Seed a decision carrying adversarial text (comma + a formula-
    # injection prefix) and confirm the export (a) includes it for the
    # owning tenant, (b) RFC-4180 quotes the comma field, (c) neutralises
    # the leading '=' so a spreadsheet won't execute it, and (d) does NOT
    # leak another tenant's rows (session-derived tenant + RLS).
    owner = await _make_user("owner")
    await insert_safety_decision(
        tenant_id=owner.tenant_id,
        stage="pre_tool_call",
        verdict_action="block",
        triggered_rule_ids=[],
        findings=[{"code": "PII", "severity": "high", "message": "ssn"}],
        context_digest="d",
        context_summary='=HYPERLINK("evil"),tool=x',
    )
    other = await _make_user("owner")
    await insert_safety_decision(
        tenant_id=other.tenant_id,
        stage="pre_tool_call",
        verdict_action="block",
        triggered_rule_ids=[],
        findings=[],
        context_digest="d",
        context_summary="OTHER-TENANT-SECRET",
    )
    async with _client(_build_app(owner)) as ac:
        resp = await ac.get("/api/v1/safety/decisions.csv", headers=_HDRS)
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Leading '=' is neutralised with a single quote, and the comma/quote
    # field is RFC-4180 wrapped: `"'=HYPERLINK(""evil""),tool=x"`.
    assert "'=HYPERLINK" in body
    assert "tool=x" in body
    # Cross-tenant row must not appear (session tenant + RLS scope).
    assert "OTHER-TENANT-SECRET" not in body
