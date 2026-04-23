"""Egress gateway container entry point.

``python -m rolemesh.egress.gateway`` is the Docker ``ENTRYPOINT`` for
``rolemesh-egress-gateway``.

Port plan:
    3001  reverse proxy (credential injection for LLM + MCP)
    3128  forward proxy (HTTP CONNECT)
    53    authoritative DNS resolver (gated on CAP_NET_BIND_SERVICE)

EC-1 bound only 3001. EC-2 wires all three plus the NATS-backed
identity + policy plumbing that feeds the Safety pipeline.

Bind addresses default to ``0.0.0.0`` because the container is
dual-homed on two bridges (internal agent-net + external egress-net)
and we want agents on the first bridge to reach each listener without
caring which interface they're on. The actual isolation is enforced by
the Internal=true flag on agent-net — see docs/egress/deployment.md.

Startup is fail-closed: any subsystem that cannot reach NATS or load
its initial state raises out of ``main``, which exits with a non-zero
code and triggers the container's ``restart: unless-stopped`` policy.
Agents sitting on the internal bridge will simply have no gateway
during the restart loop — intentional, because running without a
populated policy cache would mean defaulting every request to allow
or every request to block, both of which are worse than no gateway
(agents time out quickly and the operator sees the paging signal).
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import AsyncExitStack

from rolemesh.core.config import (
    CREDENTIAL_PROXY_PORT,
    EGRESS_GATEWAY_DNS_PORT,
    EGRESS_GATEWAY_FORWARD_PORT,
    NATS_URL,
)
from rolemesh.core.logger import get_logger

from .dns_resolver import DnsServer, UpstreamResolver
from .forward_proxy import ForwardProxy
from .identity import IdentityResolver, subscribe_lifecycle
from .policy_cache import (
    PolicyCache,
    fetch_snapshot_via_nats,
    subscribe_rule_changes,
)
from .reverse_proxy import start_credential_proxy
from .safety_call import AuditPublisher, EgressSafetyCaller

logger = get_logger()


# Gateway-side env knobs. All three are optional so a default build
# runs end-to-end without extra config; override in production.
#
# EGRESS_UPSTREAM_DNS: comma-separated list of DNS resolvers the
#   authoritative resolver recurses to on allow. Defaults match the
#   public internet resolvers that respond fastest under typical
#   workloads.
# EGRESS_SNAPSHOT_TIMEOUT: how long the gateway waits for the initial
#   rule snapshot before giving up and failing start.
_UPSTREAM_DNS_DEFAULT = "8.8.8.8,1.1.1.1"
_SNAPSHOT_TIMEOUT_S = 5.0


def _parse_upstream_dns(raw: str) -> list[UpstreamResolver]:
    """Split "8.8.8.8,1.1.1.1:5353" into UpstreamResolver records.

    Accepts optional ``:port`` on each entry; defaults to 53 so a plain
    list like ``"8.8.8.8,1.1.1.1"`` continues to work.
    """
    out: list[UpstreamResolver] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            host, _, port_str = token.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                logger.warning("dns upstream: bad port — skipping", entry=token)
                continue
            out.append(UpstreamResolver(host=host, port=port))
        else:
            out.append(UpstreamResolver(host=token))
    return out


async def main() -> None:
    upstream_dns = _parse_upstream_dns(
        os.environ.get("EGRESS_UPSTREAM_DNS", _UPSTREAM_DNS_DEFAULT)
    )

    # Import nats-py lazily so unit tests importing this module don't
    # require the dependency. The gateway Dockerfile pins it, so
    # production always finds it.
    import nats  # type: ignore[import-not-found]

    nats_client = await nats.connect(NATS_URL)
    logger.info("gateway connected to NATS", url=NATS_URL)

    stop = asyncio.Event()

    async with AsyncExitStack() as stack:
        stack.push_async_callback(nats_client.close)

        # --- Policy cache: snapshot + hot reload ---------------------
        cache = PolicyCache()
        try:
            snapshot = await fetch_snapshot_via_nats(
                nats_client, timeout_s=_SNAPSHOT_TIMEOUT_S
            )
        except Exception as exc:
            logger.error(
                "gateway: could not load rule snapshot — refusing to start",
                error=str(exc),
            )
            raise
        await cache.seed(snapshot)
        rule_sub = await subscribe_rule_changes(nats_client, cache)
        stack.push_async_callback(rule_sub.unsubscribe)  # type: ignore[attr-defined]

        # --- Identity resolver: snapshot + lifecycle subscribe -------
        identity = IdentityResolver()
        # Identity snapshot goes through the same pattern but using an
        # /api/internal/egress/identity-snapshot REST endpoint that
        # belongs to EC-2.8. For now, seed empty — the subscription
        # below picks up state as agents start.
        # TODO(EC-2.8): fetch via orchestrator REST before subscribing.
        lifecycle_sub = await subscribe_lifecycle(nats_client, identity)
        stack.push_async_callback(lifecycle_sub.unsubscribe)  # type: ignore[attr-defined]

        # --- Safety caller (audit publish via NATS) ------------------
        audit = AuditPublisher(nats_client=nats_client)
        # Checks are registered by check_id. EC-3 adds the
        # ``egress.domain_rule`` check; until then the dict is empty
        # and every request blocks (default-deny aggregation).
        check_map: dict[str, object] = {}
        try:
            # Lazy import: the egress check lives in the main
            # rolemesh.safety.checks tree, added by EC-3. When EC-3
            # ships, this block resolves and checks start evaluating.
            from rolemesh.safety.checks.egress_domain_rule import (  # type: ignore[import-not-found]
                make_egress_domain_check,
            )

            check_map["egress.domain_rule"] = make_egress_domain_check()
        except ImportError:
            logger.warning(
                "gateway: egress.domain_rule check not available yet "
                "(expected until EC-3 lands) — all requests will be blocked"
            )

        safety = EgressSafetyCaller(
            cache=cache,
            checks=check_map,
            audit_publisher=audit,
        )

        # --- Reverse proxy (port 3001) -------------------------------
        reverse_runner = await start_credential_proxy(
            port=CREDENTIAL_PROXY_PORT,
            host="0.0.0.0",
            identity_resolver=identity,
            safety_caller=safety,
        )
        stack.push_async_callback(reverse_runner.cleanup)

        # --- Forward proxy (port 3128) -------------------------------
        forward = ForwardProxy(identity_resolver=identity, safety_caller=safety)
        forward_server = await forward.serve("0.0.0.0", EGRESS_GATEWAY_FORWARD_PORT)

        async def _close_forward() -> None:
            forward_server.close()
            await forward_server.wait_closed()

        stack.push_async_callback(_close_forward)

        # --- DNS resolver (port 53) ----------------------------------
        dns = DnsServer(
            identity_resolver=identity,
            safety_caller=safety,
            upstreams=upstream_dns,
        )
        await dns.serve("0.0.0.0", EGRESS_GATEWAY_DNS_PORT)

        def _close_dns() -> None:
            dns.close()

        stack.callback(_close_dns)

        logger.info(
            "egress gateway ready",
            reverse_port=CREDENTIAL_PROXY_PORT,
            forward_port=EGRESS_GATEWAY_FORWARD_PORT,
            dns_port=EGRESS_GATEWAY_DNS_PORT,
            upstreams=[f"{u.host}:{u.port}" for u in upstream_dns],
            stage="EC-2",
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("egress gateway stopping")


if __name__ == "__main__":
    asyncio.run(main())
