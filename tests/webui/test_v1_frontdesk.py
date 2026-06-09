"""Frontdesk v1.2 additions to the v1 coworker + approval surface.

Hits the FastAPI app via httpx ASGI transport against a real Postgres
testcontainer (no DB mock) — same harness as
``test_v1_coworkers_create.py`` / ``test_v1_coworkers_crud.py`` /
``test_v1_approval_policies.py``. Every test seeds its own tenant + owner
user so cross-test interference is impossible.

What's under test (re-expressed against ``/api/v1`` — the ORIGINAL
``feat/frontdesk:tests/webui/test_admin_frontdesk.py`` targeted the now-
deleted ``/api/admin/agents`` surface and the removed ``agent_role``
field, so this is a faithful re-expression, not a copy):

  * ``POST /api/v1/coworkers`` and ``PATCH /api/v1/coworkers/{id}`` thread
    ``permissions`` / ``is_frontdesk`` / ``routing_description`` through and
    run ``_validate_frontdesk_role`` (frontdesk v1.2, migration D1/D4):
    ``is_frontdesk=True`` requires ``permissions.agent_delegate=True``. The
    PATCH gate is evaluated against the EFFECTIVE post-update values, so an
    absent field resolves to the current row.
  * ``routing_description`` over 500 chars is a Pydantic 422 (model
    validation), distinct from the 400 role-gate.
  * ``GET /api/v1/approvals/requests?conversation_id=<parent>`` parent-walk:
    a pending approval attributed to a delegation CHILD conversation surfaces
    under the PARENT's conversation_id filter, while an unrelated
    conversation_id does not return it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    get_or_create_internal_binding,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Fixture helpers (copied verbatim from the sibling v1 suites so the harness
# stays identical — TestClient + auth override + per-test tenant seeding).
# ---------------------------------------------------------------------------


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


def _folder(prefix: str = "fd") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _make_tenant_and_user(slug_prefix: str = "v1fd") -> tuple[str, str]:
    t = await create_tenant(
        name=f"T-{slug_prefix}",
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return t.id, u.id


# ---------------------------------------------------------------------------
# Create — is_frontdesk + agent_delegate role gate (D1/D4)
# ---------------------------------------------------------------------------


async def test_create_frontdesk_with_agent_delegate_succeeds_and_round_trips() -> None:
    """is_frontdesk=True + permissions.agent_delegate=True is the working
    frontdesk built in one call. The response must echo every frontdesk
    field back — a regression that drops the field from the projection
    would silently strip the router flag.
    """
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "frontdesk",
                "folder": _folder(),
                "agent_backend": "claude",
                "is_frontdesk": True,
                "permissions": {"agent_delegate": True},
                "routing_description": "Routes user requests to specialists.",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_frontdesk"] is True
    assert body["permissions"]["agent_delegate"] is True
    # The two un-set bits keep their defaults rather than being coerced True.
    assert body["permissions"]["task_schedule"] is False
    assert body["permissions"]["task_manage_others"] is False
    assert body["routing_description"] == "Routes user requests to specialists."


async def test_create_frontdesk_without_agent_delegate_is_400() -> None:
    """is_frontdesk=True with agent_delegate=False is the confused state the
    gate exists to reject: a router that can't invoke delegate_to_agent.
    Must be a 400 INVALID_REQUEST with the documented message — not a 201,
    and not a generic Pydantic 422.
    """
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "broken-frontdesk",
                "folder": _folder(),
                "agent_backend": "claude",
                "is_frontdesk": True,
                "permissions": {"agent_delegate": False},
            },
        )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["message"] == (
        "is_frontdesk=True requires permissions.agent_delegate=True."
    )


async def test_create_frontdesk_with_omitted_permissions_is_400() -> None:
    """Omitting ``permissions`` entirely defaults agent_delegate to False, so
    is_frontdesk=True must still be rejected. Mutation guard: a regression
    that only checks an explicitly-supplied permissions object (treating
    absence as "assume delegate") would wrongly accept this.
    """
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "no-perms-frontdesk",
                "folder": _folder(),
                "agent_backend": "claude",
                "is_frontdesk": True,
            },
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_create_specialist_with_routing_description_defaults_no_caps() -> None:
    """A non-frontdesk specialist (is_frontdesk=False) carries a
    routing_description — the capability card the frontdesk LLM reads when
    routing — and must NOT trip the gate. With permissions omitted, all
    capability bits default False.
    """
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "trading-specialist",
                "folder": _folder("trade"),
                "agent_backend": "claude",
                "routing_description": "Buy and sell stocks.",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_frontdesk"] is False
    assert body["routing_description"] == "Buy and sell stocks."
    assert body["permissions"] == {
        "agent_delegate": False,
        "task_schedule": False,
        "task_manage_others": False,
    }


async def test_create_routing_description_over_500_chars_is_422() -> None:
    """The length cap is a Pydantic field constraint (max_length=500), so an
    over-long card is a 422 model-validation error — distinct from the 400
    role-gate. 501 chars is one past the boundary.
    """
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "verbose",
                "folder": _folder(),
                "agent_backend": "claude",
                "routing_description": "x" * 501,
            },
        )
    assert resp.status_code == 422, resp.text


async def test_create_routing_description_exactly_500_chars_ok() -> None:
    """Boundary: exactly 500 chars is accepted — catches an off-by-one that
    flipped ``<=`` to ``<`` on the cap.
    """
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "exact",
                "folder": _folder(),
                "agent_backend": "claude",
                "routing_description": "y" * 500,
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["routing_description"] == "y" * 500


# ---------------------------------------------------------------------------
# PATCH — effective-post-update role gate (D4)
# ---------------------------------------------------------------------------


async def _create_coworker_row(
    *,
    tenant_id: str,
    created_by_user_id: str,
    agent_delegate: bool = False,
    is_frontdesk: bool = False,
) -> str:
    """Seed a coworker directly in the DB (bypassing the create gate) so the
    PATCH tests can start from a precise prior state.

    Using the DB helper rather than the POST endpoint lets us construct the
    is_frontdesk=False-but-agent_delegate=True starting row that the POST gate
    would happily accept anyway, while keeping the test's intent (the PATCH
    flip) isolated from create-side validation.
    """
    cw = await create_coworker(
        tenant_id=tenant_id,
        name=f"cw-{uuid.uuid4().hex[:6]}",
        folder=_folder(),
        created_by_user_id=created_by_user_id,
        permissions=AgentPermissions(agent_delegate=agent_delegate),
        is_frontdesk=is_frontdesk,
    )
    return cw.id


async def test_patch_flip_frontdesk_on_coworker_with_delegate_succeeds() -> None:
    """Operator flow: a coworker already has agent_delegate=True; later they
    flip is_frontdesk=True. The body doesn't re-send permissions, so the gate
    must resolve agent_delegate from the CURRENT row (effective True) and
    accept the PATCH.
    """
    tid, uid = await _make_tenant_and_user()
    cw_id = await _create_coworker_row(
        tenant_id=tid, created_by_user_id=uid, agent_delegate=True,
    )
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={"is_frontdesk": True},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_frontdesk"] is True
    # agent_delegate was never sent — it must survive unchanged.
    assert resp.json()["permissions"]["agent_delegate"] is True


async def test_patch_flip_frontdesk_without_delegate_is_400() -> None:
    """A PATCH that flips is_frontdesk=True on a coworker WITHOUT
    agent_delegate, and does not grant it in the same call, must 400 — the
    effective agent_delegate is still False.

    Mutation guard: a regression that validates against the BODY's
    permissions (absent here) instead of the EFFECTIVE row would wrongly
    accept this.
    """
    tid, uid = await _make_tenant_and_user()
    cw_id = await _create_coworker_row(
        tenant_id=tid, created_by_user_id=uid, agent_delegate=False,
    )
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={"is_frontdesk": True},
        )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["message"] == (
        "is_frontdesk=True requires permissions.agent_delegate=True."
    )


async def test_patch_grant_delegate_and_flip_frontdesk_together_succeeds() -> None:
    """Granting agent_delegate=True AND is_frontdesk=True in the SAME PATCH is
    accepted — both effective values resolve from the body and satisfy the
    gate atomically.
    """
    tid, uid = await _make_tenant_and_user()
    cw_id = await _create_coworker_row(
        tenant_id=tid, created_by_user_id=uid, agent_delegate=False,
    )
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={
                "is_frontdesk": True,
                "permissions": {"agent_delegate": True},
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_frontdesk"] is True
    assert body["permissions"]["agent_delegate"] is True


async def test_patch_clear_frontdesk_is_always_allowed() -> None:
    """Clearing is_frontdesk=False is always allowed regardless of
    agent_delegate — the gate only fires when is_frontdesk ends up True. Here
    we start from a true frontdesk and flip it off WITHOUT touching
    permissions; it must 200.
    """
    tid, uid = await _make_tenant_and_user()
    cw_id = await _create_coworker_row(
        tenant_id=tid,
        created_by_user_id=uid,
        agent_delegate=True,
        is_frontdesk=True,
    )
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={"is_frontdesk": False},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_frontdesk"] is False


# ---------------------------------------------------------------------------
# Approval list parent-walk (frontdesk v1.2)
# ---------------------------------------------------------------------------


async def _make_pending_approval(
    *, tenant_id: str, coworker_id: str, conversation_id: str,
) -> str:
    r = await create_approval_request(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        conversation_id=conversation_id,
        job_id=f"job-{uuid.uuid4().hex[:8]}",
        mcp_server_name="mcp",
        action={"tool_name": "charge", "params": {"amount": 1}},
        action_summary="charge $1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return r.id


async def test_approval_parent_walk_surfaces_child_under_parent_filter() -> None:
    """A user viewing the PARENT conversation hits
    ``/api/v1/approvals/requests?conversation_id=<parent>`` and gets back the
    approval the specialist submitted while running in the delegation CHILD
    conversation. Without the parent walk this returns [] and the user has no
    path to the pending approval the delegate is blocked on.
    """
    tid, uid = await _make_tenant_and_user()

    # Frontdesk coworker + its web binding + the user-facing parent conv.
    fd = await create_coworker(
        tenant_id=tid, name="frontdesk", folder=_folder("fd"),
        created_by_user_id=uid,
        permissions=AgentPermissions(agent_delegate=True),
        is_frontdesk=True,
    )
    fd_binding = await create_channel_binding(
        coworker_id=fd.id, tenant_id=tid, channel_type="web", credentials={},
    )
    parent_conv = await create_conversation(
        tenant_id=tid, coworker_id=fd.id,
        channel_binding_id=fd_binding.id,
        channel_chat_id=f"web-{uuid.uuid4().hex[:8]}",
        user_id=uid,
    )

    # Target specialist + its internal binding + a delegation child conv that
    # links back to the parent via parent_conversation_id.
    target = await create_coworker(
        tenant_id=tid, name="trading", folder=_folder("trade"),
        created_by_user_id=uid,
    )
    target_binding = await get_or_create_internal_binding(
        tenant_id=tid, coworker_id=target.id,
    )
    child_conv = await create_conversation(
        tenant_id=tid, coworker_id=target.id,
        channel_binding_id=target_binding.id,
        channel_chat_id=f"internal:{parent_conv.id}:{target.id}",
        parent_conversation_id=parent_conv.id,
    )

    # The pending approval is attributed to the CHILD conv (where the
    # specialist is actually running), NOT the parent.
    child_req = await _make_pending_approval(
        tenant_id=tid, coworker_id=target.id, conversation_id=child_conv.id,
    )

    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.get(
            "/api/v1/approvals/requests",
            params={"conversation_id": parent_conv.id},
        )
    assert resp.status_code == 200, resp.text
    ids = {row["request_id"] for row in resp.json()["items"]}
    assert child_req in ids
    # The row really is the child's — its conversation_id is the child conv,
    # surfaced via the parent-walk subquery, not re-attributed to the parent.
    row = next(r for r in resp.json()["items"] if r["request_id"] == child_req)
    assert row["conversation_id"] == child_conv.id


async def test_approval_parent_walk_does_not_leak_to_unrelated_conv() -> None:
    """Filtering by an UNRELATED parent conversation must NOT return the
    child's approval. The parent-walk only follows
    ``parent_conversation_id = <filter>`` — a sibling conv that is not the
    child's parent must see nothing, or the filter would be a tenant-wide
    leak dressed up as a per-conversation read.
    """
    tid, uid = await _make_tenant_and_user()

    fd = await create_coworker(
        tenant_id=tid, name="frontdesk", folder=_folder("fd"),
        created_by_user_id=uid,
        permissions=AgentPermissions(agent_delegate=True),
        is_frontdesk=True,
    )
    fd_binding = await create_channel_binding(
        coworker_id=fd.id, tenant_id=tid, channel_type="web", credentials={},
    )
    parent_conv = await create_conversation(
        tenant_id=tid, coworker_id=fd.id,
        channel_binding_id=fd_binding.id,
        channel_chat_id=f"web-{uuid.uuid4().hex[:8]}",
        user_id=uid,
    )
    # An unrelated user-facing conversation under the SAME tenant + coworker,
    # with no child of its own.
    other_conv = await create_conversation(
        tenant_id=tid, coworker_id=fd.id,
        channel_binding_id=fd_binding.id,
        channel_chat_id=f"web-{uuid.uuid4().hex[:8]}",
        user_id=uid,
    )

    target = await create_coworker(
        tenant_id=tid, name="trading", folder=_folder("trade"),
        created_by_user_id=uid,
    )
    target_binding = await get_or_create_internal_binding(
        tenant_id=tid, coworker_id=target.id,
    )
    child_conv = await create_conversation(
        tenant_id=tid, coworker_id=target.id,
        channel_binding_id=target_binding.id,
        channel_chat_id=f"internal:{parent_conv.id}:{target.id}",
        parent_conversation_id=parent_conv.id,
    )
    child_req = await _make_pending_approval(
        tenant_id=tid, coworker_id=target.id, conversation_id=child_conv.id,
    )

    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        # Filtering by the UNRELATED conv: the child belongs to parent_conv,
        # not other_conv, so nothing should come back.
        unrelated = await c.get(
            "/api/v1/approvals/requests",
            params={"conversation_id": other_conv.id},
        )
        # Sanity: filtering by the real parent DOES return it (so a green
        # "unrelated" assertion isn't just an empty table).
        related = await c.get(
            "/api/v1/approvals/requests",
            params={"conversation_id": parent_conv.id},
        )
    assert unrelated.status_code == 200, unrelated.text
    assert child_req not in {r["request_id"] for r in unrelated.json()["items"]}
    assert unrelated.json()["total"] == 0
    assert related.status_code == 200, related.text
    assert child_req in {r["request_id"] for r in related.json()["items"]}
