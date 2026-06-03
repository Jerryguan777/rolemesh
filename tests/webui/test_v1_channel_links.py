"""Integration tests for ``/api/v1/me/channel-links/...`` (v6.1 §P1.4).

The WebUI side of the Telegram link flow. The DB primitives and the
gateway handler are covered separately; this file pins the wire
contract + per-user scoping the SPA depends on.

Real Postgres (testcontainer), real router; only ``get_current_user``
is overridden so we can drive identity without an actual JWT.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    consume_link_token,
    create_channel_binding,
    create_channel_identity,
    create_coworker,
    create_tenant,
    create_user,
    list_channel_identities_for_user,
    update_channel_binding_bot_username,
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


async def _seed_tenant_with_telegram_bot(
    slug: str, *, bot_username: str | None = "rolemesh_bot"
) -> tuple[AuthenticatedUser, str]:
    """Returns (auth_user, telegram_binding_id)."""
    t = await create_tenant(
        name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:6]}",
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{slug}-{uuid.uuid4().hex[:6]}",
    )
    binding = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    if bot_username is not None:
        await update_channel_binding_bot_username(binding.id, bot_username)
    return (
        AuthenticatedUser(
            user_id=u.id, tenant_id=t.id, role="owner",
            email="x@x.com", name="U",
        ),
        binding.id,
    )


# ---------------------------------------------------------------------------
# POST — token issuance
# ---------------------------------------------------------------------------


async def test_post_issues_token_with_deep_link_when_bot_username_known() -> None:
    """Happy path: token is fresh, expires in the future, deep-link
    embeds the bot @handle and the exact token.
    """
    user, _ = await _seed_tenant_with_telegram_bot("post-ok")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["token"]) >= 22
    assert body["expires_at"]
    # Deep-link MUST embed BOTH the bot handle and the token, otherwise
    # the user clicks through to a bot but lands on a /start without
    # args (the wrong branch in the handler).
    assert body["deep_link"] == (
        f"https://t.me/rolemesh_bot?start={body['token']}"
    )
    # And the token is genuinely consumable — guards against a
    # future regression where the endpoint returns a token that was
    # never written to ``link_tokens``.
    consumed = await consume_link_token(body["token"])
    assert consumed is not None
    user_id, tenant_id, platform = consumed
    assert user_id == user.user_id
    assert tenant_id == user.tenant_id
    assert platform == "telegram"


async def test_post_returns_null_deep_link_when_bot_username_missing() -> None:
    """A tenant with a Telegram binding that hasn't connected yet has
    no @username on file. The endpoint still mints a token (the user
    can paste the code into any of the tenant's bots once they go
    live) but ``deep_link`` is null so the SPA falls back to the
    paste UI.
    """
    user, _ = await _seed_tenant_with_telegram_bot(
        "post-no-uname", bot_username=None
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["deep_link"] is None
    assert body["token"]


async def test_post_409s_when_tenant_has_no_telegram_binding() -> None:
    """A tenant without any Telegram binding has no bot to send /start
    to. We surface 409 RESOURCE_NOT_AVAILABLE so the SPA can render
    "Configure a Telegram bot first" instead of producing a dangling
    token the user could never consume.
    """
    t = await create_tenant(
        name="T", slug=f"no-tg-{uuid.uuid4().hex[:6]}",
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    user = AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner",
        email="x@x.com", name="U",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "RESOURCE_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# GET — status / polling
# ---------------------------------------------------------------------------


async def test_get_returns_empty_list_before_linking() -> None:
    """Pre-link state: GET returns ``[]``, not 404 — the SPA renders
    the "not yet connected" UI off the empty list.
    """
    user, _ = await _seed_tenant_with_telegram_bot("get-empty")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_returns_only_callers_identities_not_tenant_peers() -> None:
    """User A and User B share a tenant; both link different Telegram
    accounts. GET for A returns A's links and nothing of B's.
    """
    user_a, _ = await _seed_tenant_with_telegram_bot("get-iso")
    # Add a second user under the SAME tenant, give them their own link.
    u_b = await create_user(
        tenant_id=user_a.tenant_id, name="B",
        email=f"b-{uuid.uuid4().hex[:6]}@x.com",
    )
    user_b = AuthenticatedUser(
        user_id=u_b.id, tenant_id=user_a.tenant_id, role="member",
        email="b@x.com", name="B",
    )
    await create_channel_identity(
        user_a.tenant_id, "telegram", "1001", user_a.user_id
    )
    await create_channel_identity(
        user_a.tenant_id, "telegram", "2002", user_b.user_id
    )

    async with _client(_build_app(user_a)) as ac:
        a_resp = await ac.get(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
    assert a_resp.status_code == 200
    a_rows = a_resp.json()
    assert [r["channel_id"] for r in a_rows] == ["1001"]
    # Pin the wire field-set so the SPA never sees foreign
    # internal columns leak.
    assert set(a_rows[0].keys()) == {
        "id", "platform", "channel_id", "created_at"
    }


# ---------------------------------------------------------------------------
# DELETE — unbind
# ---------------------------------------------------------------------------


async def test_delete_unlinks_own_identity() -> None:
    user, _ = await _seed_tenant_with_telegram_bot("del-ok")
    identity = await create_channel_identity(
        user.tenant_id, "telegram", "777", user.user_id
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/me/channel-links/{identity.id}", headers=_HDRS,
        )
    assert resp.status_code == 204, resp.text
    remaining = await list_channel_identities_for_user(
        user.user_id, user.tenant_id
    )
    assert remaining == []


async def test_delete_others_identity_404s() -> None:
    """A user must not be able to delete another user's link, even
    inside the same tenant — return 404 so existence isn't leaked.
    """
    user_a, _ = await _seed_tenant_with_telegram_bot("del-other")
    u_b = await create_user(
        tenant_id=user_a.tenant_id, name="B",
        email=f"b-{uuid.uuid4().hex[:6]}@x.com",
    )
    user_b = AuthenticatedUser(
        user_id=u_b.id, tenant_id=user_a.tenant_id, role="member",
        email="b@x.com", name="B",
    )
    identity_a = await create_channel_identity(
        user_a.tenant_id, "telegram", "777", user_a.user_id
    )
    async with _client(_build_app(user_b)) as ac:
        resp = await ac.delete(
            f"/api/v1/me/channel-links/{identity_a.id}", headers=_HDRS,
        )
    assert resp.status_code == 404
    # A's link survives intact.
    a_links = await list_channel_identities_for_user(
        user_a.user_id, user_a.tenant_id
    )
    assert any(link.id == identity_a.id for link in a_links)


async def test_delete_missing_identity_404s() -> None:
    user, _ = await _seed_tenant_with_telegram_bot("del-missing")
    bogus = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/me/channel-links/{bogus}", headers=_HDRS,
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# End-to-end: POST → simulate /start → GET shows the link
# ---------------------------------------------------------------------------


async def test_post_then_consume_then_get_reflects_linked_state() -> None:
    """Composite check: the SPA's poll loop sees the link appear
    after the user consumes the token in Telegram. Touches every
    public surface in this module + the gateway side primitive.
    """
    user, _ = await _seed_tenant_with_telegram_bot("e2e")
    async with _client(_build_app(user)) as ac:
        post = await ac.post(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
        token = post.json()["token"]
        # SPA before /start: GET is empty.
        pre = await ac.get(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
        assert pre.json() == []
        # Simulate the gateway-side path (covered fully in
        # tests/channels/test_telegram_start_handler.py).
        consumed = await consume_link_token(token)
        assert consumed is not None
        await create_channel_identity(
            user.tenant_id, "telegram", "999", user.user_id
        )
        # SPA after /start: GET reflects.
        post_get = await ac.get(
            "/api/v1/me/channel-links/telegram", headers=_HDRS,
        )
    rows = post_get.json()
    assert [r["channel_id"] for r in rows] == ["999"]
