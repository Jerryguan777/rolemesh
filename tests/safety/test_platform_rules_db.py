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
    create_platform_rule,
    create_safety_rule,
    create_tenant,
    delete_platform_rule,
    fetch_platform_rule_snapshots,
    get_platform_rule,
    insert_safety_decision,
    list_all_platform_rules,
    list_safety_decisions,
    list_visible_platform_rules,
    set_platform_rule_enabled,
    update_platform_rule,
)
from rolemesh.db.platform_safety import VISIBLE_TIERS
from rolemesh.db.schema import _seed_platform_safety_rules
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
    """Insert one floor-tier rule directly (bypassing the write API)."""
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


class TestWriteHelpers:
    @pytest.mark.asyncio
    async def test_create_sets_is_seeded_false_and_full_crud(self) -> None:
        await _tenant_and_coworker("crud")
        created = await create_platform_rule(
            tier="transparent_floor",
            stage="model_output",
            check_id="pii.regex",
            config={"patterns": {"EMAIL": True}},
            priority=200,
            description="pa rule",
        )
        assert created["is_seeded"] is False
        rid = created["id"]

        fetched = await get_platform_rule(rid)
        assert fetched is not None
        assert fetched["check_id"] == "pii.regex"

        updated = await update_platform_rule(
            rid, priority=5, enabled=False, description="edited",
        )
        assert updated is not None
        assert updated["priority"] == 5
        assert updated["enabled"] is False
        assert updated["description"] == "edited"

        toggled = await set_platform_rule_enabled(rid, enabled=True)
        assert toggled is not None and toggled["enabled"] is True

        assert await delete_platform_rule(rid) is True
        assert await get_platform_rule(rid) is None
        # Deleting a now-absent id is a no-op (False).
        assert await delete_platform_rule(rid) is False

    @pytest.mark.asyncio
    async def test_list_all_returns_floor_tier(self) -> None:
        await _tenant_and_coworker("listall")
        await _insert_floor_rule()
        rows = await list_all_platform_rules()
        # Unlike list_visible_platform_rules, floor IS surfaced here.
        assert any(r["tier"] == "floor" for r in rows)
        # The 5 seeded defaults are present and flagged.
        seeded = {r["check_id"] for r in rows if r["is_seeded"]}
        assert seeded >= _EXPECTED_DEFAULT_CHECKS


class TestSeedSurvival:
    @pytest.mark.asyncio
    async def test_seed_does_not_overwrite_pa_edits(self) -> None:
        """A re-seed (fresh-DB rebuild) must not clobber a PA's edits.

        The seed is ON CONFLICT DO UPDATE that touches ONLY is_seeded, so a
        disabled / re-configured factory default keeps the operator's state
        across the next seed run.
        """
        await _tenant_and_coworker("survive")
        rows = await list_all_platform_rules()
        seeded = next(r for r in rows if r["is_seeded"])
        rid = seeded["id"]

        await update_platform_rule(
            rid, config={"sentinel": True}, enabled=False, priority=42,
        )

        # Re-run the build-time seed (simulates a fresh-DB rebuild / reseed).
        async with admin_conn() as conn:
            await _seed_platform_safety_rules(conn)

        after = await get_platform_rule(rid)
        assert after is not None
        assert after["enabled"] is False  # disable survives
        assert after["config"] == {"sentinel": True}  # config edit survives
        assert after["priority"] == 42  # priority edit survives
        assert after["is_seeded"] is True  # still a managed default
