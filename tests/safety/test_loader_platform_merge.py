"""Platform-rule merge in the loader + snapshot projection (DB-free).

The loader resolves both reader functions via ``globals()`` so they can
be monkeypatched without a live Postgres — that is the whole point of the
module-scope import dance. The projection helpers in
``rolemesh.db.platform_safety`` take plain dicts (``asyncpg.Record`` is
dict-like) so they are tested directly.

What these pin:
  - tenant rules come first, platform rules are appended (single seam);
  - a platform snapshot is byte-shaped exactly like a tenant snapshot
    (so the UNTOUCHED pipeline cannot tell them apart);
  - the running job's tenant_id is stamped on, coworker_id is None;
  - platform rules alone (zero tenant rules) still produce a non-None
    snapshot so the hook handler registers.
"""

from __future__ import annotations

import pytest

from rolemesh.db import platform_safety
from rolemesh.safety import loader
from rolemesh.safety.types import Rule, Stage


class _FakeTenantRule:
    """Minimal stand-in exposing ``to_snapshot_dict`` like safety.types.Rule."""

    def __init__(self, rule_id: str) -> None:
        self._rule_id = rule_id

    def to_snapshot_dict(self) -> dict[str, object]:
        return {
            "id": self._rule_id,
            "tenant_id": "T",
            "coworker_id": None,
            "stage": "input_prompt",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
            "description": "",
        }


def _platform_snapshot(rule_id: str, tenant_id: str) -> dict[str, object]:
    return {
        "id": rule_id,
        "tenant_id": tenant_id,
        "coworker_id": None,
        "stage": "model_output",
        "check_id": "llm_guard.toxicity",
        "config": {"threshold": 0.7},
        "priority": 1000,
        "enabled": True,
        "description": "tox",
    }


@pytest.mark.asyncio
async def test_fetch_merges_platform_after_tenant(monkeypatch) -> None:
    async def fake_tenant(tenant_id: str, coworker_id: str) -> list[_FakeTenantRule]:
        assert (tenant_id, coworker_id) == ("T", "C")
        return [_FakeTenantRule("tenant-rule-1")]

    async def fake_platform(tenant_id: str) -> list[dict[str, object]]:
        assert tenant_id == "T"
        return [_platform_snapshot("plat-1", tenant_id)]

    monkeypatch.setattr(loader, "list_safety_rules_for_coworker", fake_tenant)
    monkeypatch.setattr(loader, "fetch_platform_rule_snapshots", fake_platform)

    snaps = await loader.fetch_safety_rule_snapshots("T", "C")

    # Tenant rules first, platform appended — single, ordered seam.
    assert [s["id"] for s in snaps] == ["tenant-rule-1", "plat-1"]
    plat = snaps[-1]
    assert plat["coworker_id"] is None
    assert plat["tenant_id"] == "T"


@pytest.mark.asyncio
async def test_platform_only_snapshot_is_non_none(monkeypatch) -> None:
    """Zero tenant rules + ≥1 platform rule still registers the hook."""

    async def fake_tenant(tenant_id: str, coworker_id: str) -> list[_FakeTenantRule]:
        return []

    async def fake_platform(tenant_id: str) -> list[dict[str, object]]:
        return [_platform_snapshot("plat-1", tenant_id)]

    monkeypatch.setattr(loader, "list_safety_rules_for_coworker", fake_tenant)
    monkeypatch.setattr(loader, "fetch_platform_rule_snapshots", fake_platform)

    snaps = await loader.load_safety_rules_snapshot("T", "C")
    assert snaps is not None
    assert [s["id"] for s in snaps] == ["plat-1"]


@pytest.mark.asyncio
async def test_no_rules_anywhere_is_none(monkeypatch) -> None:
    async def fake_tenant(tenant_id: str, coworker_id: str) -> list[_FakeTenantRule]:
        return []

    async def fake_platform(tenant_id: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(loader, "list_safety_rules_for_coworker", fake_tenant)
    monkeypatch.setattr(loader, "fetch_platform_rule_snapshots", fake_platform)

    assert await loader.load_safety_rules_snapshot("T", "C") is None


def test_row_to_snapshot_matches_tenant_rule_shape() -> None:
    """A platform snapshot must be key-identical to a tenant snapshot so
    the pipeline (untouched) cannot distinguish their origin.
    """
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "stage": "input_prompt",
        "check_id": "pii.regex",
        "config": '{"patterns": {"SSN": true}}',  # JSON string from DB
        "priority": 1000,
        "enabled": True,
        "description": "x",
    }
    snap = platform_safety._row_to_snapshot(row, tenant_id="TEN")

    assert snap["tenant_id"] == "TEN"  # stamped for audit attribution
    assert snap["coworker_id"] is None  # applies to all coworkers
    assert snap["config"] == {"patterns": {"SSN": True}}  # str coerced to dict

    reference = Rule(
        id="i",
        tenant_id="TEN",
        coworker_id=None,
        stage=Stage.INPUT_PROMPT,
        check_id="pii.regex",
        config={},
    ).to_snapshot_dict()
    assert set(snap.keys()) == set(reference.keys())


def test_row_to_snapshot_passes_through_dict_config() -> None:
    row = {
        "id": "abc",
        "stage": "model_output",
        "check_id": "llm_guard.toxicity",
        "config": {"threshold": 0.7},  # already a dict
        "priority": 1000,
        "enabled": True,
        "description": "",
    }
    snap = platform_safety._row_to_snapshot(row, tenant_id="T")
    assert snap["config"] == {"threshold": 0.7}
