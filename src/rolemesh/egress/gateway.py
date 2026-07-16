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

Startup is *degraded-but-serving* (docs/21-container-runtime-decoupling
§5): once NATS is connected, every listener (reverse proxy with
/healthz, forward proxy, DNS) binds immediately without waiting for the
rule or MCP snapshots. The snapshot responders live in the
orchestrator, and in the compose deployment the gateway starts first —
blocking on a snapshot would deadlock the whole stack. Until the rule
snapshot arrives the policy plane is deny-all (an unseeded PolicyCache
makes the safety caller block every request); until the MCP snapshot
arrives ``/mcp-proxy`` answers 404. Each plane has a background task
that retries its snapshot RPC with exponential backoff; the MCP task
then keeps re-fetching the snapshot every _RECONCILE_INTERVAL_S — core
NATS deltas are at-most-once, so the periodic full reconcile, not the
delta stream, is what guarantees the registry converges (see
``_seed_and_reconcile_mcp``). /healthz stays 200 throughout (health
means "NATS connected + listeners bound"); its body reports
``rules_seeded`` / ``mcp_seeded`` / ``mcp_entries``, and ``status`` is
"ok" only when both planes are seeded.

Hard prerequisites stay fail-closed: a NATS that won't connect or a
missing EGRESS_TOKEN_SECRET raises out of ``main``, exits non-zero,
and triggers the container's ``restart: unless-stopped`` policy.
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
import contextlib
import os
import signal
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from rolemesh.core.config import (
    CREDENTIAL_PROXY_PORT,
    EGRESS_GATEWAY_DNS_PORT,
    EGRESS_GATEWAY_FORWARD_PORT,
    NATS_URL,
)
from rolemesh.core.logger import get_logger

from .dns_internal import build_internal_exemption, parse_resolv_conf
from .dns_policy import GlobalDnsPolicy
from .dns_resolver import DnsServer, InternalMatcher, UpstreamResolver
from .forward_proxy import ForwardProxy
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
from .remote_credentials import RemoteCredentialResolver
from .remote_token_vault import RemoteTokenVault
from .reverse_proxy import (
    is_known_provider_host,
    is_registered_mcp_origin,
    known_provider_endpoints,
    set_token_vault,
    start_credential_proxy,
)
from .safety_call import AuditPublisher, EgressSafetyCaller
from .token_identity import TokenAuthority

if TYPE_CHECKING:
    import nats.aio.client

logger = get_logger()


# Gateway-side env knobs. All three are optional so a default build
# runs end-to-end without extra config; override in production.
#
# EGRESS_UPSTREAM_DNS: comma-separated list of DNS resolvers the
#   authoritative resolver recurses to on allow. Defaults match the
#   public internet resolvers that respond fastest under typical
#   workloads.
# EGRESS_SNAPSHOT_TIMEOUT: per-attempt NATS request-reply timeout for
#   the rule / MCP snapshot RPCs.
# EGRESS_DNS_ALLOWLIST / EGRESS_DNS_MODE: platform DNS policy — see
#   dns_policy.py for semantics and the empty-by-default rationale.
_UPSTREAM_DNS_DEFAULT = "8.8.8.8,1.1.1.1"
_SNAPSHOT_TIMEOUT_S = 5.0

# Exponential-backoff schedule for the degraded-startup rule-snapshot
# retry loop. Deliberately module-level constants, not config.py knobs:
# the values only matter for how fast the gateway leaves the deny-all
# window after the orchestrator comes up, and tests patch them directly.
_SNAPSHOT_RETRY_INITIAL_S = 1.0
_SNAPSHOT_RETRY_MAX_S = 30.0

# Steady-state interval for the MCP registry reconcile loop (same
# module-level-constant rationale as the retry schedule above). Bounds
# how long a lost ``egress.mcp.changed`` delta can keep the registry
# stale.
_RECONCILE_INTERVAL_S = 300.0


def _derive_internal_exemption(
    resolv_path: str = "/etc/resolv.conf",
) -> tuple[InternalMatcher | None, list[UpstreamResolver] | None]:
    """Build the internal-name DNS exemption from the gateway's own
    resolv.conf (egress/dns_internal.py — runtime-agnostic: kube-dns on
    K8s, 127.0.0.11 on Docker).

    Returns ``(None, None)`` — exemption disabled, every name stays on the
    allowlist path — when resolv.conf is unreadable or yields nothing
    internal. That is fail-closed but degraded: internal names like
    ``nats`` will NXDOMAIN, so the failure is logged at WARNING with the
    parsed contents for an operator to diagnose.
    """
    try:
        with open(resolv_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        logger.warning(
            "dns: cannot read resolv.conf for internal-name exemption — "
            "agents may fail to resolve internal services (nats)",
            path=resolv_path,
            error=str(exc),
        )
        return None, None

    resolv = parse_resolv_conf(text)
    # Same knob the orchestrator stamps the agent pod search domains with
    # (k8s_runtime._CLUSTER_DOMAIN), so the two cannot disagree about what
    # "internal" means.
    cluster_domain = os.environ.get("ROLEMESH_K8S_CLUSTER_DOMAIN", "cluster.local")
    exemption = build_internal_exemption(resolv, cluster_domain=cluster_domain)
    if exemption is None:
        logger.warning(
            "dns: no internal-name exemption derived from resolv.conf — "
            "agents may fail to resolve internal services (nats)",
            nameservers=list(resolv.nameservers),
            search=list(resolv.search),
        )
        return None, None

    matcher, internal_upstreams = exemption
    logger.info(
        "dns: internal-name exemption active",
        internal_upstreams=[f"{u.host}:{u.port}" for u in internal_upstreams],
        cluster_domain=cluster_domain,
    )
    return matcher, internal_upstreams


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


async def _seed_rules_with_retry(
    nats_client: nats.aio.client.Client,
    cache: PolicyCache,
) -> None:
    """Retry the rule-snapshot RPC until it succeeds, then seed *cache*.

    Runs as a background task for the whole degraded-startup window.
    Exponential backoff starting at _SNAPSHOT_RETRY_INITIAL_S, capped at
    _SNAPSHOT_RETRY_MAX_S. Every failure logs a warning (the operator's
    signal that the orchestrator is not answering yet); success logs an
    info and ends the task. Cancellation (gateway shutdown) propagates.
    """
    delay = _SNAPSHOT_RETRY_INITIAL_S
    attempt = 0
    while True:
        attempt += 1
        try:
            snapshot = await fetch_snapshot_via_nats(nats_client, timeout_s=_SNAPSHOT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any RPC failure means "retry"
            logger.warning(
                "gateway: rule snapshot fetch failed — policy plane stays default-deny, retrying",
                attempt=attempt,
                retry_in_s=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _SNAPSHOT_RETRY_MAX_S)
            continue
        await cache.seed(snapshot)
        logger.info(
            "gateway: rule snapshot seeded — leaving degraded mode",
            attempt=attempt,
            rule_count=len(snapshot),
        )
        return


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    """AsyncExitStack callback: cancel *task* and await it quietly."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _log_task_death(task: asyncio.Task[Any]) -> None:
    """Done-callback for the background sync tasks.

    A task that dies on an unexpected exception (not cancellation)
    would otherwise vanish silently — asyncio only surfaces the
    exception at GC time, and /healthz keeps reporting the last seeded
    state while seeding/reconciling has actually stopped. There is no
    in-task recovery for a bug-grade failure; this ERROR log is the
    operator's signal to restart the gateway.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "gateway: background sync task died — its state stays frozen until restart",
            task=task.get_name(),
            error=repr(exc),
        )


class _McpSyncState:
    """Seed marker for the MCP registry sync, polled by /healthz.

    ``seeded`` means "the snapshot RPC has succeeded at least once",
    NOT "the registry is non-empty" — an empty registry is a legal
    state (no MCP servers configured yet) and must not read as
    degraded.
    """

    def __init__(self) -> None:
        self.seeded = False


async def _seed_and_reconcile_mcp(
    nats_client: nats.aio.client.Client,
    state: _McpSyncState,
) -> None:
    """Seed the MCP registry, then reconcile it against the
    orchestrator forever. One task, two phases:

    Seed — retry the snapshot RPC with the same exponential backoff as
    ``_seed_rules_with_retry`` until the first success, then set
    ``state.seeded``. Covers the cold-start race where the gateway is
    up before the orchestrator's responder.

    Reconcile — every ``_RECONCILE_INTERVAL_S``, re-fetch the snapshot
    and apply it as a full replacement (``apply_snapshot_to_registry``
    is replace-not-merge and idempotent). This loop is the registry's
    ONLY correctness mechanism: core NATS pub/sub is at-most-once, so
    ``egress.mcp.changed`` deltas dropped across a NATS reconnect
    would otherwise leave the registry stale until a restart. The
    delta subscription on top is purely a propagation-latency
    optimisation — losing every delta delays convergence by at most
    one interval. That bound also covers deletions: a removed MCP
    server may stay routable here for up to one interval, which is
    acceptable because this table is routing, not access control —
    egress rules gate what agents may actually reach.

    A failed cycle — fetch or apply — logs a warning and waits for the
    next attempt/interval; fast backoff is seed-phase-only behaviour.
    Apply sits inside the try deliberately: an exception escaping the
    loop would kill this task while /healthz keeps reporting the last
    seeded state — exactly the invisible degradation this task exists
    to prevent. Cancellation (gateway shutdown) propagates.
    """
    delay = _SNAPSHOT_RETRY_INITIAL_S
    attempt = 0
    while not state.seeded:
        attempt += 1
        try:
            entries = await fetch_mcp_snapshot_via_nats(nats_client, timeout_s=_SNAPSHOT_TIMEOUT_S)
            apply_snapshot_to_registry(entries)
        except Exception as exc:  # noqa: BLE001 — any failure means "retry"
            logger.warning(
                "gateway: MCP snapshot seed failed — /mcp-proxy serves 404s until seeded, retrying",
                attempt=attempt,
                retry_in_s=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _SNAPSHOT_RETRY_MAX_S)
            continue
        state.seeded = True
        logger.info(
            "gateway: MCP registry seeded — leaving degraded mode",
            attempt=attempt,
            count=len(entries),
        )

    while True:
        await asyncio.sleep(_RECONCILE_INTERVAL_S)
        try:
            entries = await fetch_mcp_snapshot_via_nats(nats_client, timeout_s=_SNAPSHOT_TIMEOUT_S)
            apply_snapshot_to_registry(entries)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gateway: MCP reconcile failed — registry may be stale until next interval",
                error=str(exc),
            )
            continue
        logger.debug("gateway: MCP registry reconciled", count=len(entries))


async def _start_mcp_sync(
    nats_client: nats.aio.client.Client,
    stack: AsyncExitStack,
) -> _McpSyncState:
    """Wire the MCP registry sync: subscribe to change deltas FIRST,
    then start the background seed+reconcile task.

    Subscription-before-snapshot mirrors the rule-side ordering above
    so no delta can fall between snapshot generation and subscription.
    Unlike PolicyCache the registry dict has no unseeded deny-all gate,
    so a delta that lands while a snapshot reply is in flight can be
    overwritten by the slightly older snapshot — at seed time and on
    every reconcile fetch alike. Accepted (the rule side carries the
    same theoretical race): the periodic reconcile bounds any such
    loss to one interval.
    """
    mcp_sub = await subscribe_mcp_changes(nats_client)
    stack.push_async_callback(mcp_sub.unsubscribe)  # type: ignore[attr-defined]
    state = _McpSyncState()
    task = asyncio.create_task(
        _seed_and_reconcile_mcp(nats_client, state),
        name="egress-mcp-snapshot-seed",
    )
    task.add_done_callback(_log_task_death)
    stack.push_async_callback(_cancel_task, task)
    return state


async def main() -> None:
    upstream_dns = _parse_upstream_dns(os.environ.get("EGRESS_UPSTREAM_DNS", _UPSTREAM_DNS_DEFAULT))

    # Platform DNS policy. Loaded before any network setup so a config
    # typo (bad EGRESS_DNS_MODE) kills the boot immediately — same
    # fail-closed posture as the token-authority secret check below.
    dns_policy = GlobalDnsPolicy.from_env()

    # Internal-name exemption: internal service names (nats, the gateway,
    # *.cluster.local) bypass the allowlist and forward to the platform's
    # own resolver. Derived from the gateway's own resolv.conf so the same
    # code is correct on Docker and K8s (egress/dns_internal.py).
    internal_matcher, internal_upstreams = _derive_internal_exemption()

    # Import nats-py lazily so unit tests importing this module don't
    # require the dependency. The gateway Dockerfile pins it, so
    # production always finds it.
    import nats  # type: ignore[import-not-found]

    nats_client = await nats.connect(NATS_URL)
    logger.info("gateway connected to NATS", url=NATS_URL)

    stop = asyncio.Event()

    async with AsyncExitStack() as stack:
        stack.push_async_callback(nats_client.close)

        # --- Policy cache: degraded start + hot reload ---------------
        # Subscribe to rule changes BEFORE the snapshot seed so there is
        # no event gap between snapshot generation and subscription.
        # PolicyCache supports this ordering: events applied in the
        # degraded window cannot allow traffic (unseeded cache ⇒ the
        # safety caller denies everything), and seed() is the
        # authoritative full overwrite once it lands; events after seed
        # apply incrementally as usual.
        cache = PolicyCache()
        rule_sub = await subscribe_rule_changes(nats_client, cache)
        stack.push_async_callback(rule_sub.unsubscribe)  # type: ignore[attr-defined]

        # Background snapshot retry (docs/21 §5): the responder lives in
        # the orchestrator, which starts AFTER the gateway in compose —
        # blocking here would deadlock the stack. Listeners come up now;
        # the policy plane stays default-deny until this task seeds.
        seed_task = asyncio.create_task(
            _seed_rules_with_retry(nats_client, cache),
            name="egress-rule-snapshot-seed",
        )
        seed_task.add_done_callback(_log_task_death)
        stack.push_async_callback(_cancel_task, seed_task)

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

        # Platform-managed provider allow layer: known LLM-provider hosts
        # are always permitted egress so a tenant's BYOK credential works
        # without hand-configuring an egress allowlist. Registered MCP
        # server origins get the same reverse-only treatment via
        # ``mcp_allow`` — adding an MCP server through the admin API is
        # itself the egress approval, no hand-written egress.domain_rule
        # needed. Per-tenant rules in the policy cache still govern any
        # custom egress.
        safety = EgressSafetyCaller(
            cache=cache,
            checks=check_map,
            audit_publisher=audit,
            platform_allow=is_known_provider_host,
            mcp_allow=is_registered_mcp_origin,
        )
        logger.info(
            "gateway: platform provider allowlist active",
            endpoints=sorted(f"{h}:{p}" for h, p in known_provider_endpoints()),
            bedrock="bedrock-runtime.<region>.amazonaws.com:443",
        )

        # --- MCP server registry: subscribe + seed + reconcile --------
        # Same degraded-start shape as the policy cache above: listeners
        # bind now, /mcp-proxy answers 404 until the first snapshot
        # lands, and /healthz reports mcp_seeded=false in the meantime.
        # Ordering and correctness rationale live on _start_mcp_sync /
        # _seed_and_reconcile_mcp.
        mcp_state = await _start_mcp_sync(nats_client, stack)

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

        # --- Credential resolver (remote via NATS) ------------------
        # Gateway ships without rolemesh.db / rolemesh.auth (EC-1
        # stateless boundary). Each credential lookup forwards to the
        # orchestrator's start_credential_responder over NATS.
        credential_resolver = RemoteCredentialResolver(nats_client)

        # --- Token authority (token-identity refactor) --------------
        # Verifies the signed identity token agents carry in their proxy
        # env. Shares EGRESS_TOKEN_SECRET with the orchestrator (same
        # bind-mounted .env). Fail-closed: a missing secret raises here
        # and the gateway refuses to boot — unlike the rule snapshot,
        # this is pure local config and cannot be retried into
        # existence. The IP resolver stays wired as the dual-run
        # fallback.
        token_authority = TokenAuthority.from_env()

        # --- Reverse proxy (port 3001) -------------------------------
        reverse_runner = await start_credential_proxy(
            port=CREDENTIAL_PROXY_PORT,
            host="0.0.0.0",
            credential_resolver=credential_resolver,
            safety_caller=safety,
            token_authority=token_authority,
            # /healthz stays 200 while degraded; the body's "status"
            # flips to "ok" once BOTH background snapshot tasks have
            # seeded their plane (rules_seeded and mcp_seeded).
            rules_seeded=lambda: cache.seeded,
            mcp_seeded=lambda: mcp_state.seeded,
        )
        stack.push_async_callback(reverse_runner.cleanup)

        # --- Forward proxy (port 3128) -------------------------------
        forward = ForwardProxy(
            safety_caller=safety,
            token_authority=token_authority,
        )
        forward_server = await forward.serve("0.0.0.0", EGRESS_GATEWAY_FORWARD_PORT)

        async def _close_forward() -> None:
            forward_server.close()
            await forward_server.wait_closed()

        stack.push_async_callback(_close_forward)

        # --- DNS resolver (port 53) ----------------------------------
        # Platform-wide policy, no identity: see dns_policy.py for why
        # the DNS plane is the one place per-tenant scoping buys nothing.
        dns = DnsServer(
            policy=dns_policy,
            upstreams=upstream_dns,
            internal_matcher=internal_matcher,
            internal_upstreams=internal_upstreams,
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
            dns_mode=dns_policy.mode,
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
