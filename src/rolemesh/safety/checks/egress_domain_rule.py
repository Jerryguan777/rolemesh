"""Domain allowlist check for the EGRESS_REQUEST stage (EC-3).

This check is the EC stage's answer to ``pii.regex`` — a cheap,
pure-Python detector with a small, closed config schema. Each rule row
carries one ``domain_pattern`` and an optional ``ports`` list; the
check reports whether the request host/port matches, and the gateway's
aggregator decides allow vs block based on whether any rule matched.

Matching semantics:

    pattern="api.anthropic.com"     → exact (case-insensitive)
    pattern="*.github.com"          → suffix; matches api.github.com
                                      and raw.github.com but NOT
                                      github.com.evil.com

``ports=None`` means "any port". This matters because a legitimate
``*.github.com`` allowlist almost always wants to be scoped to 443
— leaving the port open is how you accidentally whitelist the SSH
service for an attacker with a matching SNI.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ..types import Finding, Stage, Verdict

if TYPE_CHECKING:
    from ..types import CostClass, SafetyContext


class EgressDomainCode(StrEnum):
    """Stable Finding.code values emitted by the check."""

    DOMAIN_ALLOWED = "EGRESS.DOMAIN_ALLOWED"
    DOMAIN_DENIED = "EGRESS.DOMAIN_DENIED"


class EgressDomainRuleConfig(BaseModel):
    """REST-time validated config shape.

    ``extra='forbid'`` so an admin typo like ``{"domains": ["x"]}`` (plural
    + wrong key) fails loud at rule create time rather than accepted-and-
    silently-ignored at run time.

    ``domain_pattern``'s upper bound of 253 matches the DNS name length
    limit. A longer "pattern" is either a bug or an attempt to sneak
    one through.
    """

    model_config = ConfigDict(extra="forbid")

    domain_pattern: str = Field(..., min_length=1, max_length=253)
    ports: list[int] | None = None


def _matches(host: str, pattern: str) -> bool:
    """Domain-aware match used by the gateway and the admin-side check.

    Both sides strip trailing dots + lowercase; keeps
    "Api.Anthropic.com." identical to "api.anthropic.com" regardless of
    which side of the wire the label came from.
    """
    pattern = pattern.lower().rstrip(".")
    host = host.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".github.com"
        return host.endswith(suffix)
    return host == pattern


class EgressDomainRuleCheck:
    """SafetyCheck Protocol implementation for admin-side validation.

    The gateway does not invoke this class directly — see
    ``make_egress_domain_check`` below for the gateway adapter. This
    class exists so ``build_orchestrator_registry`` can validate
    rule configs at REST create/update time via ``config_model``.
    """

    id: str = "egress.domain_rule"
    version: str = "1"
    stages: frozenset[Stage] = frozenset({Stage.EGRESS_REQUEST})
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset(c.value for c in EgressDomainCode)
    config_model: type[BaseModel] = EgressDomainRuleConfig

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        """Evaluate the check given a SafetyContext payload.

        The orchestrator-side pipeline is the only caller of this path;
        the gateway uses the adapter callable returned by
        ``make_egress_domain_check`` instead because it operates on a
        pared-down EgressRequest rather than a full SafetyContext.

        Allow on match so the orchestrator pipeline can aggregate the
        same way the gateway does (any rule allow → overall allow).
        """
        cfg = EgressDomainRuleConfig.model_validate(config)
        host = str(ctx.payload.get("host", ""))
        port = int(ctx.payload.get("port", 0))
        if not host:
            return Verdict(action="allow")
        if not _matches(host, cfg.domain_pattern):
            return Verdict(action="allow")
        if cfg.ports is not None and port not in cfg.ports:
            return Verdict(action="allow")
        return Verdict(
            action="allow",
            findings=[
                Finding(
                    code=EgressDomainCode.DOMAIN_ALLOWED.value,
                    severity="info",
                    message=f"matched {cfg.domain_pattern}",
                )
            ],
        )


def make_egress_domain_check() -> Any:
    """Return the gateway-side callable for ``egress.domain_rule``.

    The gateway's safety_call.py expects
        async def check(req: EgressRequest, config: dict)
            -> tuple[bool, list[dict]]
    — a flat shape that doesn't require importing the full
    ``SafetyContext`` type into the gateway image. This factory
    returns exactly that callable, sharing the ``_matches`` helper
    with the Protocol-compliant class above.
    """
    async def _check(
        request: Any, config: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        try:
            cfg = EgressDomainRuleConfig.model_validate(config)
        except Exception:  # noqa: BLE001 — bad config = no match; logged by caller
            return False, []
        host = getattr(request, "host", "")
        port = int(getattr(request, "port", 0))
        if not host or not _matches(host, cfg.domain_pattern):
            return False, []
        if cfg.ports is not None and port not in cfg.ports:
            return False, []
        return True, [
            {
                "code": EgressDomainCode.DOMAIN_ALLOWED.value,
                "severity": "info",
                "message": f"matched {cfg.domain_pattern}",
                "metadata": {"host": host, "port": port},
            }
        ]

    return _check


__all__ = [
    "EgressDomainCode",
    "EgressDomainRuleCheck",
    "EgressDomainRuleConfig",
    "make_egress_domain_check",
]
