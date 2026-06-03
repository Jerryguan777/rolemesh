"""Integration tests for ``/api/v1/runs/{id}`` and ``/api/v1/runs/{id}/cancel``.

Real Postgres testcontainer for the reads, real NATS for the
cancel-publish side. The NATS publisher is the bug-bait: if the
handler ever writes ``status='cancelled'`` itself (which would
leave a ghost container) the tests catch it because the post-state
of the DB row stays ``running`` until the orchestrator processes
the event.

A captured publisher (no real JetStream) is wired via the
``run_events`` module's ``set_jetstream`` hook so the test does
not need a docker-compose'd NATS just to assert subjects + payloads.
The orchestrator-side subscriber is tested separately in 01b PR3
(INV-6 state machine) — here we only assert what the webui emits.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    _get_admin_pool,
    create_coworker,
    create_tenant,
    create_user,
    tenant_conn,
)
from rolemesh.runs import create_run, update_run_terminal
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1 import run_events
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Captured publisher (NATS not required)
# ---------------------------------------------------------------------------


class _CapturedJS:
    """Minimal JetStream stub recording ``(subject, payload)`` calls.

    Intentionally does not match the real ``JetStreamContext``
    interface — only ``publish`` is exercised by
    ``run_events.publish_run_cancel``. A failed call surfaces as a
    typed AttributeError which keeps the test honest about which
    method the publisher relies on.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.calls.append((subject, payload))


@pytest.fixture
def js_capture() -> _CapturedJS:
    cap = _CapturedJS()
    run_events.set_jetstream(cap)  # type: ignore[arg-type]
    try:
        yield cap
    finally:
        run_events.set_jetstream(None)


# ---------------------------------------------------------------------------
# Fixture helpers
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


def _authed(tid: str, uid: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uid, tenant_id=tid, role="owner",
        email="x@x.com", name="X",
    )


async def _seed_tenant_coworker_conversation() -> tuple[str, str, str, str]:
    """Build a tenant / user / coworker / conversation; return their ids.

    Conversations need a channel_binding row to satisfy the NOT
    NULL FK; the v1 POST handler does this auto-magically but here
    we go through the DB layer so the run-related test paths
    stay isolated from the conversations endpoint's behaviour.
    """
    t = await create_tenant(
        name=f"T-{uuid.uuid4().hex[:6]}",
        slug=f"r-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id,
        name="Owner",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name="Coworker",
        folder=f"f-{uuid.uuid4().hex[:8]}",
        agent_backend="claude",
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings (coworker_id, tenant_id, channel_type) "
            "VALUES ($1::uuid, $2::uuid, 'web') RETURNING id::text",
            cw.id, t.id,
        )
        conv_id = await conn.fetchval(
            "INSERT INTO conversations (tenant_id, coworker_id, channel_binding_id, channel_chat_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4) RETURNING id::text",
            t.id, cw.id, binding_id, uuid.uuid4().hex,
        )
    return t.id, u.id, cw.id, conv_id


async def _insert_running_run(tenant_id: str, conv_id: str) -> str:
    async with tenant_conn(tenant_id) as conn:
        return await create_run(
            tenant_id=tenant_id,
            conversation_id=conv_id,
            conn=conn,
        )


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_returns_running_snapshot() -> None:
    tid, uid, _, conv_id = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid, conv_id)
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.get(f"/api/v1/runs/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == run_id
    assert body["conversation_id"] == conv_id
    assert body["status"] == "running"
    assert body["completed_at"] is None
    assert body["started_at"] is not None


@pytest.mark.asyncio
async def test_get_run_terminal_returns_completed_with_usage() -> None:
    tid, uid, _, conv_id = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid, conv_id)
    async with tenant_conn(tid) as conn:
        ok = await update_run_terminal(
            run_id=run_id,
            status="completed",
            usage={"total_tokens": 42},
            conn=conn,
        )
    assert ok
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.get(f"/api/v1/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["usage"] == {"total_tokens": 42}
    assert body["completed_at"] is not None


@pytest.mark.asyncio
async def test_get_run_with_unknown_uuid_returns_404() -> None:
    tid, uid, _, _ = await _seed_tenant_coworker_conversation()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.get(f"/api/v1/runs/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_tenant_b_cannot_read_tenant_a_run() -> None:
    """Cross-tenant GET surfaces 404 even when the UUID is known."""
    tid_a, _uid_a, _, conv_a = await _seed_tenant_coworker_conversation()
    tid_b, uid_b, _, _ = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid_a, conv_a)
    app_b = _build_app(_authed(tid_b, uid_b))
    async with _client(app_b) as c:
        r = await c.get(f"/api/v1/runs/{run_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{id}/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_publishes_to_nats_and_does_not_update_db(
    js_capture: _CapturedJS,
) -> None:
    """The contract: cancel emits a NATS event and *leaves the DB row alone*.

    Writing ``status='cancelled'`` from the webui would create a
    ghost (agent container still running, DB says cancelled). The
    DB post-condition assertion is the load-bearing mutation
    test — if a future refactor lets the webui write the terminal
    status itself this assertion turns red.
    """
    tid, uid, _, conv_id = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid, conv_id)
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(f"/api/v1/runs/{run_id}/cancel")
    assert r.status_code == 202, r.text
    # The response body echoes the still-running snapshot
    body = r.json()
    assert body["status"] == "running"
    assert body["id"] == run_id

    # NATS publish observed
    assert len(js_capture.calls) == 1
    subject, payload = js_capture.calls[0]
    assert subject == f"web.run.cancel.{run_id}"
    decoded = json.loads(payload.decode("utf-8"))
    assert decoded == {
        "run_id": run_id,
        "tenant_id": tid,
        "conversation_id": conv_id,
    }

    # DB still says running — webui must not write the terminal state
    async with tenant_conn(tid) as conn:
        status = await conn.fetchval(
            "SELECT status FROM runs WHERE id = $1::uuid", run_id
        )
    assert status == "running"


@pytest.mark.asyncio
async def test_cancel_already_terminal_returns_409_and_no_publish(
    js_capture: _CapturedJS,
) -> None:
    """Cancelling a terminal run returns 409 ALREADY_TERMINAL and skips NATS."""
    tid, uid, _, conv_id = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid, conv_id)
    async with tenant_conn(tid) as conn:
        await update_run_terminal(
            run_id=run_id, status="completed", conn=conn
        )
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(f"/api/v1/runs/{run_id}/cancel")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "ALREADY_TERMINAL"
    assert body["details"]["status"] == "completed"
    # No publish for an already-terminal run — the orchestrator
    # would have nothing to do and the noise would confuse logs.
    assert js_capture.calls == []


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_404_and_no_publish(
    js_capture: _CapturedJS,
) -> None:
    tid, uid, _, _ = await _seed_tenant_coworker_conversation()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(f"/api/v1/runs/{uuid.uuid4()}/cancel")
    assert r.status_code == 404
    assert js_capture.calls == []


@pytest.mark.asyncio
async def test_tenant_b_cannot_cancel_tenant_a_run(
    js_capture: _CapturedJS,
) -> None:
    """Cross-tenant cancel surfaces 404 and does NOT publish."""
    tid_a, _uid_a, _, conv_a = await _seed_tenant_coworker_conversation()
    tid_b, uid_b, _, _ = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid_a, conv_a)
    app_b = _build_app(_authed(tid_b, uid_b))
    async with _client(app_b) as c:
        r = await c.post(f"/api/v1/runs/{run_id}/cancel")
    assert r.status_code == 404
    assert js_capture.calls == [], (
        "publishing for a cross-tenant id would leak the run's existence"
    )


@pytest.mark.asyncio
async def test_cancel_with_no_publisher_still_returns_202() -> None:
    """No JetStream attached (e.g. NATS outage at startup) should still 202.

    The handler degrades gracefully: it logs the missing publisher
    and returns 202 anyway. The operator sees the dangling run via
    the GET path; the alternative — failing the cancel — would let
    a NATS hiccup wedge the SPA in a "cancelling…" state.
    """
    tid, uid, _, conv_id = await _seed_tenant_coworker_conversation()
    run_id = await _insert_running_run(tid, conv_id)
    # No fixture, so js_capture is not attached. Confirm publisher
    # really is set back to None (a previous test could have
    # leaked state) and that the handler still 202s.
    run_events.set_jetstream(None)
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        r = await c.post(f"/api/v1/runs/{run_id}/cancel")
    assert r.status_code == 202
