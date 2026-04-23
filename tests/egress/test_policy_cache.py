"""Tests for the gateway's in-memory egress-rule cache."""

from __future__ import annotations

import pytest

from rolemesh.egress.policy_cache import CachedRule, PolicyCache

pytestmark = pytest.mark.asyncio


def _make_rule(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "rule-1",
        "rule_id": "rule-1",
        "tenant_id": "tenant-a",
        "coworker_id": "coworker-x",
        "stage": "egress_request",
        "check_id": "egress.domain_rule",
        "config": {"domain_pattern": "api.anthropic.com"},
        "priority": 100,
        "enabled": True,
    }
    base.update(overrides)
    return base


async def test_seed_populates_cache() -> None:
    cache = PolicyCache()
    await cache.seed([_make_rule()])
    rules = cache.get_rules_for("tenant-a", "coworker-x")
    assert len(rules) == 1
    assert rules[0].id == "rule-1"


async def test_seed_skips_disabled() -> None:
    """Disabled rules must not enter the cache — they'd otherwise burn
    a check call on the hot path for zero benefit."""
    cache = PolicyCache()
    await cache.seed([_make_rule(id="rule-2", rule_id="rule-2", enabled=False)])
    assert cache.get_rules_for("tenant-a", "coworker-x") == []


async def test_tenant_wide_rule_merged_with_coworker_scope() -> None:
    cache = PolicyCache()
    await cache.seed(
        [
            _make_rule(id="tw", rule_id="tw", coworker_id=None, priority=50),
            _make_rule(id="cs", rule_id="cs", priority=200),
        ]
    )
    rules = cache.get_rules_for("tenant-a", "coworker-x")
    assert [r.id for r in rules] == ["cs", "tw"], (
        "higher-priority rule must come first"
    )


async def test_apply_event_created_inserts() -> None:
    cache = PolicyCache()
    await cache.apply_event({"action": "created", **_make_rule(id="new-rule", rule_id="new-rule")})
    rules = cache.get_rules_for("tenant-a", "coworker-x")
    assert rules[0].id == "new-rule"


async def test_apply_event_deleted_removes() -> None:
    cache = PolicyCache()
    await cache.seed([_make_rule()])
    await cache.apply_event({"action": "deleted", "rule_id": "rule-1"})
    assert cache.get_rules_for("tenant-a", "coworker-x") == []


async def test_apply_event_updated_replaces_in_place() -> None:
    """Update must replace, not duplicate — otherwise the cache grows unbounded."""
    cache = PolicyCache()
    await cache.seed([_make_rule(priority=100)])
    await cache.apply_event(
        {"action": "updated", **_make_rule(priority=500)}
    )
    rules = cache.get_rules_for("tenant-a", "coworker-x")
    assert len(rules) == 1
    assert rules[0].priority == 500


async def test_apply_event_disabled_is_treated_as_delete() -> None:
    cache = PolicyCache()
    await cache.seed([_make_rule()])
    await cache.apply_event(
        {"action": "updated", **_make_rule(enabled=False)}
    )
    assert cache.get_rules_for("tenant-a", "coworker-x") == []


async def test_malformed_event_does_not_crash_cache() -> None:
    """One bad event must not poison the cache — fail-safe on deltas."""
    cache = PolicyCache()
    await cache.seed([_make_rule()])
    await cache.apply_event({"action": "created"})  # missing rule_id
    # Existing state survives.
    assert len(cache.get_rules_for("tenant-a", "coworker-x")) == 1


async def test_cached_rule_carries_config() -> None:
    cache = PolicyCache()
    await cache.seed(
        [_make_rule(config={"domain_pattern": "*.github.com", "ports": [443]})]
    )
    rule = cache.get_rules_for("tenant-a", "coworker-x")[0]
    assert isinstance(rule, CachedRule)
    assert rule.config == {"domain_pattern": "*.github.com", "ports": [443]}
