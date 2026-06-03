"""DB tests for Platform Safety Rules (cross-tenant, platform-owned).

Hits a real Postgres testcontainer (``test_db``), so schema drift and
grant/visibility behavior show up immediately. Exercises:

  - the 5 default-tier rules are seeded idempotently;
  - snapshots stamp the running job's tenant_id and null the coworker;
  - the loader merges platform rules into EVERY tenant's snapshot
    (cross-tenant), and tenants cannot edit them (separate table);
  - tier visibility: floor rules enforce but are never surfaced to
    tenants, default / transparent_floor are;
  - ``safety_decisions.source`` is 'platform' when a triggered rule is a
    platform rule, else 'tenant'.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.db import (
    admin_conn,
    create_coworker,
    create_safety_rule,
    create_tenant,
    fetch_platform_rule_snapshots,
    insert_safety_decision,
    list_safety_decisions,
    list_visible_platform_rules,
)
from rolemesh.db.platform_safety import VISIBLE_TIERS
from rolemesh.safety import loader

pytestmark = pytest.mark.usefixtures("test_db")

_EXPECTED_DEFAULT_CHECKS = {
    "secret_scanner",
    "pii.regex",
    "llm_guard.prompt_injection",
    "llm_guard.jailbreak",
    "llm_guard.toxicity",
}


async def _tenant_and_coworker(slug: str) -> tuple[str, str]:
    t = await create_tenant(name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    return t.id, cw.id


async def _insert_floor_rule() -> None:
    """Seed one floor-tier rule directly (no write helper this phase)."""
    async with admin_conn() as conn:
        await conn.execute(
            "INSERT INTO platform_safety_rules (tier, stage, check_id) "
            "VALUES ('floor', 'input_prompt', 'pii.regex') "
            "ON CONFLICT (tier, check_id, stage) DO NOTHING"
        )


class TestSeed:
    @pytest.mark.asyncio
    async def test_five_default_rules_seeded(self) -> None:
        tid, _ = await _tenant_and_coworker("seed")
        snaps = await fetch_platform_rule_snapshots(tid)
        assert {s["check_id"] for s in snaps} == _EXPECTED_DEFAULT_CHECKS
        assert all(s["enabled"] for s in snaps)

    @pytest.mark.asyncio
    async def test_snapshots_stamp_tenant_and_null_coworker(self) -> None:
        tid, _ = await _tenant_and_coworker("stamp")
        snaps = await fetch_platform_rule_snapshots(tid)
        assert snaps
        assert all(s["tenant_id"] == tid for s in snaps)
        assert all(s["coworker_id"] is None for s in snaps)
        # Snapshot shape is exactly the pipeline contract.
        expected_keys = {
            "id", "tenant_id", "coworker_id", "stage", "check_id",
            "config", "priority", "enabled", "description",
        }
        assert all(set(s.keys()) == expected_keys for s in snaps)


class TestVisibility:
    @pytest.mark.asyncio
    async def test_floor_enforces_but_is_invisible(self) -> None:
        tid, _ = await _tenant_and_coworker("vis")
        await _insert_floor_rule()

        # Floor IS injected into the enforcement snapshot...
        snaps = await fetch_platform_rule_snapshots(tid)
        assert any(
            s["stage"] == "input_prompt" and s["check_id"] == "pii.regex"
            for s in snaps
        )
        # ...but is NEVER surfaced to the tenant.
        visible = await list_visible_platform_rules(tid)
        assert visible
        assert all(row["tier"] in VISIBLE_TIERS for row in visible)
        assert all(row["tier"] != "floor" for row in visible)


class TestCrossTenant:
    @pytest.mark.asyncio
    async def test_platform_rules_apply_to_every_tenant(self) -> None:
        ta, ca = await _tenant_and_coworker("xa")
        tb, cb = await _tenant_and_coworker("xb")

        snaps_a = await loader.fetch_safety_rule_snapshots(ta, ca)
        snaps_b = await loader.fetch_safety_rule_snapshots(tb, cb)

        plat_a = {s["id"] for s in snaps_a if s["coworker_id"] is None
                  and s["check_id"] in _EXPECTED_DEFAULT_CHECKS}
        plat_b = {s["id"] for s in snaps_b if s["coworker_id"] is None
                  and s["check_id"] in _EXPECTED_DEFAULT_CHECKS}
        # Same platform rule rows reach both tenants (cross-tenant).
        assert plat_a == plat_b
        assert len(plat_a) == len(_EXPECTED_DEFAULT_CHECKS)

    @pytest.mark.asyncio
    async def test_tenant_rules_merge_alongside_platform(self) -> None:
        tid, cw = await _tenant_and_coworker("merge")
        own = await create_safety_rule(
            tenant_id=tid,
            stage="input_prompt",
            check_id="pii.regex",
            config={"patterns": {"EMAIL": True}},
        )
        snaps = await loader.fetch_safety_rule_snapshots(tid, cw)
        ids = {s["id"] for s in snaps}
        # Tenant rule AND the 5 platform rules are all present.
        assert own.id in ids
        assert len(ids) >= len(_EXPECTED_DEFAULT_CHECKS) + 1


class TestDecisionSource:
    @pytest.mark.asyncio
    async def test_source_platform_when_triggered_rule_is_platform(self) -> None:
        tid, _ = await _tenant_and_coworker("srcp")
        snaps = await fetch_platform_rule_snapshots(tid)
        platform_rule_id = snaps[0]["id"]

        decision_id = await insert_safety_decision(
            tenant_id=tid,
            stage="model_output",
            verdict_action="block",
            triggered_rule_ids=[platform_rule_id],
            findings=[],
            context_digest="d",
            context_summary="s",
        )
        rows = await list_safety_decisions(tid)
        row = next(r for r in rows if r["id"] == decision_id)
        assert row["source"] == "platform"

    @pytest.mark.asyncio
    async def test_source_tenant_for_non_platform_rule(self) -> None:
        tid, _ = await _tenant_and_coworker("srct")
        decision_id = await insert_safety_decision(
            tenant_id=tid,
            stage="input_prompt",
            verdict_action="block",
            triggered_rule_ids=[str(uuid.uuid4())],
            findings=[],
            context_digest="d",
            context_summary="s",
        )
        rows = await list_safety_decisions(tid)
        row = next(r for r in rows if r["id"] == decision_id)
        assert row["source"] == "tenant"
