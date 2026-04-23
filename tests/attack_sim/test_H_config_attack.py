"""H. Configuration-layer attacks.

Attacks exploiting the admin / REST / DB configuration surface.

  H1. Schedule task for another coworker without permission
      → AgentPermissions.task_manage_others guard
  H2. Inject malformed policy JSON to cause runtime explosion
      → Pydantic config_model on every check rejects at REST time
  H3. data_scope=self agent reads tenant-wide workspace
      → Mount builder only attaches the project root when
        ``permissions.data_scope == "tenant"``
"""

from __future__ import annotations

import pytest

import rolemesh.agent  # noqa: F401  (see test_B for rationale)


# ---------------------------------------------------------------------------
# H1. Cross-coworker task scheduling
# ---------------------------------------------------------------------------


def test_H1_cross_coworker_schedule_requires_permission() -> None:
    """Attacker: a coworker with ``task_schedule=True`` but NOT
    ``task_manage_others`` calls schedule_task targeting a DIFFERENT
    coworker in the tenant. Defense: can_manage_task enforces the
    permission tuple."""
    from rolemesh.auth.authorization import (
        can_manage_task,
        can_schedule_task,
    )
    from rolemesh.auth.permissions import AgentPermissions

    perms = AgentPermissions(
        data_scope="self",
        task_schedule=True,
        task_manage_others=False,
        agent_delegate=False,
    )
    # Own coworker → OK
    assert can_schedule_task(perms) is True
    assert can_manage_task(perms, "own-cw", "own-cw") is True

    # Different coworker → NOT OK without task_manage_others
    assert can_manage_task(perms, "target-cw", "own-cw") is False


# ---------------------------------------------------------------------------
# H2. Malformed policy config — pydantic rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_config", "reason"),
    [
        ({}, "missing required allowed_hosts"),
        ({"allowed_hosts": []}, "empty list must be rejected"),
        ({"allowed_hosts": ["github.com"], "extra_field": True}, "extra='forbid' must reject"),
        ({"allowed_hosts": "github.com"}, "string instead of list"),
        ({"allowed_hosts": [""]}, "empty string host"),
    ],
)
def test_H2_domain_allowlist_config_rejects_bad_inputs(
    bad_config: dict, reason: str
) -> None:
    """Attacker: a tenant admin with access to REST /safety/rules
    pushes a malformed config that would either (a) block every call
    silently, (b) allow every call accidentally, or (c) crash the
    runtime. Defense: Pydantic config_model validates at REST
    create-time; the check's runtime code never sees a malformed config."""
    from pydantic import ValidationError

    from rolemesh.safety.checks.domain_allowlist import DomainAllowlistConfig

    with pytest.raises(ValidationError):
        DomainAllowlistConfig.model_validate(bad_config)


def test_H2_pii_regex_config_rejects_bad_inputs() -> None:
    """Same defense on pii.regex. Passing an arbitrary ``__class__``
    key to leverage a Pydantic deserialisation gadget must fail."""
    from pydantic import ValidationError

    from rolemesh.safety.checks.pii_regex import PIIRegexConfig

    # extra='forbid' on the pii.regex config → attacker's extra key rejected.
    with pytest.raises(ValidationError):
        PIIRegexConfig.model_validate(
            {"patterns": {"SSN": True}, "__class__": "os.system"}
        )


# ---------------------------------------------------------------------------
# H3. data_scope=self tries to access tenant-wide data
# ---------------------------------------------------------------------------


def test_H3_data_scope_self_does_not_mount_project_root() -> None:
    """Attacker: a coworker configured with ``data_scope=self`` should
    only see its own workspace, not the tenant-wide project root.
    Defense: build_volume_mounts conditionally attaches the project
    root only when data_scope == 'tenant'."""
    from rolemesh.auth.permissions import AgentPermissions
    from rolemesh.container.runner import build_volume_mounts
    from rolemesh.core.types import Coworker

    cw = Coworker(
        id="cw",
        tenant_id="t",
        name="Agent",
        folder="agent-folder",
    )
    # data_scope=self
    self_perms = AgentPermissions(
        data_scope="self",
        task_schedule=False,
        task_manage_others=False,
        agent_delegate=False,
    )
    mounts_self = build_volume_mounts(
        cw,
        tenant_id="t",
        conversation_id="conv",
        permissions=self_perms,
        backend_config=None,
    )
    # No mount should expose the tenant-wide project root.
    for m in mounts_self:
        assert "/workspace/project-root" not in m.container_path, (
            "data_scope=self must NOT mount project-root"
        )

    # data_scope=tenant — project-root can appear (the mount builder
    # may add a tenant-scoped shared dir). We don't assert its
    # presence (depends on config), only that the self-case excludes
    # the sensitive path.
    _ = AgentPermissions(
        data_scope="tenant",
        task_schedule=False,
        task_manage_others=False,
        agent_delegate=False,
    )
