"""REST attack-sim for ``/api/v1/approval-policies`` + ``/approval-requests``.

Hits the FastAPI app via httpx ASGI transport against a real Postgres
testcontainer. The DB-layer scoping is already proven in
``tests/db/test_approval_crud.py``; this file proves the **REST layer** above
it: that the handlers scope strictly by the *authenticated* tenant
(``user.tenant_id``), never by a client-supplied field, and that a tenant-A
user wielding a tenant-B resource id gets a flat 404 — no read, no write, no
existence oracle.

Cross-tenant isolation is the S5 exit criterion (docs/21-hitl-approval-plan.md
§10 S5), so it is the bulk of this file and is written adversarially: each test
sets up a real victim resource in tenant B and then attacks it as tenant A.
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
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")

_AUTH = {"Authorization": "Bearer x"}


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


async def _make_actor(slug: str) -> AuthenticatedUser:
    """A real tenant + owner user; the AuthenticatedUser the handler trusts."""
    t = await create_tenant(name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id,
        name="Owner",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner", email="o@x.com", name="O",
    )


def _policy_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"mcp_server_name": "stripe", "tool_name": "charge"}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path — the CRUD round-trips work at all, so a failed isolation test
# means "isolation broke", not "the endpoint is dead".
# ---------------------------------------------------------------------------


async def test_create_list_get_patch_delete_round_trip() -> None:
    user = await _make_actor("crud")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/approval-policies",
            json=_policy_body(
                tool_name="*",
                condition_expr={"field": "amount", "op": ">", "value": 100},
                priority=5,
            ),
            headers=_AUTH,
        )
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
        assert created.json()["condition_expr"] == {
            "field": "amount", "op": ">", "value": 100,
        }
        assert created.json()["priority"] == 5
        assert created.json()["enabled"] is True

        listing = await ac.get("/api/v1/approval-policies", headers=_AUTH)
        assert listing.status_code == 200
        assert [p["id"] for p in listing.json()] == [pid]

        got = await ac.get(f"/api/v1/approval-policies/{pid}", headers=_AUTH)
        assert got.status_code == 200
        assert got.json()["tool_name"] == "*"

        patched = await ac.patch(
            f"/api/v1/approval-policies/{pid}",
            json={"enabled": False, "priority": 9},
            headers=_AUTH,
        )
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False
        assert patched.json()["priority"] == 9
        # A field not sent stays untouched (model_fields_set routing).
        assert patched.json()["condition_expr"] == {
            "field": "amount", "op": ">", "value": 100,
        }

        deleted = await ac.delete(f"/api/v1/approval-policies/{pid}", headers=_AUTH)
        assert deleted.status_code == 204
        gone = await ac.get(f"/api/v1/approval-policies/{pid}", headers=_AUTH)
        assert gone.status_code == 404


async def test_create_defaults_condition_to_always_true() -> None:
    user = await _make_actor("dflt")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/approval-policies", json=_policy_body(), headers=_AUTH,
        )
    assert created.status_code == 201
    assert created.json()["condition_expr"] == {"always": True}


# ---------------------------------------------------------------------------
# Condition validation — a malformed condition is rejected at the API (422),
# never silently stored as a gate-everything policy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_expr",
    [
        {"field": "amount", "op": "??", "value": 1},   # unknown op
        {"always": "yes"},                              # non-bool always
        {"and": []},                                    # empty connective
        {"field": "x", "op": ">"},                      # missing value
        {"always": True, "field": "x", "op": "==", "value": 1},  # mixed forms
        {"or": [{"field": "x", "op": "==", "value": 1}, {"nope": 1}]},  # bad nested
    ],
)
async def test_create_rejects_malformed_condition(bad_expr: dict) -> None:
    user = await _make_actor("badc")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/approval-policies",
            json=_policy_body(condition_expr=bad_expr),
            headers=_AUTH,
        )
    assert resp.status_code == 422, resp.text


async def test_patch_rejects_malformed_condition() -> None:
    user = await _make_actor("badp")
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/approval-policies", json=_policy_body(), headers=_AUTH,
        )
        pid = created.json()["id"]
        resp = await ac.patch(
            f"/api/v1/approval-policies/{pid}",
            json={"condition_expr": {"op": "bogus"}},
            headers=_AUTH,
        )
    assert resp.status_code == 422, resp.text


async def test_body_cannot_smuggle_tenant_or_id() -> None:
    """``extra="forbid"`` means a hostile body can't override the server-side
    tenant scoping by stuffing an ``id`` / ``tenant_id`` field."""
    user = await _make_actor("smug")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/approval-policies",
            json=_policy_body(tenant_id=str(uuid.uuid4()), id=str(uuid.uuid4())),
            headers=_AUTH,
        )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Cross-tenant isolation (the S5 attack-sim). Victim resource lives in B;
# the attacker authenticates as A and wields B's id.
# ---------------------------------------------------------------------------


async def _seed_policy_in(actor: AuthenticatedUser) -> str:
    async with _client(_build_app(actor)) as ac:
        created = await ac.post(
            "/api/v1/approval-policies",
            json=_policy_body(mcp_server_name="victim-srv", tool_name="secret"),
            headers=_AUTH,
        )
    assert created.status_code == 201
    return created.json()["id"]


async def test_get_cross_tenant_policy_is_404() -> None:
    a = await _make_actor("a")
    b = await _make_actor("b")
    victim = await _seed_policy_in(b)
    async with _client(_build_app(a)) as ac:
        resp = await ac.get(f"/api/v1/approval-policies/{victim}", headers=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_list_does_not_leak_other_tenant_policies() -> None:
    a = await _make_actor("a")
    b = await _make_actor("b")
    await _seed_policy_in(b)
    async with _client(_build_app(a)) as ac:
        listing = await ac.get("/api/v1/approval-policies", headers=_AUTH)
    assert listing.status_code == 200
    assert listing.json() == []


async def test_patch_cross_tenant_policy_is_404_and_leaves_victim_intact() -> None:
    a = await _make_actor("a")
    b = await _make_actor("b")
    victim = await _seed_policy_in(b)
    async with _client(_build_app(a)) as ac:
        resp = await ac.patch(
            f"/api/v1/approval-policies/{victim}",
            json={"enabled": False, "priority": 999},
            headers=_AUTH,
        )
    assert resp.status_code == 404
    # B's policy is untouched — the cross-tenant PATCH wrote nothing.
    async with _client(_build_app(b)) as ac:
        got = await ac.get(f"/api/v1/approval-policies/{victim}", headers=_AUTH)
    assert got.status_code == 200
    assert got.json()["enabled"] is True
    assert got.json()["priority"] == 0


async def test_delete_cross_tenant_policy_is_404_and_does_not_delete() -> None:
    a = await _make_actor("a")
    b = await _make_actor("b")
    victim = await _seed_policy_in(b)
    async with _client(_build_app(a)) as ac:
        resp = await ac.delete(
            f"/api/v1/approval-policies/{victim}", headers=_AUTH,
        )
    assert resp.status_code == 404
    # Victim still exists for its owner.
    async with _client(_build_app(b)) as ac:
        got = await ac.get(f"/api/v1/approval-policies/{victim}", headers=_AUTH)
    assert got.status_code == 200


async def test_garbage_uuid_collapses_to_same_404() -> None:
    """A structurally-invalid id must not 500 or behave differently from a
    well-formed-but-absent id — otherwise it's a uuid-shape oracle."""
    user = await _make_actor("garb")
    async with _client(_build_app(user)) as ac:
        bad = await ac.get("/api/v1/approval-policies/not-a-uuid", headers=_AUTH)
        absent = await ac.get(
            f"/api/v1/approval-policies/{uuid.uuid4()}", headers=_AUTH,
        )
    assert bad.status_code == 404
    assert absent.status_code == 404
    assert bad.json()["code"] == absent.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Pending-request read (web reconnect) — also strictly tenant-scoped.
# ---------------------------------------------------------------------------


async def _seed_pending_request(actor: AuthenticatedUser) -> tuple[str, str]:
    cw = await create_coworker(
        tenant_id=actor.tenant_id,
        name=f"cw-{uuid.uuid4().hex[:6]}",
        folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    req = await create_approval_request(
        tenant_id=actor.tenant_id,
        coworker_id=cw.id,
        job_id="job-x",
        mcp_server_name="stripe",
        action={"tool_name": "charge", "params": {"amount": 500}},
        action_summary="charge $500",
        rationale="refunding the duplicate order",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return req.id, cw.id


async def test_pending_requests_returns_own_tenant_only() -> None:
    a = await _make_actor("a")
    b = await _make_actor("b")
    a_req, a_cw = await _seed_pending_request(a)
    b_req, _ = await _seed_pending_request(b)
    async with _client(_build_app(a)) as ac:
        resp = await ac.get("/api/v1/approval-requests", headers=_AUTH)
    assert resp.status_code == 200
    ids = {r["request_id"] for r in resp.json()}
    assert a_req in ids
    assert b_req not in ids
    # §1.2: the projection now carries the decision-relevant payload — the raw
    # params (the decision input), the requesting coworker, and the rationale —
    # flattened out of the internal ``action`` wrapper.
    row = next(r for r in resp.json() if r["request_id"] == a_req)
    assert row["tool_name"] == "charge"
    assert row["action_summary"] == "charge $500"
    assert row["params"] == {"amount": 500}
    assert row["coworker_id"] == a_cw
    assert row["rationale"] == "refunding the duplicate order"
    # The internal {tool_name, params} wrapper itself is never exposed verbatim.
    assert "action" not in row


async def test_pending_requests_conversation_filter_cannot_cross_tenant() -> None:
    """Filtering by a conversation id the attacker doesn't own returns [] —
    the filter is applied *inside* the tenant-scoped read, so a foreign
    conversation id can never surface another tenant's pending request."""
    a = await _make_actor("a")
    b = await _make_actor("b")
    await _seed_pending_request(b)
    async with _client(_build_app(a)) as ac:
        resp = await ac.get(
            "/api/v1/approval-requests",
            params={"conversation_id": str(uuid.uuid4())},
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json() == []
