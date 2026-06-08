"""``GET /api/v1/me`` capabilities/plane reflect the role->action matrix.

These run without a DB: they call the wire projection directly. The point
is to pin that ``Me.capabilities`` is *derived from*
``rolemesh.auth.permissions._USER_ROLE_ACTIONS`` (the single source of
truth), not a hand-maintained copy — so the SPA can branch on
``capabilities`` while the server keeps doing the real enforcement in
``require_action`` / ``require_manage_or_owner``.
"""

from __future__ import annotations

import pytest

from rolemesh.auth.permissions import (
    _USER_ROLE_ACTIONS,
    role_capabilities,
)
from rolemesh.auth.provider import AuthenticatedUser
from webui.v1.auth import _user_to_me

_ROLES = ["platform_admin", "owner", "admin", "member"]


def _me(role: str):
    return _user_to_me(
        AuthenticatedUser(
            user_id="u", tenant_id="t", role=role, email="e@x", name="N",
        )
    )


@pytest.mark.parametrize("role", _ROLES)
def test_me_capabilities_equal_the_role_table(role: str) -> None:
    me = _me(role)
    assert me.capabilities == sorted(_USER_ROLE_ACTIONS[role])
    assert me.role == role


@pytest.mark.parametrize("role", _ROLES)
def test_me_plane_is_platform_only_for_platform_admin(role: str) -> None:
    assert _me(role).plane == (
        "platform" if role == "platform_admin" else "tenant"
    )


def test_platform_admin_capabilities_are_the_superset() -> None:
    # The platform-superset role must hold every other role's actions.
    union: set[str] = set()
    for r in ("owner", "admin", "member"):
        union |= set(_USER_ROLE_ACTIONS[r])
    assert union <= set(_me("platform_admin").capabilities)


def test_member_cannot_see_manage_affordances() -> None:
    caps = set(_me("member").capabilities)
    assert "coworker.create" in caps
    assert "coworker.manage" not in caps
    assert "user.manage" not in caps


def test_role_capabilities_is_fail_closed_for_unknown_role() -> None:
    assert role_capabilities("nope") == []  # type: ignore[arg-type]
