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
# ruff: noqa: I001
# Intentional import order: rolemesh.bootstrap MUST run before
# rolemesh.core.config to get .env values into os.environ.

from __future__ import annotations

# Side-effect import: loads /app/.env (bind-mounted by the launcher)
# into os.environ before rolemesh.core.config captures module-level
# values. Without this the gateway's NATS_URL / CREDENTIAL_PROXY_PORT
# etc. come through as defaults even when the operator set them in
# .env. Must stay at the very top of rolemesh imports.
import rolemesh.bootstrap  # noqa: F401

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
from .identity import (
    IdentityResolver,
    fetch_identity_snapshot_via_nats,
    subscribe_lifecycle,
)
from .mcp_cache import (
    apply_snapshot_to_registry,
    fetch_mcp_snapshot_via_nats,
    subscribe_mcp_changes,
)
from .policy_cache import (
    PolicyCache,
    fetch_snapshot_via_nats,
    subscribe_rule_changes,
)
from .remote_token_vault import RemoteTokenVault
from .reverse_proxy import set_token_vault, start_credential_proxy
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
        # Seed from orchestrator's identity registry BEFORE subscribing
        # to live events. Without this, a gateway restart strands every
        # already-running agent at "Unknown source identity" until its
        # next spawn — lifecycle events are NEW-only on the gateway's
        # consumer and have no replay. A snapshot failure here is
        # non-fatal: seed empty, keep going, and rely on live events to
        # refill as new agents spawn. That's the same behavior we had
        # before this snapshot was implemented, so a degraded snapshot
        # RPC doesn't make things worse.
        try:
            identity_entries = await fetch_identity_snapshot_via_nats(
                nats_client, timeout_s=_SNAPSHOT_TIMEOUT_S
            )
            await identity.seed(identity_entries)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gateway: identity snapshot fetch failed — continuing with empty "
                "cache; live lifecycle events will refill",
                error=str(exc),
            )
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

        # --- MCP server registry: snapshot + hot reload --------------
        # The MCP registry has to be seeded BEFORE start_credential_proxy
        # binds — otherwise a client request that lands during the boot
        # window between bind and snapshot-arrival sees the registry as
        # empty and gets a 404 it shouldn't have. Snapshot failure is
        # fail-soft (log + continue with empty registry); subsequent
        # ``egress.mcp.changed`` events still fill it in as the
        # orchestrator publishes them, and the operator sees the warning
        # instead of crash-looping the gateway over a transient NATS
        # blip on the orchestrator side.
        try:
            mcp_entries = await fetch_mcp_snapshot_via_nats(
                nats_client, timeout_s=_SNAPSHOT_TIMEOUT_S
            )
            apply_snapshot_to_registry(mcp_entries)
            logger.info("gateway: MCP registry seeded", count=len(mcp_entries))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gateway: MCP snapshot fetch failed — continuing with empty "
                "registry; live change events will refill",
                error=str(exc),
            )
        mcp_sub = await subscribe_mcp_changes(nats_client)
        stack.push_async_callback(mcp_sub.unsubscribe)  # type: ignore[attr-defined]

        # --- Token vault: forward per-user MCP token requests --------
        # The orchestrator owns the DB-backed TokenVault (refresh
        # tokens live encrypted in oidc_user_tokens, IdP credentials
        # live in env on the orchestrator's filesystem). The gateway
        # carries a ``RemoteTokenVault`` that forwards every
        # ``get_fresh_access_token(user_id)`` over NATS RPC instead.
        # Wired unconditionally — when OIDC isn't configured the
        # orchestrator-side responder is absent, the RPC times out,
        # and the proxy degrades to ""skip Bearer injection"" — the
        # same posture as if ``_token_vault`` were None.
        set_token_vault(RemoteTokenVault(nats_client))
        logger.info("gateway: RemoteTokenVault wired")

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
