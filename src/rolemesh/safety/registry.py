"""In-memory registry of SafetyCheck instances.

Both orchestrator and container hold a CheckRegistry. The orchestrator
registry is populated once at startup with every known check (V1: just
``pii.regex``); the container registry is populated with cheap checks
only (V2 will add RemoteCheck proxies for slow checks).

The registry itself carries no config — it maps ``check_id`` →
``SafetyCheck`` instance. Rule configs live in the DB and are passed to
``check.check()`` at run-time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import UnknownCheckError

if TYPE_CHECKING:
    from .types import SafetyCheck


class CheckRegistry:
    """check_id → SafetyCheck lookup.

    Duplicate registration for the same id is a programming error and
    raises. The registry is typically built once at process start and
    treated as immutable thereafter.
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


def build_default_registry() -> CheckRegistry:
    """Return the V1 default registry.

    Lives in this module so both orchestrator and container can call it
    and get an identical set of cheap-class checks. V2 extends this with
    a separate ``build_orchestrator_registry`` that additionally
    registers slow checks (Presidio, LLM Guard, etc).
    """
    from .checks.pii_regex import PIIRegexCheck

    reg = CheckRegistry()
    reg.register(PIIRegexCheck())
    return reg


__all__ = ["CheckRegistry", "build_default_registry"]
