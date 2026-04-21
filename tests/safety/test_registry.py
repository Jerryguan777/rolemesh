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
from rolemesh.safety.registry import CheckRegistry, build_default_registry
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


class TestBuildDefaultRegistry:
    def test_contains_pii_regex(self) -> None:
        reg = build_default_registry()
        assert reg.has("pii.regex")
        assert isinstance(reg.get("pii.regex"), PIIRegexCheck)

    def test_only_default_check_in_v1(self) -> None:
        reg = build_default_registry()
        # V1 ships exactly one check. If this breaks because V2 added a
        # check, update the list — don't make this assertion lax.
        assert reg.ids() == ["pii.regex"]


class TestContainerMirror:
    def test_container_registry_is_same_default(self) -> None:
        from agent_runner.safety.registry import (
            build_default_registry as container_build,
        )

        orchestrator_reg = build_default_registry()
        container_reg = container_build()
        # Not object identity (fresh instances), but same check class
        # sets. A future V2 patch that accidentally wires a different
        # registry on one side will surface here.
        assert set(orchestrator_reg.ids()) == set(container_reg.ids())

    def test_container_pii_regex_import_same_class(self) -> None:
        # The §5.4 "no drift between sides" test: the container's
        # re-export MUST be the same class as the orchestrator's.
        from agent_runner.safety.checks.pii_regex import (
            PIIRegexCheck as ContainerPIIRegexCheck,
        )

        assert ContainerPIIRegexCheck is PIIRegexCheck
