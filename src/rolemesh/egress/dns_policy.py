"""Platform-wide DNS allowlist for the gateway's authoritative resolver.

Replaces the per-tenant rule lookup the DNS plane used until this
module: tenant ``egress.domain_rule`` rows keep governing the HTTP
planes (forward CONNECT + reverse proxy), but DNS decisions are now a
single platform-level policy, identical for every agent.

Why platform-level is enough here
---------------------------------

Agents reach the outside world exclusively through the proxies, and a
proxied request never resolves its target inside the agent container —
the hostname travels to the gateway as a string and the gateway
resolves it on its own egress-side resolver path, which this policy
does not touch. The only queries that arrive at the gateway's
``DnsServer`` therefore come from code that bypasses the proxy
convention (raw ``getaddrinfo`` calls): either a tool missing proxy
configuration, or a DNS-exfiltration attempt. Neither deserves a
per-tenant answer, and serving one required the source-IP → identity
machinery this refactor is retiring (see docs/16, "DNS plane").

The expected steady-state allowlist is EMPTY. Resolution through this
server is a tripwire, not a service: a legitimate flow that fails here
should be fixed by making the tool honor ``HTTP_PROXY``, not by
widening this list — even a successful resolution buys the caller
nothing, because the agent bridge has no route to the resolved address.

Modes
-----

``enforce`` (default)
    Non-matching names get NXDOMAIN and the query never reaches the
    upstream resolver. Fail-closed, matching the rest of the EC stack.

``observe``
    Every name resolves, but would-be blocks are logged at WARNING.
    Migration aid: run a real workload against ``observe`` for a few
    days and every log line is a tool that needs proxy configuration
    (or an incident). Not a steady-state setting.

Configuration is environment-only by design — the list is expected to
stay empty and essentially never change, so a DB row plus gateway-side
snapshot/invalidation plumbing would be cost without benefit. If a
runtime-editable list is ever genuinely needed, the upgrade path is the
``platform_safety_rules`` catalog; this module's ``decide()`` is the
single seam where that lookup would slot in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from rolemesh.core.logger import get_logger
from rolemesh.safety.checks.egress_domain_rule import matches_domain

logger = get_logger()


# Env knobs, read by ``from_env`` at gateway boot (the gateway container
# bind-mounts .env, so these are operator-settable without a rebuild).
ALLOWLIST_ENV = "EGRESS_DNS_ALLOWLIST"
MODE_ENV = "EGRESS_DNS_MODE"

DnsMode = Literal["enforce", "observe"]

_VALID_MODES: frozenset[str] = frozenset({"enforce", "observe"})


@dataclass(frozen=True)
class GlobalDnsPolicy:
    """Immutable platform DNS policy: pattern list + enforcement mode.

    Frozen for the same reason ``Identity`` was: the hot path shares one
    instance across every in-flight query and must not be able to
    mutate it. Pattern semantics are exactly ``egress.domain_rule``'s
    (exact match or ``*.suffix``), via the shared ``matches_domain``.
    """

    patterns: tuple[str, ...] = ()
    mode: DnsMode = "enforce"

    def is_allowed(self, qname: str) -> bool:
        """True when *qname* matches any allowlist pattern."""
        return any(matches_domain(qname, p) for p in self.patterns)

    @classmethod
    def from_env(cls) -> GlobalDnsPolicy:
        """Build the policy from EGRESS_DNS_ALLOWLIST / EGRESS_DNS_MODE.

        Raises ``ValueError`` on an unknown mode so a typo like
        ``EGRESS_DNS_MODE=observ`` kills the gateway at boot instead of
        silently running in whichever mode the typo happens not to be.
        Empty / whitespace allowlist entries are dropped rather than
        rejected — trailing commas are a fact of .env editing.
        """
        raw_list = os.environ.get(ALLOWLIST_ENV, "")
        patterns = tuple(p.strip() for p in raw_list.split(",") if p.strip())

        raw_mode = os.environ.get(MODE_ENV, "enforce").strip().lower()
        if raw_mode not in _VALID_MODES:
            raise ValueError(
                f"{MODE_ENV} must be one of {sorted(_VALID_MODES)}, "
                f"got {raw_mode!r}"
            )

        policy = cls(patterns=patterns, mode=raw_mode)  # type: ignore[arg-type]
        logger.info(
            "dns policy loaded",
            mode=policy.mode,
            pattern_count=len(policy.patterns),
            patterns=list(policy.patterns),
        )
        return policy


__all__ = [
    "ALLOWLIST_ENV",
    "MODE_ENV",
    "DnsMode",
    "GlobalDnsPolicy",
]
