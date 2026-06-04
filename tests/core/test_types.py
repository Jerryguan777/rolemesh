"""Tests for rolemesh.core.types.

These target the real behavior in the types module — the least-privilege
default in Coworker.__post_init__, the RegisteredGroup→Coworker conversion,
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

# --- Coworker.__post_init__ fills least-privilege permissions ----------------


def test_coworker_default_gets_least_privilege_permissions() -> None:
    """A coworker with no explicit permissions must default to the
    locked-down set: no scheduling, no managing others, no delegation."""
    cw = Coworker(id="cw1", tenant_id="t1", name="Bot", folder="bot")
    assert cw.permissions == AgentPermissions(
        task_schedule=False,
        task_manage_others=False,
        agent_delegate=False,
    )


def test_coworker_explicit_permissions_are_not_overwritten() -> None:
    """If a caller supplies permissions (e.g. a custom grant loaded from
    DB), __post_init__ must preserve them, not clobber with defaults."""
    custom = AgentPermissions(task_manage_others=True, task_schedule=True)
    cw = Coworker(
        id="cw1", tenant_id="t1", name="Bot", folder="bot",
        permissions=custom,
    )
    assert cw.permissions is custom


# --- RegisteredGroup → Coworker conversion -----------------------------------


def test_registered_group_converts_to_coworker() -> None:
    group = RegisteredGroup(name="ops", folder="ops", added_at="2024-01-01")
    cw = registered_group_to_coworker(group, "t1", "cw1")
    assert (cw.name, cw.folder, cw.tenant_id, cw.id) == ("ops", "ops", "t1", "cw1")
    # No explicit permissions → least-privilege defaults.
    assert cw.permissions is not None and cw.permissions.agent_delegate is False


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
        task_schedule=True, task_manage_others=False, agent_delegate=True
    )
    assert AgentPermissions.from_dict(perms.to_dict()) == perms


def test_agent_permissions_from_dict_defaults_missing_bits_to_false() -> None:
    """Missing keys (corrupt row, partial client payload) must coerce to
    the safe False, never silently grant a capability."""
    perms = AgentPermissions.from_dict({"task_schedule": True})
    assert perms.task_schedule is True
    assert perms.task_manage_others is False
    assert perms.agent_delegate is False
