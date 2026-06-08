"""``get_current_user`` must reject an authenticated principal with no tenant.

This is the single ``/api/v1`` auth chokepoint, so it is the belt-and-braces
complement to the provider-level tenant check: even if some provider returns a
user with an empty ``tenant_id``, a tenant-less identity reaching a
tenant-scoped query is a cross-tenant leak and must be denied here.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rolemesh.auth.provider import AuthenticatedUser
from webui.dependencies import get_current_user


def _request_with_bearer() -> Request:
    return Request(
        {"type": "http", "headers": [(b"authorization", b"Bearer x")]},
    )


async def test_rejects_authenticated_user_without_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_auth(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(user_id="u1", tenant_id="", role="member")

    monkeypatch.setattr("webui.auth.authenticate_ws", _fake_auth)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request_with_bearer())
    assert exc.value.status_code == 401
