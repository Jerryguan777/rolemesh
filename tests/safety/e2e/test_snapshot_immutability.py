"""Snapshot-immutability contract test (reflection #2).

The design doc §5.1 table says hot-update semantics is "next job
effective" — meaning the snapshot taken at container start is the
snapshot the container uses until it restarts. This is the pillar
that makes rule toggling safe: an admin disabling a rule cannot
mid-turn change what the agent sees, because a running container
holds its own list reference.

Pinning this as a contract test — if a future refactor moves to
"pipeline re-queries DB per tool call" the feature flip would be
silent. A hot-update-on-every-call design is tempting (no container
restart needed!) but has two insidious downsides:

  1. A DB outage would fail every tool call closed, not just fresh
     container starts — far bigger blast radius.
  2. An admin toggling a rule off mid-request would leave "half
     enforcement" behind: some of the tool_input fields already
     scanned, some not.

Both of these are detectable only via this test, because unit tests
pass fake rules directly into pipeline_run and never touch DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_container_registry
from rolemesh.db import pg

pytestmark = pytest.mark.usefixtures("test_db")


@dataclass
class _FakeToolCtx:
    tenant_id: str
    coworker_id: str
    job_id: str = "job-immut"
    conversation_id: str = "conv-immut"
    user_id: str = "user-immut"
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))


async def _seed(patterns: dict[str, bool] | None = None) -> tuple[str, str, str]:
    tenant = await pg.create_tenant(
        name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
    )
    cw = await pg.create_coworker(
        tenant_id=tenant.id, name="cw",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    rule = await pg.create_safety_rule(
        tenant_id=tenant.id,
        stage="pre_tool_call",
        check_id="pii.regex",
        config={"patterns": patterns or {"SSN": True}},
    )
    return tenant.id, cw.id, rule.id


async def _run_ssn_against(handler: SafetyHookHandler) -> bool:
    """True if the handler blocked an SSN-bearing tool call."""
    verdict = await handler.on_pre_tool_use(
        ToolCallEvent(
            tool_name="github__create_issue",
            tool_input={"body": "SSN 123-45-6789"},
        )
    )
    return bool(verdict and verdict.block)


class TestSnapshotImmutability:
    @pytest.mark.asyncio
    async def test_handler_is_unaffected_by_mid_run_db_changes(self) -> None:
        """An existing Handler MUST continue to block after the admin
        disables the rule. The Handler's internal rules list is the
        snapshot it was born with — re-querying DB per tool call
        would undermine the "next-job" hot-update promise.
        """
        tid, cwid, rule_id = await _seed()

        # Load snapshot into Handler A — represents a long-running
        # container holding a rules reference.
        rows_a = await pg.list_safety_rules_for_coworker(tid, cwid)
        snapshot_a = [r.to_snapshot_dict() for r in rows_a]
        handler_a = SafetyHookHandler(
            rules=snapshot_a,
            registry=build_container_registry(),
            tool_ctx=_FakeToolCtx(tenant_id=tid, coworker_id=cwid),  # type: ignore[arg-type]
        )
        assert await _run_ssn_against(handler_a), "initial SSN must block"

        # Admin disables the rule mid-flight.
        await pg.update_safety_rule(rule_id, enabled=False)

        # Handler A is UNCHANGED — still blocks. This is the core
        # contract: the snapshot is immutable until container restart.
        assert await _run_ssn_against(handler_a), (
            "handler A must keep blocking — its snapshot predates the "
            "DB update; hot-update is next-job, not mid-run"
        )

        # A fresh Handler B (simulating a container restart) sees the
        # new empty snapshot and no longer blocks.
        rows_b = await pg.list_safety_rules_for_coworker(tid, cwid)
        snapshot_b = [r.to_snapshot_dict() for r in rows_b]
        handler_b = SafetyHookHandler(
            rules=snapshot_b,
            registry=build_container_registry(),
            tool_ctx=_FakeToolCtx(tenant_id=tid, coworker_id=cwid),  # type: ignore[arg-type]
        )
        assert not await _run_ssn_against(handler_b), (
            "handler B started after the disable — must allow"
        )

    @pytest.mark.asyncio
    async def test_new_rule_not_visible_to_running_handler(self) -> None:
        """Converse: an admin ADDING a rule after container start
        must not start blocking in the running handler. Symmetry
        matters — hot-update in only one direction would be a footgun.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        # Start with no rules.
        rows_empty = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        assert rows_empty == []
        handler = SafetyHookHandler(
            rules=[],
            registry=build_container_registry(),
            tool_ctx=_FakeToolCtx(
                tenant_id=tenant.id, coworker_id=cw.id
            ),  # type: ignore[arg-type]
        )
        assert not await _run_ssn_against(handler)

        # Admin adds a blocking rule.
        await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )

        # Handler still has no rules in its list — must continue to allow.
        assert not await _run_ssn_against(handler), (
            "handler must not see rules added after its birth"
        )

    @pytest.mark.asyncio
    async def test_snapshot_dict_is_not_a_db_view(self) -> None:
        """Concretely: the snapshot dicts the Handler stores must be
        plain dicts, not objects that lazily hit DB. If Rule.to_
        snapshot_dict ever returned something like a proxy that
        re-queried on access, the immutability guarantee collapses.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        rule = await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        rows = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        snapshot = [r.to_snapshot_dict() for r in rows]

        # Delete the rule in DB.
        await pg.delete_safety_rule(rule.id)

        # Snapshot dicts must still carry the old data.
        assert snapshot[0]["check_id"] == "pii.regex"
        assert snapshot[0]["enabled"] is True
        assert snapshot[0]["config"] == {"patterns": {"SSN": True}}
