"""Derive the gateway's internal-name DNS exemption from its own resolv.conf.

The authoritative DNS resolver (``dns_resolver.DnsServer``) blocks every
name that is not on ``EGRESS_DNS_ALLOWLIST`` — a tripwire against DNS
exfiltration. But agents must still resolve *internal* names (``nats`` to
reach the bus, ``egress-gateway`` for the proxies) and, on Kubernetes,
arbitrary ``*.cluster.local`` service names. Those are not exfil: the
platform's own resolver is authoritative for them and never forwards them
to a public upstream, so an attacker's nameserver can never receive them.

This is really the KUBERNETES adapter. On Docker, the agent's resolver is
the embedded DNS (127.0.0.11); it answers container names (``nats``,
``egress-gateway``) locally and forwards only external names to the gateway
(its configured upstream), so internal names never reach the gateway
resolver and need no exemption. Kubernetes pods have no per-container
embedded DNS — ``dnsPolicy: None`` pins the agent's resolver straight at
the gateway — so the gateway must itself recreate that "answer internal
names from the platform resolver" behaviour. This module derives it,
runtime-agnostically, from the gateway container's OWN ``/etc/resolv.conf``
rather than branching on ``ROLEMESH_CONTAINER_RUNTIME``:

  Kubernetes  nameserver = kube-dns ClusterIP
              search     = <ns>.svc.cluster.local svc.cluster.local cluster.local
              => internal = any name under the cluster domain. kube-dns is
                 authoritative for cluster.local and never forwards those
                 names upstream, so they cannot leak.

  Docker      nameserver = 127.0.0.11 (embedded DNS)
              search     = (typically an inherited host domain)
              => internal = single-label names only (a defensive fallback;
                 real internal names are already answered by the agent's
                 embedded DNS before reaching the gateway).

Two safety rules keep the exemption from becoming an exfil hole:

  * Only suffixes within ``cluster_domain`` count — an inherited HOST
    search domain (a dev box's ISP domain, a node's corp domain) must NOT
    exempt every external name under it. See ``build_internal_exemption``.
  * Single-label exemption is gated on the Docker embedded-DNS address:
    kube-dns WOULD forward a bare single label to its own upstream (a
    leak), whereas ``127.0.0.11`` cannot route a single label anywhere an
    attacker controls.

Multi-label external names (``secret.attacker.com``) match no internal
suffix on either runtime and stay on the allowlist path.
"""

from __future__ import annotations

from dataclasses import dataclass

from rolemesh.core.logger import get_logger

from .dns_resolver import InternalMatcher, UpstreamResolver

logger = get_logger()

# Docker's embedded DNS, fixed by Docker at this address. Its presence as
# the gateway's resolver is what makes single-label names safe to treat as
# internal (see the module docstring).
DOCKER_EMBEDDED_DNS = "127.0.0.11"


@dataclass(frozen=True)
class ResolvConf:
    """The fields of /etc/resolv.conf this module cares about."""

    nameservers: tuple[str, ...] = ()
    search: tuple[str, ...] = ()


def parse_resolv_conf(text: str) -> ResolvConf:
    """Parse ``nameserver`` and ``search`` / ``domain`` lines.

    Mirrors the libc resolver where it matters: the last ``search`` (or
    ``domain``) line wins, trailing dots on suffixes are stripped, and
    comments (``#`` / ``;``) are ignored. Other directives are dropped.
    """
    nameservers: list[str] = []
    search: tuple[str, ...] = ()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        parts = line.split()
        if parts[0] == "nameserver" and len(parts) >= 2:
            nameservers.append(parts[1])
        elif parts[0] in ("search", "domain") and len(parts) >= 2:
            search = tuple(p.rstrip(".").lower() for p in parts[1:] if p.strip("."))
    return ResolvConf(nameservers=tuple(nameservers), search=search)


def _is_ipv4(addr: str) -> bool:
    """True for a dotted-quad. The upstream forwarder is AF_INET only, so
    IPv6 nameservers (kube-dns dual-stack, link-local) are skipped."""
    octets = addr.split(".")
    if len(octets) != 4:
        return False
    try:
        return all(0 <= int(o) <= 255 for o in octets)
    except ValueError:
        return False


def build_internal_exemption(
    resolv: ResolvConf,
    *,
    cluster_domain: str = "cluster.local",
) -> tuple[InternalMatcher, list[UpstreamResolver]] | None:
    """Build ``(is_internal, internal_upstreams)`` from a parsed resolv.conf.

    Only search suffixes WITHIN *cluster_domain* count as internal. This is
    the load-bearing safety filter: a gateway container often inherits the
    host's search domain (a dev box's ISP domain, a node's corp domain),
    and treating that as internal would exempt every external name under it
    from the allowlist — reopening the exfil channel. The cluster domain is
    the only suffix the platform's own resolver is authoritative for.

    Returns ``None`` when no exemption can be derived — no usable IPv4
    nameserver, or nothing that counts as internal — in which case the
    caller leaves the resolver fail-closed (every name on the allowlist
    path). Never returns an exemption without a resolver to forward to.
    """
    upstreams = [UpstreamResolver(host=ns) for ns in resolv.nameservers if _is_ipv4(ns)]
    if not upstreams:
        return None

    cluster_domain = cluster_domain.rstrip(".").lower()
    suffixes = tuple(s for s in resolv.search if s and (s == cluster_domain or s.endswith("." + cluster_domain)))
    single_label_internal = DOCKER_EMBEDDED_DNS in resolv.nameservers

    if not suffixes and not single_label_internal:
        return None

    def is_internal(qname: str) -> bool:
        name = qname.rstrip(".").lower()
        if not name:
            return False
        if "." not in name:
            # Bare single label: only internal where the platform resolver
            # answers such names locally without an upstream hop (Docker).
            return single_label_internal
        return any(name == suf or name.endswith("." + suf) for suf in suffixes)

    return is_internal, upstreams


__all__ = [
    "DOCKER_EMBEDDED_DNS",
    "ResolvConf",
    "build_internal_exemption",
    "parse_resolv_conf",
]
