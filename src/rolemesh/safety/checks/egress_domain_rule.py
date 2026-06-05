"""Domain allowlist check for the EGRESS_REQUEST stage (EC-3).

This check is the EC stage's answer to ``pii.regex`` — a cheap,
pure-Python detector with a small, closed config schema. Each rule row
carries a list of ``domain_patterns`` and an optional ``ports`` list;
the check reports whether the request host/port matches ANY pattern,
and the gateway's aggregator decides allow vs block based on whether
any rule matched.

One rule carries many patterns deliberately. A typical operator
allowlist is 5-10 hosts (Stripe + Amazon Ads + Slack; a handful of
SaaS); modelling that as one rule — rather than N near-identical
rows — keeps the Safety rules page legible, makes the audit story
"patterns: [...] → [...]" a single record, and matches the shape of
``domain_allowlist`` (``allowed_hosts: list[str]``). ``ports`` is
shared across every pattern in the rule; patterns that need different
port scoping belong in separate rules.

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
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..types import Finding, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..types import Action, ActionModel, CostClass, SafetyContext


class EgressDomainCode(StrEnum):
    """Stable Finding.code values emitted by the check."""

    DOMAIN_ALLOWED = "EGRESS.DOMAIN_ALLOWED"
    DOMAIN_DENIED = "EGRESS.DOMAIN_DENIED"


class EgressDomainRuleConfig(BaseModel):
    """REST-time validated config shape.

    ``extra='forbid'`` so an admin typo like ``{"domains": ["x"]}`` (plural
    + wrong key) fails loud at rule create time rather than accepted-and-
    silently-ignored at run time. This is the property that makes the
    closed schema worth keeping closed — there is intentionally no
    back-compat ``domain_pattern`` (singular) alias, because accepting
    two legal shapes is exactly the silent-typo hazard ``extra='forbid'``
    exists to prevent.

    ``domain_patterns`` is required and non-empty: an empty list overlaps
    semantically with "delete the rule", so it is rejected rather than
    silently meaning "match nothing". The ``max_length=100`` cap keeps a
    single rule from ballooning past audit-readability — typical
    allowlists are 5-10, extreme ones well under 50. Each element is
    bounded to the DNS name length limit (253); a longer "pattern" is
    either a bug or an attempt to sneak one through.
    """

    model_config = ConfigDict(extra="forbid")

    domain_patterns: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=253)]],
        Field(min_length=1, max_length=100),
    ]
    ports: list[int] | None = None

    @field_validator("domain_patterns")
    @classmethod
    def _strip_patterns(cls, patterns: list[str]) -> list[str]:
        # Reject whitespace-only entries so they can't sneak through as a
        # silent "match nothing" slot. Element length bounds above run
        # first; this normalisation only trims and re-checks emptiness.
        clean = [p.strip() for p in patterns]
        if any(not p for p in clean):
            raise ValueError("domain_patterns entries must be non-empty")
        return clean


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

    # Action matrix (descriptive — see SafetyCheck Protocol). Aggregated
    # model: this check VOTES, it does not decide. ``check()`` returns
    # "allow" on a domain match; the gateway aggregator produces the
    # effective block when no rule allowed the request. natural_actions
    # is therefore the check's own return ("allow"), NOT the aggregated
    # outcome — the UI surfaces the effective semantics via action_model.
    #                   natural   supported
    # EGRESS_REQUEST    allow     block, allow
    # (no warn: nobody reads appended_context at the gateway; no
    #  require_approval: the gateway has no agent/human-in-the-loop UX;
    #  no redact: a TCP/DNS attempt has no rewritable payload)
    action_model: ActionModel = "aggregated"
    natural_actions: Mapping[Stage, Action] = {
        Stage.EGRESS_REQUEST: "allow",
    }
    supported_actions: Mapping[Stage, frozenset[Action]] = {
        Stage.EGRESS_REQUEST: frozenset({"block", "allow"}),
    }
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
        if cfg.ports is not None and port not in cfg.ports:
            return Verdict(action="allow")
        matched = next(
            (p for p in cfg.domain_patterns if _matches(host, p)), None
        )
        if matched is None:
            return Verdict(action="allow")
        return Verdict(
            action="allow",
            findings=[
                Finding(
                    code=EgressDomainCode.DOMAIN_ALLOWED.value,
                    severity="info",
                    message=f"matched {matched}",
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
        if not host:
            return False, []
        if cfg.ports is not None and port not in cfg.ports:
            return False, []
        matched = next(
            (p for p in cfg.domain_patterns if _matches(host, p)), None
        )
        if matched is None:
            return False, []
        return True, [
            {
                "code": EgressDomainCode.DOMAIN_ALLOWED.value,
                "severity": "info",
                "message": f"matched {matched}",
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
