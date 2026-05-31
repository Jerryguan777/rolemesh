"""DB-layer cross-tenant isolation regression tests.

By-id lookups such as `get_safety_rule(rule_id)`, `get_user(user_id)`,
and `get_safety_decision(id)` must take a tenant_id and filter on it,
rather than relying on an upstream REST handler to check
`row.tenant_id == user.tenant_id`.

If any caller forgot the upstream check — or a future caller forgot the
upstream check — the DB would happily hand over another tenant's data.
These tests pin the boundary at the DB function layer: each lookup MUST
reject mismatched tenant_id.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.types import ScheduledTask
from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_safety_rule,
    create_task,
    create_tenant,
    create_user,
    get_channel_binding,
    get_conversation,
    get_coworker,
    get_safety_decision,
    get_safety_rule,
    get_task_by_id,
    get_user,
    insert_safety_decision,
    resolve_user_for_auth,
    update_user,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Two-tenant fixture
# ---------------------------------------------------------------------------


async def _two_tenants() -> dict[str, dict[str, str]]:
    """Build two complete (tenant, user, coworker, conversation) chains."""
    out: dict[str, dict[str, str]] = {}
    for tag in ("A", "B"):
        t = await create_tenant(name=f"Tenant {tag}", slug=f"t-{tag.lower()}-{uuid.uuid4().hex[:6]}")
        u = await create_user(
            tenant_id=t.id,
            name=f"User {tag}",
            email=f"u-{tag.lower()}-{uuid.uuid4().hex[:6]}@x.com",
            role="owner",
        )
        cw = await create_coworker(
            tenant_id=t.id, name=f"CW{tag}", folder=f"cw-{tag.lower()}-{uuid.uuid4().hex[:6]}"
        )
        b = await create_channel_binding(
            coworker_id=cw.id,
            tenant_id=t.id,
            channel_type="telegram",
            credentials={"bot_token": "x"},
        )
        conv = await create_conversation(
            tenant_id=t.id,
            coworker_id=cw.id,
            channel_binding_id=b.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        out[tag] = {
            "tenant_id": t.id,
            "user_id": u.id,
            "coworker_id": cw.id,
            "conversation_id": conv.id,
        }
    return out


# ---------------------------------------------------------------------------
# get_safety_rule
# ---------------------------------------------------------------------------


async def test_get_safety_rule_rejects_mismatched_tenant() -> None:
    """A safety rule created for tenant A must NOT be returned when
    looked up under tenant B."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    rule = await create_safety_rule(
        tenant_id=a["tenant_id"],
        stage="pre_tool_call",
        check_id="domain_allowlist",
        config={"allowed_hosts": ["api.example.com"]},
    )

    same_tenant = await get_safety_rule(rule.id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == rule.id

    leaked = await get_safety_rule(rule.id, tenant_id=b["tenant_id"])
    assert leaked is None, (
        "get_safety_rule leaked tenant A's rule to tenant B"
    )


# ---------------------------------------------------------------------------
# PR-A: by-id business functions
# ---------------------------------------------------------------------------


async def test_get_user_rejects_mismatched_tenant() -> None:
    """get_user(user_id, tenant_id=B) for a tenant-A user_id must
    return None — even when the user_id is a real, valid UUID."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    same_tenant = await get_user(a["user_id"], tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == a["user_id"]

    leaked = await get_user(a["user_id"], tenant_id=b["tenant_id"])
    assert leaked is None, "get_user leaked tenant A's user to tenant B"


async def test_get_coworker_rejects_mismatched_tenant() -> None:
    """get_coworker is the most-frequented by-id call. Cross-tenant
    leakage here would cascade into every endpoint going through
    ``_get_agent_or_404``."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    same_tenant = await get_coworker(a["coworker_id"], tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == a["coworker_id"]

    leaked = await get_coworker(a["coworker_id"], tenant_id=b["tenant_id"])
    assert leaked is None, "get_coworker leaked tenant A's coworker to tenant B"


async def test_get_channel_binding_rejects_mismatched_tenant() -> None:
    """A binding's id must not be resolvable from another tenant."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]
    # _two_tenants doesn't return binding_id; recreate one by lookup.
    # The tenant chain already created a binding — fish it back via a
    # fresh insert on tenant A so the test is self-contained.
    binding = await create_channel_binding(
        coworker_id=a["coworker_id"],
        tenant_id=a["tenant_id"],
        channel_type="slack",
        credentials={"bot_token": "x"},
    )

    same_tenant = await get_channel_binding(binding.id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == binding.id

    leaked = await get_channel_binding(binding.id, tenant_id=b["tenant_id"])
    assert leaked is None, (
        "get_channel_binding leaked tenant A's binding to tenant B"
    )


async def test_get_conversation_rejects_mismatched_tenant() -> None:
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    same_tenant = await get_conversation(
        a["conversation_id"], tenant_id=a["tenant_id"]
    )
    assert same_tenant is not None
    assert same_tenant.id == a["conversation_id"]

    leaked = await get_conversation(
        a["conversation_id"], tenant_id=b["tenant_id"]
    )
    assert leaked is None, (
        "get_conversation leaked tenant A's conversation to tenant B"
    )


async def test_get_task_by_id_rejects_mismatched_tenant() -> None:
    """ScheduledTask.id is globally unique. Cross-tenant fetches must
    still return None — covers task IPC path that takes task_id from
    a NATS payload."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=a["tenant_id"],
            coworker_id=a["coworker_id"],
            prompt="ping",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2030-01-01T00:00:00+00:00",
            status="active",
        )
    )

    same_tenant = await get_task_by_id(task_id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == task_id

    leaked = await get_task_by_id(task_id, tenant_id=b["tenant_id"])
    assert leaked is None, "get_task_by_id leaked tenant A's task to tenant B"


async def test_get_safety_decision_rejects_mismatched_tenant() -> None:
    """Already enforced pre-PR-A but now via kwarg-only signature.
    A regression that flips it back to positional ``(decision_id,
    tenant_id)`` would compile against the old call sites that pass
    only ``decision_id`` and silently bypass the filter."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    decision_id = await insert_safety_decision(
        tenant_id=a["tenant_id"],
        stage="pre_tool_call",
        verdict_action="block",
        triggered_rule_ids=[],
        findings=[],
        context_digest="d",
        context_summary="s",
    )

    same_tenant = await get_safety_decision(decision_id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert str(same_tenant["id"]) == decision_id

    leaked = await get_safety_decision(decision_id, tenant_id=b["tenant_id"])
    assert leaked is None, (
        "get_safety_decision leaked tenant A's decision to tenant B"
    )


# ---------------------------------------------------------------------------
# PR-A: by-id write paths must also enforce tenant scope
# ---------------------------------------------------------------------------


async def test_update_user_does_not_modify_other_tenant() -> None:
    """update_user with tenant_id=B for a tenant-A user_id must NOT
    update the row. Without the SQL ``AND tenant_id`` filter, the
    UPDATE would happily mutate another tenant's user record."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    result = await update_user(
        a["user_id"], tenant_id=b["tenant_id"], name="HIJACKED"
    )
    assert result is None, "update_user returned a row for cross-tenant id"

    # Verify the actual row in tenant A is untouched.
    a_user = await get_user(a["user_id"], tenant_id=a["tenant_id"])
    assert a_user is not None
    assert a_user.name != "HIJACKED", (
        "update_user mutated tenant A's user when called with tenant B's id"
    )


# ---------------------------------------------------------------------------
# PR-A: resolve_user_for_auth (admin escape hatch)
# ---------------------------------------------------------------------------


async def test_resolve_user_for_auth_returns_tenant_and_role() -> None:
    """The auth bootstrap path needs (tenant_id, role) from a JWT
    user_id alone. Verifies shape and content."""
    tenants = await _two_tenants()
    a = tenants["A"]

    resolved = await resolve_user_for_auth(a["user_id"])
    assert resolved is not None
    tenant_id, role = resolved
    assert tenant_id == a["tenant_id"]
    assert role == "owner"  # set by _two_tenants


async def test_resolve_user_for_auth_returns_none_for_missing_id() -> None:
    """A nonexistent user_id must return None — JWT replay against a
    deleted user must not crash or leak."""
    bogus = str(uuid.uuid4())
    resolved = await resolve_user_for_auth(bogus)
    assert resolved is None
