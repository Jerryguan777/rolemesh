"""Tests for rolemesh.core.types.

These target the real behavior in the types module — the role→permissions
wiring in Coworker.__post_init__, the RegisteredGroup→Coworker conversion,
and the security-relevant default on AdditionalMount — rather than reading
back hard-coded dataclass defaults (which only re-states the source).
"""

from __future__ import annotations

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.types import (
    AdditionalMount,
    Coworker,
    RegisteredGroup,
    registered_group_to_coworker,
)

# --- Coworker.__post_init__ fills role-based permissions ---------------------


def test_coworker_default_role_gets_restricted_agent_permissions() -> None:
    """A plain agent must default to the locked-down permission set:
    self-scoped data, no scheduling, no delegation."""
    cw = Coworker(id="cw1", tenant_id="t1", name="Bot", folder="bot")
    assert cw.agent_role == "agent"
    assert cw.permissions == AgentPermissions(
        data_scope="self",
        task_schedule=False,
        task_manage_others=False,
        agent_delegate=False,
    )


def test_coworker_super_agent_gets_elevated_permissions() -> None:
    cw = Coworker(id="cw1", tenant_id="t1", name="Boss", folder="boss", agent_role="super_agent")
    assert cw.permissions == AgentPermissions(
        data_scope="tenant",
        task_schedule=True,
        task_manage_others=True,
        agent_delegate=True,
    )


def test_coworker_unknown_role_falls_back_to_agent_permissions() -> None:
    """An unexpected role string must NOT silently grant elevated access —
    for_role defaults unknown roles to the restricted agent set."""
    cw = Coworker(id="cw1", tenant_id="t1", name="X", folder="x", agent_role="wizard")
    assert cw.permissions == AgentPermissions()  # all-restricted defaults
    assert cw.permissions is not None and cw.permissions.data_scope == "self"


def test_coworker_explicit_permissions_are_not_overwritten() -> None:
    """If a caller supplies permissions (e.g. a custom grant loaded from
    DB), __post_init__ must preserve them, not clobber with role defaults."""
    custom = AgentPermissions(data_scope="tenant", task_schedule=True)
    cw = Coworker(
        id="cw1", tenant_id="t1", name="Bot", folder="bot",
        agent_role="agent", permissions=custom,
    )
    assert cw.permissions is custom


# --- RegisteredGroup → Coworker conversion -----------------------------------


def test_main_group_converts_to_super_agent_with_elevated_permissions() -> None:
    group = RegisteredGroup(
        name="ops", folder="ops", trigger="@Andy", added_at="2024-01-01", is_main=True
    )
    cw = registered_group_to_coworker(group, "t1", "cw1")
    assert (cw.name, cw.folder, cw.tenant_id, cw.id) == ("ops", "ops", "t1", "cw1")
    assert cw.agent_role == "super_agent"
    # Role flows through __post_init__ into the elevated permission set.
    assert cw.permissions is not None and cw.permissions.data_scope == "tenant"
    assert cw.permissions.agent_delegate is True


def test_non_main_group_converts_to_restricted_agent() -> None:
    group = RegisteredGroup(
        name="team", folder="team", trigger="@Andy", added_at="2024-01-01", is_main=False
    )
    cw = registered_group_to_coworker(group, "t1", "cw1")
    assert cw.agent_role == "agent"
    assert cw.permissions is not None and cw.permissions.data_scope == "self"
    assert cw.permissions.agent_delegate is False


# --- AdditionalMount security default ----------------------------------------


def test_additional_mount_is_readonly_by_default() -> None:
    """Mounts must be read-only unless a caller explicitly opts into write
    access. A default of readonly=False would silently expose host paths to
    writes — this default is a security control, not an arbitrary value."""
    assert AdditionalMount(host_path="/tmp/test").readonly is True
    assert AdditionalMount(host_path="/tmp/test", readonly=False).readonly is False


# --- AgentPermissions serialization round-trip -------------------------------


def test_agent_permissions_roundtrips_through_dict() -> None:
    perms = AgentPermissions(
        data_scope="tenant", task_schedule=True, task_manage_others=False, agent_delegate=True
    )
    assert AgentPermissions.from_dict(perms.to_dict()) == perms


def test_agent_permissions_from_dict_coerces_invalid_scope_to_self() -> None:
    """An out-of-domain data_scope (corrupt row, bad client) must coerce to
    the safe 'self', never pass through to widen data access."""
    perms = AgentPermissions.from_dict({"data_scope": "everything", "task_schedule": True})
    assert perms.data_scope == "self"
    assert perms.task_schedule is True
