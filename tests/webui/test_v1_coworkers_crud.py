"""GET / PATCH / DELETE on a single coworker.

Bundled with end-to-end coverage of the NATS hot-reload pipeline
because the publisher and the subscriber are co-designed in 01a —
splitting them across files would let a one-sided change land
without the round-trip test catching the regression.

The subscriber is wired directly to a JetStreamContext created via
nats-py against the dev NATS server. Mocking JS here would hide
exactly the bugs we want to catch: a missed subject name, a
durable that's not unique, a payload encoding mismatch. The test
skips itself if NATS isn't reachable so it stays runnable on a
laptop without docker-compose, but CI / smoke must run it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid

import httpx
import nats
import pytest
from fastapi import FastAPI
from nats.js.api import StreamConfig

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.orchestrator_state import OrchestratorState
from rolemesh.db import (
    _get_admin_pool,
    create_tenant,
    create_user,
    get_coworker,
)
from rolemesh.orchestration.coworker_hot_reload import (
    WEB_COWORKER_RESTART_SUBJECT,
    reload_coworker_into_state,
    subscribe_coworker_restart,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1 import coworker_events
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")


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


def _authed(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role="owner",
        email="x@x.com", name="X",
    )


def _folder(prefix: str = "cw") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _seed_models() -> dict[str, str]:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO models (provider, model_id, model_family, display_name) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (provider, model_id) DO NOTHING",
            "anthropic", "claude-opus-4-7", "claude", "Claude Opus 4.7",
        )
        await conn.execute(
            "INSERT INTO models (provider, model_id, model_family, display_name) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (provider, model_id) DO NOTHING",
            "anthropic", "claude-sonnet-4-6", "claude", "Claude Sonnet 4.6",
        )
        rows = await conn.fetch(
            "SELECT id, model_id FROM models WHERE provider='anthropic'"
        )
    return {r["model_id"]: str(r["id"]) for r in rows}


async def _add_credential(tenant_id: str, provider: str) -> None:
    """Seed a credential row purely so the validation chain finds one."""
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_model_credentials (tenant_id, provider, credential_data) "
            "VALUES ($1::uuid, $2, $3) ON CONFLICT (tenant_id, provider) DO NOTHING",
            tenant_id, provider, b"placeholder-ciphertext",
        )


async def _make_tenant_and_user(slug_prefix: str = "v1crud") -> tuple[str, str]:
    t = await create_tenant(
        name=f"T-{slug_prefix}",
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return t.id, u.id


async def _make_coworker(app: FastAPI, *, model_id: str | None = None) -> str:
    folder = _folder()
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": f"Helper {folder}",
                "folder": folder,
                "agent_backend": "claude",
                **({"model_id": model_id} if model_id else {}),
            },
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_coworker_returns_envelope_on_404() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.get(f"/api/v1/coworkers/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"
    assert "coworker_id" in body["details"]


@pytest.mark.asyncio
async def test_get_coworker_with_bad_uuid_returns_404_not_500() -> None:
    """``invalid-uuid`` would crash asyncpg; the handler must absorb it."""
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.get("/api/v1/coworkers/not-a-uuid")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_coworker_cross_tenant_returns_404() -> None:
    """Tenant B cannot read tenant A's coworker by guessing the UUID."""
    tid_a, uid_a = await _make_tenant_and_user("crud-a")
    tid_b, uid_b = await _make_tenant_and_user("crud-b")
    app_a = _build_app(_authed(tid_a, uid_a))
    app_b = _build_app(_authed(tid_b, uid_b))
    cw_id = await _make_coworker(app_a)
    async with _client(app_b) as c:
        resp = await c.get(f"/api/v1/coworkers/{cw_id}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_coworker_name_round_trip() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    cw_id = await _make_coworker(app)
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={"name": "Renamed Helper"},
        )
        assert resp.status_code == 200, resp.text
        get_resp = await c.get(f"/api/v1/coworkers/{cw_id}")
        assert get_resp.json()["name"] == "Renamed Helper"


@pytest.mark.asyncio
async def test_patch_model_id_validates_credential() -> None:
    """A PATCH that introduces a model_id but no credential is rejected."""
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    app = _build_app(_authed(tid, uid))
    cw_id = await _make_coworker(app)
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={"model_id": models["claude-opus-4-7"]},
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "MISSING_CREDENTIAL"


@pytest.mark.asyncio
async def test_patch_model_id_publishes_restart_event_only_when_changed() -> None:
    """Two PATCH cases: same model_id is a no-op event; new model_id fires.

    Uses an in-memory recording publisher injected via
    ``coworker_events.set_jetstream`` — that's the only seam the
    handler talks to, so we don't need a real NATS connection for
    this assertion. The end-to-end NATS round-trip is the next
    test; this one isolates the *handler* policy.
    """
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    await _add_credential(tid, "anthropic")
    app = _build_app(_authed(tid, uid))
    cw_id = await _make_coworker(app, model_id=models["claude-opus-4-7"])

    published: list[tuple[str, bytes]] = []

    class _RecordingJs:
        async def publish(self, subject: str, payload: bytes) -> None:
            published.append((subject, payload))

    coworker_events.set_jetstream(_RecordingJs())  # type: ignore[arg-type]
    try:
        async with _client(app) as c:
            # PATCH with the SAME model_id — must NOT fire.
            r1 = await c.patch(
                f"/api/v1/coworkers/{cw_id}",
                json={"model_id": models["claude-opus-4-7"]},
            )
            assert r1.status_code == 200, r1.text
            assert published == []

            # PATCH with a DIFFERENT model_id — must fire exactly once.
            r2 = await c.patch(
                f"/api/v1/coworkers/{cw_id}",
                json={"model_id": models["claude-sonnet-4-6"]},
            )
            assert r2.status_code == 200, r2.text
            assert len(published) == 1
            subject, payload = published[0]
            assert subject == WEB_COWORKER_RESTART_SUBJECT
            body = json.loads(payload.decode("utf-8"))
            assert body == {"coworker_id": cw_id, "tenant_id": tid}
    finally:
        coworker_events.set_jetstream(None)


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_coworker_returns_204_and_purges() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    cw_id = await _make_coworker(app)
    async with _client(app) as c:
        d = await c.delete(f"/api/v1/coworkers/{cw_id}")
        assert d.status_code == 204
        g = await c.get(f"/api/v1/coworkers/{cw_id}")
        assert g.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_coworker_returns_404() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        d = await c.delete(f"/api/v1/coworkers/{uuid.uuid4()}")
    assert d.status_code == 404
    assert d.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Hot-reload unit test (in-memory, no NATS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_coworker_into_state_replaces_config_and_preserves_runtime() -> None:
    """The reloader must mutate ``.config`` in place, not replace the state.

    Replacing the ``CoworkerState`` whole would orphan the
    ``conversations`` / ``channel_bindings`` dicts that the orchestrator
    builds up at runtime; the in-flight message loop holds those
    references. This test poisons a fake conversation entry and
    asserts it survives a reload.
    """
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    await _add_credential(tid, "anthropic")
    app = _build_app(_authed(tid, uid))
    cw_id = await _make_coworker(app, model_id=models["claude-opus-4-7"])

    state = OrchestratorState()

    async def _fetch(cid: str, tid_: str) -> object:
        return await get_coworker(cid, tenant_id=tid_)

    # First reload — fresh CoworkerState created.
    ok = await reload_coworker_into_state(
        coworker_id=cw_id, tenant_id=tid, state=state, fetch_coworker=_fetch,
    )
    assert ok is True
    assert cw_id in state.coworkers
    # Mutate runtime state
    state.coworkers[cw_id].conversations["sentinel"] = "preserved-me"  # type: ignore[assignment]

    # Patch model in DB via the handler so we get a real swap path
    async with _client(app) as c:
        resp = await c.patch(
            f"/api/v1/coworkers/{cw_id}",
            json={"model_id": models["claude-sonnet-4-6"]},
        )
        assert resp.status_code == 200

    # Second reload — config swapped, runtime state preserved.
    ok2 = await reload_coworker_into_state(
        coworker_id=cw_id, tenant_id=tid, state=state, fetch_coworker=_fetch,
    )
    assert ok2 is True
    assert state.coworkers[cw_id].config.model_id == models["claude-sonnet-4-6"]
    assert state.coworkers[cw_id].conversations.get("sentinel") == "preserved-me"


@pytest.mark.asyncio
async def test_reload_for_deleted_coworker_returns_false() -> None:
    state = OrchestratorState()

    async def _fetch_none(_cid: str, _tid: str) -> object:
        return None

    ok = await reload_coworker_into_state(
        coworker_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
        state=state,
        fetch_coworker=_fetch_none,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# End-to-end NATS round-trip (publish from webui -> subscribe in orch)
# ---------------------------------------------------------------------------


async def _nats_available() -> bool:
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=2)
    except Exception:
        return False
    await nc.close()
    return True


@pytest.mark.asyncio
async def test_patch_model_id_round_trips_through_real_nats() -> None:
    """PATCH -> publish -> subscribe in another asyncio context.

    Skips if NATS isn't reachable (local dev sometimes runs the
    suite without the docker-compose stack). CI smoke must run
    this — it's the only test that exercises the wire format.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping live round-trip")

    tid, uid = await _make_tenant_and_user("crud-rt")
    models = await _seed_models()
    await _add_credential(tid, "anthropic")
    app = _build_app(_authed(tid, uid))
    cw_id = await _make_coworker(app, model_id=models["claude-opus-4-7"])

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    # The stream must exist; either side normally creates it. Tests
    # don't share a webui process, so do it here.
    try:
        await js.add_stream(
            StreamConfig(name="web-ipc", subjects=["web.>"], max_age=3600.0)
        )
    except Exception:
        await js.update_stream(
            StreamConfig(name="web-ipc", subjects=["web.>"], max_age=3600.0)
        )

    # Drop any consumer a previous (crashed) run left bound. JetStream
    # durables persist server-side and a push durable permits only one
    # active subscription, so a leaked binding makes every later run fail at
    # subscribe with "consumer is already bound to a subscription". The
    # subscribe below sits outside this test's try/finally, so without this
    # pre-clean a single crash would wedge the durable permanently.
    with contextlib.suppress(Exception):
        await js.delete_consumer("web-ipc", "orch-web-coworker-restart")

    received: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    state = OrchestratorState()

    async def _fetch(cid: str, tid_: str) -> object:
        cw = await get_coworker(cid, tenant_id=tid_)
        if cw is not None:
            await received.put({"coworker_id": cid, "tenant_id": tid_})
        return cw

    sub = await subscribe_coworker_restart(js, state=state, fetch_coworker=_fetch)

    # Wire the webui publisher to *this* JS context.
    coworker_events.set_jetstream(js)
    try:
        async with _client(app) as c:
            resp = await c.patch(
                f"/api/v1/coworkers/{cw_id}",
                json={"model_id": models["claude-sonnet-4-6"]},
            )
            assert resp.status_code == 200, resp.text

        msg = await asyncio.wait_for(received.get(), timeout=10.0)
        assert msg == {"coworker_id": cw_id, "tenant_id": tid}
        # State now reflects the new model.
        assert state.coworkers[cw_id].config.model_id == models["claude-sonnet-4-6"]
    finally:
        coworker_events.set_jetstream(None)
        await sub.unsubscribe()
        # Purge so other suites don't see stale messages.
        try:
            await js.delete_consumer("web-ipc", "orch-web-coworker-restart")
        except Exception:
            pass
        await nc.close()
