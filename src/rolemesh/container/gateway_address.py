"""Runtime-resolved egress gateway address (docs/21 §4.2).

Agents get the gateway pinned as their DNS resolver (``resolv.conf``
accepts only IPs), so every spawn needs the gateway's address. Where it
comes from depends on the deployment mode:

* **docker** — always the static ``EGRESS_GATEWAY_DNS_IP`` config value
  (the fixed bridge address); this module is a pass-through.
* **k8s, static ClusterIP** (``egressGateway.clusterIP`` set — the
  recommended production mode): same pass-through; verify_infrastructure
  strictly asserts the Service matches the configured value.
* **k8s, dynamic ClusterIP** (``egressGateway.clusterIP`` empty — e.g.
  many ephemeral instances on one cluster, where per-instance static
  allocation is impractical): the chart renders
  ``EGRESS_GATEWAY_DNS_IP=""`` and the K8s runtime DISCOVERS the
  Service's allocated ClusterIP at startup, seeding the override here;
  it re-reads before every spawn and updates on drift (Service
  recreation) with a loud error.

This module exists because ``EGRESS_GATEWAY_DNS_IP`` is imported by
value at module load across the codebase — a runtime-discovered address
needs one explicit, mutable home instead of a rebindable global. The
override is process-local state owned by the K8s runtime; nothing else
may set it.
"""

from __future__ import annotations

from rolemesh.core.config import EGRESS_GATEWAY_DNS_IP

_override: str | None = None


def get_gateway_dns_ip() -> str:
    """The address agents should use for the gateway right now.

    The discovered override wins when set; otherwise the static config
    value (which is authoritative in docker mode and k8s static mode).
    """
    return _override if _override is not None else EGRESS_GATEWAY_DNS_IP


def set_gateway_dns_ip(ip: str) -> None:
    """Record the runtime-discovered gateway address (K8s dynamic mode).

    Called by the K8s runtime only: once at verify time, and again from
    the spawn path whenever a pre-spawn re-read observes a changed
    ClusterIP.
    """
    global _override
    _override = ip


def reset_gateway_dns_ip() -> None:
    """Drop the override (tests; process restart does this naturally)."""
    global _override
    _override = None
