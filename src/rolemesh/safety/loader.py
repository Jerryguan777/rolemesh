"""Orchestrator-side snapshot loader + container-side registration guard.

Both helpers live here so they have a single, testable definition. The
previous V1 shipped these as inlined blocks inside
``container_executor.execute()`` and ``agent_runner/main.run_query_loop``
— which meant the fail-mode branch and the zero-cost registration
branch both bypassed unit tests (the enclosing functions are too
broad to instantiate in isolation).

Keeping the two helpers together here (rather than co-locating each
near its caller) is deliberate: they both enforce the "no rules →
zero overhead" invariant from different sides, and pairing them in
one file makes any future divergence visible in one diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from agent_runner.hooks.registry import HookRegistry
    from agent_runner.tools.context import ToolContext


logger = get_logger()


async def load_safety_rules_snapshot(
    tenant_id: str, coworker_id: str
) -> list[dict[str, Any]] | None:
    """Return a snapshot-ready rules list, or None if none apply.

    Fail-mode contract (``SAFETY_FAIL_MODE`` env var, default=closed):

      - ``closed`` — DB unreachable at load time raises, caller (the
        container_executor) aborts the job. This matches the fail-
        close posture of the hook layer: a safety module outage MUST
        NOT silently let every tool call run unsupervised.

      - ``open``  — DB unreachable returns None, agent starts without
        rules but a loud ERROR log surfaces to operators. Acceptable
        for self-hosted deployments that prefer agent availability
        over safety coverage during an incident.

    Returns None in two cases that look identical to the caller:
    genuinely no rules configured, OR fail-open fallback after a DB
    error. Distinguishing those in callers is never useful — both
    mean "run the agent without the safety hook".
    """
    try:
        from rolemesh.db.pg import list_safety_rules_for_coworker

        rules = await list_safety_rules_for_coworker(
            tenant_id, coworker_id
        )
        if not rules:
            return None
        return [r.to_snapshot_dict() for r in rules]
    except Exception as exc:
        # Import here so test monkeypatch on the env var lands on the
        # same symbol the code reads.
        from rolemesh.core.config import SAFETY_FAIL_MODE

        if SAFETY_FAIL_MODE == "open":
            logger.warning(
                "safety: DB unreachable — starting agent without "
                "rules (SAFETY_FAIL_MODE=open).",
                coworker_id=coworker_id,
                error=str(exc),
            )
            return None
        logger.error(
            "safety: DB unreachable at job start — refusing to "
            "start agent (SAFETY_FAIL_MODE=closed). Set "
            "SAFETY_FAIL_MODE=open to permit fail-open startup.",
            coworker_id=coworker_id,
            error=str(exc),
        )
        raise


def maybe_register_safety_handler(
    hook_registry: HookRegistry,
    safety_rules: list[dict[str, Any]] | None,
    tool_ctx: ToolContext,
) -> bool:
    """Register a SafetyHookHandler iff rules are present.

    The zero-overhead invariant: when no rules apply, the hook
    chain stays byte-identical to pre-safety builds. Returns True
    when a handler was registered, False otherwise — the return
    value exists solely to make the registration decision testable
    without a live HookRegistry. Production callers ignore it.

    An empty list and ``None`` are treated identically (no
    registration); the REST layer never produces empty lists, but
    a misbehaving snapshot producer could, and zero-rule should
    mean zero-handler in either case.
    """
    if not safety_rules:
        return False

    # Lazy import keeps this module importable when agent_runner is
    # vendored without its SDK deps — the tests exercise this branch
    # with a minimal HookRegistry so the import path has to resolve.
    from agent_runner.safety.hook_handler import SafetyHookHandler
    from agent_runner.safety.registry import build_container_registry

    hook_registry.register(
        SafetyHookHandler(
            rules=safety_rules,
            registry=build_container_registry(),
            tool_ctx=tool_ctx,
        )
    )
    return True


__all__ = ["load_safety_rules_snapshot", "maybe_register_safety_handler"]
