"""Orchestrator-side glue for the egress gateway (EC-2).

Two kinds of plumbing live here:

  1. NATS responders — serve the gateway's initial-state RPCs.
     * ``egress.rules.snapshot.request`` → current egress-stage rules
     * ``egress.mcp.snapshot.request``   → current MCP registry

  2. NATS publishers — push deltas to the gateway after the initial
     snapshot. Module-level helpers so existing orchestrator code
     (container_executor, webui/admin) can import one function without
     dragging NATS client types through their signatures.

The gateway always reads snapshot → subscribe to deltas in that order.
Any event that lands before the snapshot is idempotent (rule events on
rule_id, MCP events on name), so a benign race between the two
operations just leads to one duplicate apply.

Keeping these adapters in a single file (rather than scattered across
container_executor / webui / main) makes it easy to reason about the
egress control plane as a unit — the single file is where every new
subject is introduced, logged, unsubscribed on shutdown.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger
from rolemesh.db import (
    get_all_tenants,
    list_safety_rules,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nats.aio.client

logger = get_logger()

# Subject names — imported so there's a single authoritative string for
# each subject. Duplicate definitions would drift.
from .mcp_cache import (  # noqa: E402
    MCP_CHANGED_SUBJECT,
    MCP_SNAPSHOT_REQUEST_SUBJECT,
    McpEntry,
    entry_to_dict,
)
from .policy_cache import RULE_CHANGED_SUBJECT, SNAPSHOT_REQUEST_SUBJECT  # noqa: E402
from .remote_credentials import CREDENTIAL_REQUEST_SUBJECT  # noqa: E402
from .remote_token_vault import TOKEN_ACCESS_REQUEST_SUBJECT  # noqa: E402

# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------


async def publish_mcp_registry_changed(
    nc: nats.aio.client.Client,
    *,
    action: str,
    entry: McpEntry | None = None,
    name: str | None = None,
) -> None:
    """Push a single MCP registry delta to the gateway.

    For ``created`` / ``updated``: pass ``entry`` with the full payload.
    For ``deleted``: pass ``name`` (the only field the consumer needs).

    Best-effort publish: a failure here means the gateway misses one
    delta, but its next snapshot fetch on restart still recovers full
    state.
    """
    if action == "deleted":
        if not name:
            logger.warning("mcp_registry publish: deleted without name")
            return
        payload: dict[str, Any] = {"action": "deleted", "name": name}
    else:
        if entry is None:
            logger.warning(
                "mcp_registry publish: missing entry",
                action=action,
            )
            return
        payload = {"action": action, **entry_to_dict(entry)}
    try:
        await nc.publish(MCP_CHANGED_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mcp_registry publish failed",
            error=str(exc),
            action=action,
        )


async def publish_rule_changed(
    nc: nats.aio.client.Client,
    *,
    action: str,
    rule: dict[str, Any],
) -> None:
    """Publish a ``safety.rule.changed`` event.

    ``rule`` is the to_snapshot_dict() view of a SafetyRule row. We
    flatten into a single JSON object with ``rule_id`` mirroring
    ``id`` for consumer-side convenience (the policy_cache expects
    either key).
    """
    payload: dict[str, Any] = {"action": action, **rule}
    payload.setdefault("rule_id", payload.get("id"))
    try:
        await nc.publish(RULE_CHANGED_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule.changed publish failed", error=str(exc), action=action)


# ---------------------------------------------------------------------------
# Responders (served from the orchestrator startup path)
# ---------------------------------------------------------------------------


async def start_responders(
    nc: nats.aio.client.Client,
    *,
    rules_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]],
    mcp_fetcher: Callable[[], Awaitable[list[McpEntry]]] | None = None,
) -> list[object]:
    """Subscribe to all snapshot-request subjects and return sub handles.

    Caller is responsible for ``await sub.unsubscribe()`` at shutdown.
    Kept fire-and-forget rather than using JetStream: these are
    request-reply RPCs, so a missed reply surfaces as a gateway timeout
    and the gateway retries at its own cadence.

    ``rules_fetcher`` is injected so tests can feed a canned list
    without standing up the DB. Production wires it to
    ``fetch_all_egress_rules`` below.

    ``mcp_fetcher`` defaults to ``fetch_all_mcp_servers``, which
    reads the orchestrator's process-local ``_mcp_registry``. Tests
    can pass a stub. ``None`` (default) wires the production fetcher
    rather than skipping the responder, because a missing responder
    would cause every gateway boot to time out.
    """
    if mcp_fetcher is None:
        mcp_fetcher = fetch_all_mcp_servers

    async def _rules_handler(msg: object) -> None:
        try:
            rules = await rules_fetcher()
            body = json.dumps({"rules": rules}).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 — never crash the subscriber loop
            logger.error("rules snapshot fetch failed", error=str(exc))
            body = json.dumps({"rules": [], "error": str(exc)}).encode("utf-8")
        with contextlib.suppress(Exception):
            await msg.respond(body)  # type: ignore[attr-defined]

    async def _mcp_handler(msg: object) -> None:
        try:
            entries = await mcp_fetcher()
            body = json.dumps(
                {"entries": [entry_to_dict(e) for e in entries]}
            ).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.error("mcp snapshot fetch failed", error=str(exc))
            body = json.dumps({"entries": [], "error": str(exc)}).encode("utf-8")
        with contextlib.suppress(Exception):
            await msg.respond(body)  # type: ignore[attr-defined]

    rules_sub = await nc.subscribe(SNAPSHOT_REQUEST_SUBJECT, cb=_rules_handler)
    mcp_sub = await nc.subscribe(MCP_SNAPSHOT_REQUEST_SUBJECT, cb=_mcp_handler)
    logger.info(
        "egress responders subscribed",
        subjects=[
            SNAPSHOT_REQUEST_SUBJECT,
            MCP_SNAPSHOT_REQUEST_SUBJECT,
        ],
    )
    return [rules_sub, mcp_sub]


async def fetch_all_mcp_servers() -> list[McpEntry]:
    """Production MCP fetcher: snapshot the orchestrator's in-process
    ``_mcp_registry``.

    The orchestrator already populates this dict in ``main`` by
    walking each ``CoworkerState.mcp_configs`` projection at startup
    (the ``coworker_mcp_servers`` JOIN ``mcp_servers`` view). We read
    the dict directly rather than re-walking the DB because the dict
    is the authoritative "what has the orchestrator registered" view
    that the live hot-reload publishers keep in lockstep.

    Bug 5 (2026-04-26): rewrite ``localhost`` / ``127.0.0.1`` in each
    URL to ``host.docker.internal`` BEFORE serialising. The
    orchestrator stores raw origins (``localhost`` legitimately means
    the host inside this process), but the gateway running in a
    container will dial its own loopback if it sees the literal
    string. Rewrite at the publish boundary so the in-process
    registry stays useful for the rollback / pre-EC-1 path that
    proxies through the host's credential proxy.
    """
    from rolemesh.container.runtime import rewrite_loopback_to_host_gateway
    from rolemesh.egress.reverse_proxy import get_mcp_registry

    out: list[McpEntry] = []
    for name, (url, headers, auth_mode) in get_mcp_registry().items():
        out.append(
            McpEntry(
                name=name,
                url=rewrite_loopback_to_host_gateway(url),
                headers=dict(headers),
                auth_mode=auth_mode,
            )
        )
    return out


async def start_token_responder(
    nc: nats.aio.client.Client,
    *,
    vault: Any,
) -> object:
    """Subscribe to ``egress.token.access.request`` and serve fresh
    access tokens out of the orchestrator's local TokenVault.

    The egress gateway runs without DB access; it can't decrypt or
    refresh OIDC tokens itself. The gateway's
    ``RemoteTokenVault.get_fresh_access_token`` forwards each request
    to this subject. The orchestrator already has a real
    ``TokenVault`` (built from env via ``create_vault_from_env``); we
    just plumb each RPC into it.

    ``vault`` is typed ``Any`` so this module doesn't import the real
    ``TokenVault`` (transitively pulls in ``rolemesh.db``); the
    duck-typed call is ``vault.get_fresh_access_token(user_id)``.

    Returns the subscription handle; caller is responsible for
    ``await sub.unsubscribe()`` at shutdown — same pattern as
    ``start_responders``.
    """
    async def _handler(msg: object) -> None:
        try:
            payload = json.loads(msg.data)  # type: ignore[attr-defined]
        except (ValueError, AttributeError) as exc:
            logger.warning("token responder: non-JSON request", error=str(exc))
            await _respond(msg, {"access_token": None, "error": "bad_json"})
            return
        if not isinstance(payload, dict):
            await _respond(msg, {"access_token": None, "error": "bad_payload"})
            return
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            await _respond(msg, {"access_token": None, "error": "missing_user_id"})
            return

        try:
            access_token = await vault.get_fresh_access_token(user_id)
        except Exception as exc:  # noqa: BLE001
            # vault.get_fresh_access_token already swallows IdP /
            # transport errors and returns None; this catch is for
            # programming errors so the subscriber loop survives.
            logger.error(
                "token responder: vault raised", user_id=user_id, error=str(exc)
            )
            await _respond(msg, {"access_token": None, "error": "vault_error"})
            return

        await _respond(msg, {"access_token": access_token})

    sub = await nc.subscribe(TOKEN_ACCESS_REQUEST_SUBJECT, cb=_handler)
    logger.info(
        "token responder subscribed",
        subject=TOKEN_ACCESS_REQUEST_SUBJECT,
    )
    return sub


async def _respond(msg: object, payload: dict[str, Any]) -> None:
    """Best-effort respond helper for token RPCs. A failure to
    respond degrades to a gateway-side timeout, which already maps
    to ``access_token=None`` (see RemoteTokenVault docstring), so
    we suppress the exception to keep the subscriber alive."""
    body = json.dumps(payload).encode("utf-8")
    with contextlib.suppress(Exception):
        await msg.respond(body)  # type: ignore[attr-defined]


async def start_credential_responder(
    nc: nats.aio.client.Client,
    *,
    resolver: Any,
) -> object:
    """Subscribe to ``egress.credential.request`` and serve credentials
    out of the orchestrator's local
    :class:`rolemesh.egress.credentials.CredentialResolver`.

    The egress gateway runs without DB access; it can't decrypt rows
    itself. The gateway's
    :class:`rolemesh.egress.remote_credentials.RemoteCredentialResolver`
    forwards each lookup to this subject. The orchestrator already
    has a real :class:`CredentialResolver` (DB + vault); we just plumb
    each RPC into it.

    ``resolver`` is typed ``Any`` so this module doesn't import the
    DB-backed resolver (transitively pulls in ``rolemesh.db``); the
    duck-typed call is ``resolver.resolve(tenant_id, provider)``
    returning a dict and raising
    :class:`rolemesh.egress.credentials.MissingCredentialError` on miss.

    Returns the subscription handle; caller is responsible for
    ``await sub.unsubscribe()`` at shutdown — same pattern as
    :func:`start_token_responder`.
    """
    from .credentials import MissingCredentialError

    async def _handler(msg: object) -> None:
        try:
            payload = json.loads(msg.data)  # type: ignore[attr-defined]
        except (ValueError, AttributeError) as exc:
            logger.warning(
                "credential responder: non-JSON request", error=str(exc),
            )
            await _respond(
                msg, {"credential": None, "error": "bad_json"},
            )
            return
        if not isinstance(payload, dict):
            await _respond(
                msg, {"credential": None, "error": "bad_payload"},
            )
            return
        tenant_id = payload.get("tenant_id")
        provider = payload.get("provider")
        if not isinstance(tenant_id, str) or not tenant_id:
            await _respond(
                msg, {"credential": None, "error": "missing_tenant_id"},
            )
            return
        if not isinstance(provider, str) or not provider:
            await _respond(
                msg, {"credential": None, "error": "missing_provider"},
            )
            return

        try:
            credential = await resolver.resolve(tenant_id, provider)
        except MissingCredentialError:
            await _respond(
                msg, {"credential": None, "error": "MISSING"},
            )
            return
        except Exception as exc:  # noqa: BLE001 — keep subscriber alive
            # InvalidToken (wrong master key) or any other vault /
            # DB-layer fault. Surface as a non-MISSING error so the
            # gateway maps to 502 not 401.
            logger.error(
                "credential responder: resolver raised",
                tenant_id=tenant_id,
                provider=provider,
                error=str(exc),
            )
            await _respond(
                msg, {"credential": None, "error": "resolver_error"},
            )
            return

        await _respond(msg, {"credential": credential})

    sub = await nc.subscribe(CREDENTIAL_REQUEST_SUBJECT, cb=_handler)
    logger.info(
        "credential responder subscribed",
        subject=CREDENTIAL_REQUEST_SUBJECT,
    )
    return sub


async def fetch_all_egress_rules() -> list[dict[str, Any]]:
    """Load every enabled rule with stage='egress_request' across all tenants.

    Production implementation of the ``rules_fetcher`` argument above.
    Scans all tenants because gateway is not sharded per-tenant — it
    sees every agent on the bridge. ``enabled=TRUE`` is filtered at
    the SQL layer to keep the snapshot lean.
    """

    tenants = await get_all_tenants()
    out: list[dict[str, Any]] = []
    for tenant in tenants:
        rows = await list_safety_rules(
            tenant.id, stage="egress_request", enabled=True
        )
        out.extend(r.to_snapshot_dict() for r in rows)
    return out
