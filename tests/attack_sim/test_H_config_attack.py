"""H. Configuration-layer attacks.

Attacks exploiting the admin / REST / DB configuration surface.

  H1. Schedule task for another coworker without permission
      → AgentPermissions.task_manage_others guard
  H2. Inject malformed policy JSON to cause runtime explosion
      → Pydantic config_model on every check rejects at REST time
  H3. Agent reads the host project root
      → Mount builder never attaches the host project root to any
        container
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
# H3. Agent must never reach the host project root
# ---------------------------------------------------------------------------


def test_H3_project_root_is_never_mounted() -> None:
    """Attacker: any coworker, regardless of permissions, must never get
    the host project root mounted into its container. Defense:
    build_volume_mounts no longer attaches the project root at all."""
    from rolemesh.auth.permissions import AgentPermissions
    from rolemesh.container.runner import build_volume_mounts
    from rolemesh.core.types import Coworker

    cw = Coworker(
        id="cw",
        tenant_id="t",
        name="Agent",
        folder="agent-folder",
    )
    # Even the most-privileged permission set must not mount the host
    # project root.
    privileged = AgentPermissions(
        task_schedule=True,
        task_manage_others=True,
        agent_delegate=True,
    )
    mounts = build_volume_mounts(
        cw,
        tenant_id="t",
        conversation_id="conv",
        permissions=privileged,
        backend_config=None,
    )
    for m in mounts:
        assert "/workspace/project" not in m.container_path, (
            "the host project root must NOT be mounted into any container"
        )
