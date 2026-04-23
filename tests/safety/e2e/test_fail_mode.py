"""SAFETY_FAIL_MODE and zero-cost registration E2E.

Two behaviours the V1 acceptance spec (§5.12 non-functional) requires
but the original E2E suite did not exercise:

  - DB unreachable at job start with SAFETY_FAIL_MODE=closed (default)
    MUST refuse to start the agent — the exception propagates so the
    executor aborts. This is the fail-close posture; without this
    test, a refactor that quietly changed the default to fail-open
    would ship unnoticed.

  - DB unreachable with SAFETY_FAIL_MODE=open degrades to running
    without rules and logs an ERROR/WARNING — operators need a
    beacon, not silence.

  - When safety_rules is None/empty, SafetyHookHandler is NOT
    registered — the 'safety module absent' hook chain is byte-
    identical to the pre-safety baseline. This is the "zero-cost"
    invariant; a regression here would add per-tool-call cost to
    every agent turn across every deployment that does not use the
    safety module.

We cover both via the single loader module — the V1 hotfix extracted
these two blocks out of container_executor and agent_runner/main so
they have one source of truth + one set of tests, rather than
duplicating the logic across two inlined-in-large-functions sites.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.registry import HookRegistry
from rolemesh.db import pg
from rolemesh.safety import loader as loader_mod
from rolemesh.safety.loader import (
    load_safety_rules_snapshot,
    maybe_register_safety_handler,
)

pytestmark = pytest.mark.usefixtures("test_db")


@dataclass
class _FakeToolCtx:
    tenant_id: str
    coworker_id: str
    job_id: str = "job-fm"
    conversation_id: str = "conv-fm"
    user_id: str = "user-fm"
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))


# ---------------------------------------------------------------------------
# SAFETY_FAIL_MODE
# ---------------------------------------------------------------------------


class TestFailModeStartup:
    """DB-unreachable at snapshot load time — the loader must behave
    differently based on SAFETY_FAIL_MODE. The loader is small enough
    to exercise directly; we monkeypatch the DB function and the env
    config symbol the loader imports.
    """

    @pytest.mark.asyncio
    async def test_fail_closed_is_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: if someone flips the default, the first line of
        # this test pins the current behaviour. Check against the
        # imported module so reload semantics are correct.
        from rolemesh.core import config as cfg

        assert cfg.SAFETY_FAIL_MODE == "closed", (
            "SAFETY_FAIL_MODE default MUST remain 'closed' — changing "
            "this is a silent downgrade of the security posture"
        )

    @pytest.mark.asyncio
    async def test_fail_closed_raises_on_db_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ConnectionError is in _DB_UNREACHABLE_EXCEPTIONS; the test
        # used to raise RuntimeError but that class is now (correctly)
        # NOT in the catch tuple — programmer errors should not be
        # masked as DB outages. Use a class the fail-mode dispatch
        # actually handles.
        async def _explode(*_a: Any, **_kw: Any) -> Any:
            raise ConnectionError("simulated DB outage")

        # Patch the name the loader's function body reads. The loader
        # imports ``list_safety_rules_for_coworker`` at module top and
        # binds it locally, so monkeypatching pg.list_safety_rules_for_
        # coworker only is not picked up. This is the correct Python
        # idiom for stubbing dependencies in the caller's namespace.
        monkeypatch.setattr(
            loader_mod, "list_safety_rules_for_coworker", _explode
        )
        monkeypatch.setattr(
            "rolemesh.core.config.SAFETY_FAIL_MODE", "closed"
        )

        with pytest.raises(ConnectionError, match="simulated DB outage"):
            await load_safety_rules_snapshot("tenant-x", "cw-y")

    @pytest.mark.asyncio
    async def test_fail_open_returns_none_on_db_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _explode(*_a: Any, **_kw: Any) -> Any:
            raise ConnectionError("no connection to postgres")

        # See test_fail_closed_raises_on_db_error for why we patch the
        # name on loader_mod rather than pg.
        monkeypatch.setattr(
            loader_mod, "list_safety_rules_for_coworker", _explode
        )
        monkeypatch.setattr(
            "rolemesh.core.config.SAFETY_FAIL_MODE", "open"
        )

        # Spy on the loader's bound logger — structlog's cached-logger
        # behaviour makes stderr capture flaky under pytest, and
        # asserting at the log-call boundary is a tighter contract
        # anyway (tests the intent, not the formatting).
        warnings: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            loader_mod.logger,
            "warning",
            lambda msg, **kw: warnings.append((msg, kw)),
        )

        result = await load_safety_rules_snapshot("tenant-x", "cw-y")
        # No exception. Agent can boot without rules.
        assert result is None
        # Logs must make the degradation visible — silent fail-open
        # is unacceptable, operators need a WARNING beacon.
        assert warnings, "fail-open path must emit a WARNING log"
        msg, kwargs = warnings[0]
        assert "SAFETY_FAIL_MODE=open" in msg or "DB unreachable" in msg
        assert kwargs.get("coworker_id") == "cw-y"
        assert "error" in kwargs  # the root cause travels with the log

    @pytest.mark.asyncio
    async def test_fail_closed_also_logs_before_raising(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Symmetry: even though fail-closed raises, it must log the
        # reason at ERROR first so the operator can correlate the
        # "agent not responding" symptom with the DB outage cause.
        async def _explode(*_a: Any, **_kw: Any) -> Any:
            raise ConnectionError("simulated DB outage for closed path")

        monkeypatch.setattr(
            loader_mod, "list_safety_rules_for_coworker", _explode
        )
        monkeypatch.setattr(
            "rolemesh.core.config.SAFETY_FAIL_MODE", "closed"
        )

        errors: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            loader_mod.logger,
            "error",
            lambda msg, **kw: errors.append((msg, kw)),
        )

        with pytest.raises(ConnectionError):
            await load_safety_rules_snapshot("tenant-x", "cw-y")

        assert errors, "fail-closed must log the reason before raising"
        msg, kwargs = errors[0]
        assert "SAFETY_FAIL_MODE=closed" in msg or "refusing" in msg
        assert kwargs.get("coworker_id") == "cw-y"

    @pytest.mark.asyncio
    async def test_non_db_errors_bypass_fail_mode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The fail-mode dispatch MUST NOT catch programmer errors
        # (assert failures, typos surfacing as AttributeError, etc.)
        # and mislabel them as "DB unreachable". The except is narrowed
        # to _DB_UNREACHABLE_EXCEPTIONS specifically to surface such
        # bugs as themselves rather than degrading into silent
        # fail-open / noisy fail-close.
        async def _programmer_error(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("db pool used before init_database()")

        monkeypatch.setattr(
            loader_mod, "list_safety_rules_for_coworker",
            _programmer_error,
        )
        # Even under fail-open, a programmer error must propagate —
        # otherwise the module silently degrades for reasons that are
        # not DB outages at all.
        monkeypatch.setattr(
            "rolemesh.core.config.SAFETY_FAIL_MODE", "open"
        )

        with pytest.raises(AssertionError, match="pool used before"):
            await load_safety_rules_snapshot("tenant-x", "cw-y")

    @pytest.mark.asyncio
    async def test_happy_path_returns_snapshot_dicts(self) -> None:
        # Control case: with a live DB and at least one rule, the
        # loader returns the snapshot-ready dict list (not Rule
        # objects, because the container deserializes from JSON).
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        result = await load_safety_rules_snapshot(tenant.id, cw.id)
        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], dict)
        assert result[0]["check_id"] == "pii.regex"

    @pytest.mark.asyncio
    async def test_no_rules_returns_none_not_empty_list(self) -> None:
        # Semantically None is "no safety module active", empty list
        # is "module active but no rules". Loader returns None so the
        # registration guard skips cleanly in either case; test pins
        # this contract because the guard checks `if safety_rules:`,
        # which treats None and [] the same today but could diverge
        # in a future refactor.
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        assert await load_safety_rules_snapshot(tenant.id, cw.id) is None


# ---------------------------------------------------------------------------
# Zero-cost registration guard
# ---------------------------------------------------------------------------


class TestRegistrationGuard:
    """maybe_register_safety_handler must add exactly zero handlers
    when no rules apply. Previously this was an inlined ``if
    init.safety_rules:`` in run_query_loop — too nested for tests to
    reach, so the "zero-cost when unconfigured" claim was untested.
    """

    def test_none_registers_no_handler(self) -> None:
        hook_registry = HookRegistry()
        tool_ctx = _FakeToolCtx(tenant_id="t", coworker_id="c")
        registered = maybe_register_safety_handler(
            hook_registry=hook_registry,
            safety_rules=None,
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        assert registered is False
        assert bool(hook_registry) is False, (
            "None rules MUST keep the hook registry empty — this is "
            "the zero-cost invariant for deployments without the "
            "safety module"
        )

    def test_empty_list_registers_no_handler(self) -> None:
        # Empty list is treated identically to None. A snapshot
        # producer that accidentally emits [] instead of None must
        # not flip the hook chain into "registered but inert" — the
        # two states have different exception-propagation profiles.
        hook_registry = HookRegistry()
        tool_ctx = _FakeToolCtx(tenant_id="t", coworker_id="c")
        registered = maybe_register_safety_handler(
            hook_registry=hook_registry,
            safety_rules=[],
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        assert registered is False
        assert bool(hook_registry) is False

    def test_non_empty_list_registers_handler(self) -> None:
        hook_registry = HookRegistry()
        tool_ctx = _FakeToolCtx(tenant_id="t", coworker_id="c")
        rule = {
            "id": "r1",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "pre_tool_call",
            "check_id": "pii.regex",
            "config": {"patterns": {"SSN": True}},
            "priority": 100,
            "enabled": True,
            "description": "",
        }
        registered = maybe_register_safety_handler(
            hook_registry=hook_registry,
            safety_rules=[rule],
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        assert registered is True
        assert bool(hook_registry) is True

    def test_handler_wired_to_pipeline(self) -> None:
        # Deeper than "a handler exists": the registered handler must
        # actually fire on PreToolUse. If someone registered the
        # wrong object (e.g. a no-op) this catches it.
        import asyncio

        from agent_runner.hooks.events import ToolCallEvent

        hook_registry = HookRegistry()
        tool_ctx = _FakeToolCtx(tenant_id="t", coworker_id="c")
        rule = {
            "id": "r1",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "pre_tool_call",
            "check_id": "pii.regex",
            "config": {"patterns": {"SSN": True}},
            "priority": 100,
            "enabled": True,
            "description": "",
        }
        maybe_register_safety_handler(
            hook_registry=hook_registry,
            safety_rules=[rule],
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )

        verdict = asyncio.run(
            hook_registry.emit_pre_tool_use(
                ToolCallEvent(
                    tool_name="x__y",
                    tool_input={"body": "SSN 123-45-6789"},
                )
            )
        )
        assert verdict is not None and verdict.block
