"""In-memory registry of SafetyCheck instances.

Orchestrator and container hold distinct registries and use distinct
builders, so a slow check (Presidio, LLM Guard, etc.) the orchestrator
registers at V2 cannot accidentally leak into the container image as
an unresolvable import — the container's builder never references
slow-check modules.

The registry itself carries no config — it maps ``check_id`` →
``SafetyCheck`` instance. Rule configs live in the DB and are passed
to ``check.check()`` at run-time.

The orchestrator registry is a process-wide singleton (see
``get_orchestrator_registry``). V2 slow checks may initialize heavy
resources (spaCy models, HuggingFace downloads) in their ``__init__``,
so rebuilding on every REST call would block request handlers for
seconds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import UnknownCheckError

if TYPE_CHECKING:
    from .types import SafetyCheck


class CheckRegistry:
    """check_id → SafetyCheck lookup.

    Duplicate registration for the same id is a programming error and
    raises. Treated as immutable after ``build_*_registry`` returns —
    there is no runtime hot-plug path.
    """

    def __init__(self) -> None:
        self._checks: dict[str, SafetyCheck] = {}

    def register(self, check: SafetyCheck) -> None:
        if check.id in self._checks:
            raise ValueError(
                f"Safety check already registered: {check.id}"
            )
        self._checks[check.id] = check

    def get(self, check_id: str) -> SafetyCheck:
        try:
            return self._checks[check_id]
        except KeyError as exc:
            raise UnknownCheckError(check_id) from exc

    def has(self, check_id: str) -> bool:
        return check_id in self._checks

    def ids(self) -> list[str]:
        return list(self._checks.keys())

    def all(self) -> list[SafetyCheck]:
        return list(self._checks.values())

    def __len__(self) -> int:
        return len(self._checks)


def build_container_registry() -> CheckRegistry:
    """Cheap-class checks available inside the agent container.

    V1 = {pii.regex}. V2 extends with RemoteCheck proxies (one per
    slow check) that translate ``check()`` calls to NATS RPC requests
    to the orchestrator. The container MUST NOT import slow-check
    implementations — they may pull spaCy, llm-guard, transformers
    etc. that are not installed in the agent image.
    """
    from .checks.pii_regex import PIIRegexCheck

    reg = CheckRegistry()
    reg.register(PIIRegexCheck())
    return reg


def build_orchestrator_registry() -> CheckRegistry:
    """All checks available to the orchestrator.

    V1 only has cheap checks, so orchestrator and container registries
    contain the same entries. V2 diverges: orchestrator additionally
    registers presidio.pii, llm_guard.prompt_injection, rate_limit,
    and other slow-class checks.
    """
    from .checks.pii_regex import PIIRegexCheck

    reg = CheckRegistry()
    reg.register(PIIRegexCheck())
    return reg


_ORCHESTRATOR_REGISTRY: CheckRegistry | None = None


def get_orchestrator_registry() -> CheckRegistry:
    """Process-wide singleton; initialized on first call.

    REST handlers call this for rule validation. Lazy-init rather than
    eager at import so tests can reset between cases via
    ``reset_orchestrator_registry``.
    """
    global _ORCHESTRATOR_REGISTRY
    if _ORCHESTRATOR_REGISTRY is None:
        _ORCHESTRATOR_REGISTRY = build_orchestrator_registry()
    return _ORCHESTRATOR_REGISTRY


def reset_orchestrator_registry() -> None:
    """Drop the singleton so the next ``get_orchestrator_registry``
    call rebuilds. Test-only; production code should never call this.
    """
    global _ORCHESTRATOR_REGISTRY
    _ORCHESTRATOR_REGISTRY = None


__all__ = [
    "CheckRegistry",
    "build_container_registry",
    "build_orchestrator_registry",
    "get_orchestrator_registry",
    "reset_orchestrator_registry",
]
