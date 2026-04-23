"""Tests for CheckRegistry and the default registry builder.

Covers:
  - duplicate register raises (programming error)
  - get raises UnknownCheckError for missing ids
  - build_default_registry includes pii.regex exactly once
  - container-side re-export is object-identical (no code drift)
"""

from __future__ import annotations

from typing import Any

import pytest

from rolemesh.safety.checks.pii_regex import PIIRegexCheck
from rolemesh.safety.errors import UnknownCheckError
from rolemesh.safety.registry import (
    CheckRegistry,
    build_container_registry,
    build_orchestrator_registry,
    get_orchestrator_registry,
    reset_orchestrator_registry,
)
from rolemesh.safety.types import CostClass, SafetyContext, Stage, Verdict


class _StubCheck:
    id: str = "stub.noop"
    version: str = "1"
    stages: frozenset[Stage] = frozenset({Stage.PRE_TOOL_CALL})
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset({"STUB.NOOP"})

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


class TestCheckRegistry:
    def test_register_and_get(self) -> None:
        reg = CheckRegistry()
        check = _StubCheck()
        reg.register(check)
        assert reg.get("stub.noop") is check
        assert reg.has("stub.noop")

    def test_duplicate_register_raises(self) -> None:
        reg = CheckRegistry()
        reg.register(_StubCheck())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_StubCheck())

    def test_get_unknown_raises(self) -> None:
        reg = CheckRegistry()
        with pytest.raises(UnknownCheckError):
            reg.get("does.not.exist")

    def test_has_false_for_missing(self) -> None:
        reg = CheckRegistry()
        assert reg.has("nope") is False

    def test_ids_and_all(self) -> None:
        reg = CheckRegistry()
        c = _StubCheck()
        reg.register(c)
        assert reg.ids() == ["stub.noop"]
        assert reg.all() == [c]
        assert len(reg) == 1


class TestBuilders:
    def test_container_contains_pii_regex(self) -> None:
        reg = build_container_registry()
        assert reg.has("pii.regex")
        assert isinstance(reg.get("pii.regex"), PIIRegexCheck)

    def test_orchestrator_contains_pii_regex(self) -> None:
        reg = build_orchestrator_registry()
        assert reg.has("pii.regex")

    def test_default_cheap_checks_are_pii_regex_and_domain_allowlist(
        self,
    ) -> None:
        # Both registries ship the cheap-check set {pii.regex,
        # domain_allowlist}. Slow checks land in the orchestrator
        # registry as their optional deps get wired (P1.2+); keep
        # THIS assertion tight on the cheap-only surface so an
        # accidental drift (double registration, silent removal)
        # is caught.
        assert set(build_container_registry().ids()) == {
            "pii.regex",
            "domain_allowlist",
        }
        orch_ids = set(build_orchestrator_registry().ids())
        # Orchestrator is a superset of the container — never a
        # subset — because slow checks only live here.
        assert {"pii.regex", "domain_allowlist"}.issubset(orch_ids)


class TestOrchestratorSingleton:
    def test_returns_same_instance_across_calls(self) -> None:
        reset_orchestrator_registry()
        a = get_orchestrator_registry()
        b = get_orchestrator_registry()
        # The singleton guarantees heavy V2 check constructors (spaCy,
        # HuggingFace etc.) do not re-run per REST request.
        assert a is b

    def test_reset_forces_rebuild(self) -> None:
        reset_orchestrator_registry()
        a = get_orchestrator_registry()
        reset_orchestrator_registry()
        b = get_orchestrator_registry()
        assert a is not b


class TestContainerMirror:
    def test_container_registry_equal_ids_to_orchestrator_in_v1(self) -> None:
        # V1: container and orchestrator share the same check set.
        # This test MUST be updated (not silently extended) when V2
        # adds slow checks to orchestrator only.
        container_reg = build_container_registry()
        orch_reg = build_orchestrator_registry()
        assert set(container_reg.ids()) == set(orch_reg.ids())

    def test_container_pii_regex_import_same_class(self) -> None:
        # The §5.4 "no drift between sides" test: the container's
        # re-export MUST be the same class as the orchestrator's.
        from agent_runner.safety.checks.pii_regex import (
            PIIRegexCheck as ContainerPIIRegexCheck,
        )

        assert ContainerPIIRegexCheck is PIIRegexCheck
