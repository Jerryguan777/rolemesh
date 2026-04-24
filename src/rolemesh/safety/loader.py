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

# ``rolemesh.db.pg`` and ``asyncpg`` are orchestrator-only dependencies
# (not shipped in the agent container image). We still expose
# ``list_safety_rules_for_coworker`` as a module attribute so that
# orchestrator-side tests can ``monkeypatch.setattr(loader,
# "list_safety_rules_for_coworker", ...)`` — the agent container path
# never calls it.
try:
    from rolemesh.db.pg import list_safety_rules_for_coworker  # noqa: F401
except ModuleNotFoundError:
    # Agent container: rolemesh.db is not packaged. Leave the symbol
    # undefined at module scope; ``fetch_safety_rule_snapshots`` below
    # re-imports inside the function body so the orchestrator-only
    # code path still works when asyncpg + pg ARE present.
    pass

if TYPE_CHECKING:
    from agent_runner.hooks.registry import HookRegistry
    from agent_runner.tools.context import ToolContext


logger = get_logger()


# Exceptions the loader considers "DB unreachable at job start" — i.e.
# the fail-mode dispatch actually applies. Anything else (ImportError,
# AssertionError from _get_pool before init_database, programmer
# errors in to_snapshot_dict) propagates normally so it surfaces as a
# real bug, not as a misleading "SAFETY_FAIL_MODE=closed" log line.
#
# Python's stdlib makes TimeoutError a subclass of OSError since 3.11,
# but we list it explicitly for readability — reviewers should be able
# to see the intent without consulting the hierarchy.
#
# ``asyncpg.PostgresError`` is resolved lazily in ``_get_db_exceptions``
# so this module can import without asyncpg present (agent container
# path).


def _get_db_exceptions() -> tuple[type[BaseException], ...]:
    """Return the exception tuple that signals "DB unreachable".

    Lazy import of asyncpg so the agent container — which has asyncpg
    via pip but also has this module on its path — doesn't hit a
    ``ModuleNotFoundError`` on the orchestrator-only ``rolemesh.db``
    package. The orchestrator is the only caller of the fail-mode
    path, and it always has asyncpg installed.
    """
    import asyncpg

    return (asyncpg.PostgresError, ConnectionError, OSError, TimeoutError)


async def fetch_safety_rule_snapshots(
    tenant_id: str, coworker_id: str
) -> list[dict[str, Any]]:
    """Query + serialize. Raises on any DB failure; caller decides.

    Shared by ``load_safety_rules_snapshot`` (wraps with fail-mode
    for container startup) and ``SafetyEngine.load_rules_for_coworker``
    (admin-side path that wants the DB error to surface). Putting the
    single query + snapshot_dict conversion in one place prevents the
    two paths from drifting on things like the duplicate-enabled
    filter that the original engine path carried.

    ``list_safety_rules_for_coworker`` already filters ``enabled=TRUE``
    in SQL, so no application-side filter is needed here.
    """
    # Resolve via ``globals()`` so orchestrator tests that
    # ``monkeypatch.setattr(loader, "list_safety_rules_for_coworker",
    # ...)`` actually substitute the function at call time. A
    # ``from rolemesh.db.pg import ...`` at call site would bypass
    # the patched binding; the try/except at module top imports it
    # into this namespace on the orchestrator path, and agent
    # containers hit the ModuleNotFoundError fallback and raise a
    # clear error if this function is ever called there.
    fn = globals().get("list_safety_rules_for_coworker")
    if fn is None:
        raise RuntimeError(
            "fetch_safety_rule_snapshots called outside the orchestrator: "
            "rolemesh.db.pg is not packaged in this environment"
        )
    rules = await fn(tenant_id, coworker_id)
    return [r.to_snapshot_dict() for r in rules]


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
        rules but a loud WARNING log surfaces to operators. Acceptable
        for self-hosted deployments that prefer agent availability
        over safety coverage during an incident.

    Returns None in two cases that look identical to the caller:
    genuinely no rules configured, OR fail-open fallback after a DB
    error. Distinguishing those in callers is never useful — both
    mean "run the agent without the safety hook".

    Only catches exceptions consistent with "DB unreachable" (see
    ``_DB_UNREACHABLE_EXCEPTIONS``); programmer errors propagate so
    they surface as real bugs rather than being masked as outages.
    """
    db_exceptions = _get_db_exceptions()
    try:
        snapshots = await fetch_safety_rule_snapshots(tenant_id, coworker_id)
    except db_exceptions as exc:
        # Import SAFETY_FAIL_MODE inside the except so test monkeypatch
        # on the module attribute lands on the same symbol the code
        # reads. The hoisted top-level import of pg / asyncpg does not
        # have this concern — they are not test surfaces.
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

    if not snapshots:
        return None
    return snapshots


def maybe_register_safety_handler(
    hook_registry: HookRegistry,
    safety_rules: list[dict[str, Any]] | None,
    tool_ctx: ToolContext,
    *,
    slow_check_specs: list[dict[str, Any]] | None = None,
    nats_client: Any = None,
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

    When ``slow_check_specs`` is non-empty AND ``nats_client`` is
    provided, the container registry is extended with one
    ``RemoteCheck`` per spec. Both arguments are optional because
    deployments without any slow check (the common pre-P1.x case)
    still use this function to wire the cheap-only registry — adding
    two required arguments would force callers to pass ``None`` / a
    live NATS client just to say "no slow checks please".
    """
    if not safety_rules:
        return False

    # Lazy import preserves the zero-overhead contract: when no rules
    # apply, we never pay the agent_runner.safety.* import cost at
    # agent startup. That's the entire value of the early-return
    # above; keeping these imports inside the function body makes the
    # contract physically true rather than just aspirational.
    from agent_runner.safety.hook_handler import SafetyHookHandler
    from agent_runner.safety.registry import build_container_registry

    registry = build_container_registry()
    if slow_check_specs and nats_client is not None:
        # Import RemoteCheck only on the slow-check path. A deployment
        # with cheap checks only pays zero cost for the transport
        # proxy module — same early-return discipline as the outer
        # zero-rule guard above.
        from agent_runner.safety.remote import RemoteCheck

        for spec in slow_check_specs:
            try:
                registry.register(RemoteCheck.from_spec(spec, nats_client))
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "safety: malformed slow_check_spec — skipping",
                    component="safety",
                    check_id=spec.get("check_id", "?"),
                    error=str(exc),
                )

    hook_registry.register(
        SafetyHookHandler(
            rules=safety_rules,
            registry=registry,
            tool_ctx=tool_ctx,
        )
    )
    return True


__all__ = [
    "fetch_safety_rule_snapshots",
    "load_safety_rules_snapshot",
    "maybe_register_safety_handler",
]
