"""Role -> action capability table contract (rolemesh.auth.permissions).

These tests pin the AUTHORIZATION SPEC (PLAN.md §4 matrix), not the current
implementation. They are written to fail if a future edit silently weakens a
gate (e.g. granting ``credential.byok.manage`` to ``admin``) or breaks the
fail-closed contract.
"""

from __future__ import annotations

import pytest

from rolemesh.auth.permissions import (
    _PLATFORM_ONLY_ACTIONS,
    _TENANT_ROLE_ACTIONS,
    _USER_ROLE_ACTIONS,
    user_can,
)

# The complete §3 fine-grained action set the v1 surface gates on.
_V1_ACTIONS = {
    "agent.create",
    "agent.manage",
    "agent.use",
    "skill.create",
    "skill.manage",
    "mcp.configure",
    "approval_policy.manage",
    "credential.byok.manage",
    "safety.read",
    "safety.rule.manage",
    "user.manage",
    "tenant.manage",
    "task.manage",
}


# ---------------------------------------------------------------------------
# platform_admin is the superset
# ---------------------------------------------------------------------------


def test_platform_admin_grants_every_v1_action() -> None:
    for action in _V1_ACTIONS:
        assert user_can("platform_admin", action), action


def test_platform_admin_superset_is_derived_not_hand_copied() -> None:
    """platform_admin must equal the union of every tenant role + platform-only.

    Catches drift: if someone adds an action to a tenant role but forgets to
    extend platform_admin, this fails — proving the superset is computed, not
    a stale copy.
    """
    expected = set(_PLATFORM_ONLY_ACTIONS)
    for actions in _TENANT_ROLE_ACTIONS.values():
        expected |= actions
    assert _USER_ROLE_ACTIONS["platform_admin"] == expected


# ---------------------------------------------------------------------------
# §4 matrix: per-role boundaries (negative assertions are the valuable ones)
# ---------------------------------------------------------------------------


def test_member_cannot_manage_shared_resources() -> None:
    # A member may create/use, but NOT reach the manage/configure capability.
    assert user_can("member", "agent.create")
    assert user_can("member", "agent.use")
    assert user_can("member", "skill.create")
    assert not user_can("member", "agent.manage")
    assert not user_can("member", "skill.manage")
    assert not user_can("member", "mcp.configure")
    assert not user_can("member", "approval_policy.manage")
    assert not user_can("member", "credential.byok.manage")
    assert not user_can("member", "safety.read")
    # Tenant-administration capabilities migrated off /api/admin are
    # withheld from members too.
    assert not user_can("member", "safety.rule.manage")
    assert not user_can("member", "user.manage")
    assert not user_can("member", "tenant.manage")
    assert not user_can("member", "task.manage")


def test_admin_has_manage_but_not_byok_credentials_or_tenant() -> None:
    assert user_can("admin", "agent.manage")
    assert user_can("admin", "skill.manage")
    assert user_can("admin", "mcp.configure")
    assert user_can("admin", "approval_policy.manage")
    assert user_can("admin", "safety.read")
    # Tenant-administration capabilities admin DOES hold.
    assert user_can("admin", "safety.rule.manage")
    assert user_can("admin", "user.manage")
    assert user_can("admin", "task.manage")
    # BYOK credential management and tenant settings are owner-only (§3).
    assert not user_can("admin", "credential.byok.manage")
    assert not user_can("admin", "tenant.manage")


def test_owner_has_byok_credentials_and_tenant_settings() -> None:
    assert user_can("owner", "credential.byok.manage")
    assert user_can("owner", "tenant.manage")
    for action in _V1_ACTIONS:
        assert user_can("owner", action), action


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["", "godmode", "Owner", "PLATFORM_ADMIN"])
def test_unknown_role_denies_everything(role: str) -> None:
    for action in _V1_ACTIONS:
        assert not user_can(role, action)  # type: ignore[arg-type]


def test_unknown_action_denies_for_every_role() -> None:
    for role in ("platform_admin", "owner", "admin", "member"):
        assert not user_can(role, "nonexistent.action")  # type: ignore[arg-type]
