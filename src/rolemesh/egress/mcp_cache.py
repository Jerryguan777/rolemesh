"""MCP-server registry sync for the egress gateway (PR-egress-mcp-sync).

Mirrors the safety-rule sync layout (``policy_cache.py`` + the
publishers/responders in ``orch_glue.py``) but for the MCP server
registry that ``reverse_proxy.handle_mcp_proxy`` reads.

Why this exists
---------------
Before this module, ``register_mcp_server`` was called only on the
orchestrator process (``rolemesh.main`` walking ``coworker.tools`` at
startup). The gateway is a separate container with its own copy of
``reverse_proxy._mcp_registry`` — no IPC fed it, so every
``/mcp-proxy/<name>/<path>`` request returned ``404 MCP server not
found``.

This module fills that gap with the same NATS pattern safety rules
use:

  - ``egress.mcp.snapshot.request`` (request-reply): gateway boots,
    fetches the orchestrator's current registry, seeds itself.
  - ``egress.mcp.changed`` (broadcast): orchestrator pushes deltas
    so the gateway can hot-reload without a restart.

The gateway end is intentionally thin: it just translates each
event into a ``register_mcp_server`` / ``unregister_mcp_server``
call against the existing module-level dict in ``reverse_proxy``.
No new cache layer — the dict that ``handle_mcp_proxy`` already
reads is the cache.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    import nats.aio.client

logger = get_logger()


# NATS subjects owned by the MCP-registry sync. Naming mirrors
# ``egress.rules.snapshot.request`` / ``safety.rule.changed`` so an
# operator skimming subjects can group them.
MCP_SNAPSHOT_REQUEST_SUBJECT = "egress.mcp.snapshot.request"
MCP_CHANGED_SUBJECT = "egress.mcp.changed"


@dataclass(frozen=True)
class McpEntry:
    """Wire-format MCP server entry.

    ``url`` carries the scheme://host:port origin form (no path). The
    orchestrator computes it as ``urlparse(tool.url).scheme + ://
    + .netloc`` before publishing — same shape ``register_mcp_server``
    has stored historically.
    """

    name: str
    url: str
    headers: dict[str, str]
    auth_mode: str


def entry_to_dict(entry: McpEntry) -> dict[str, Any]:
    """Serialize for the wire / responders."""
    return {
        "name": entry.name,
        "url": entry.url,
        "headers": dict(entry.headers),
        "auth_mode": entry.auth_mode,
    }


def entry_from_dict(d: dict[str, Any]) -> McpEntry:
    """Parse one wire entry. Raises on missing required keys; the
    snapshot/event handlers catch + log so a single malformed entry
    can't poison the rest."""
    return McpEntry(
        name=str(d["name"]),
        url=str(d["url"]),
        headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()},
        auth_mode=str(d.get("auth_mode") or "user"),
    )


# ---------------------------------------------------------------------------
# Gateway-side: snapshot fetch + live subscription
# ---------------------------------------------------------------------------


async def fetch_mcp_snapshot_via_nats(
    nats_client: nats.aio.client.Client,
    *,
    timeout_s: float = 5.0,
) -> list[McpEntry]:
    """Request the orchestrator's current MCP registry over NATS.

    Mirrors ``egress.policy_cache.fetch_snapshot_via_nats``. Core NATS
    request-reply because the snapshot is a one-shot — a missed reply
    should surface as a timeout, not be persisted and replayed.
    """
    response = await nats_client.request(  # type: ignore[attr-defined]
        MCP_SNAPSHOT_REQUEST_SUBJECT,
        b"",
        timeout=timeout_s,
    )
    payload = json.loads(response.data)
    raw = payload.get("entries")
    if not isinstance(raw, list):
        raise ValueError(
            f"Unexpected MCP snapshot shape: expected list under 'entries', "
            f"got {type(raw).__name__}"
        )
    out: list[McpEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            logger.warning("mcp_cache: skipping non-dict snapshot entry")
            continue
        try:
            out.append(entry_from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "mcp_cache: skipping malformed snapshot entry",
                error=str(exc),
            )
    return out


def apply_snapshot_to_registry(entries: list[McpEntry]) -> None:
    """Seed ``reverse_proxy._mcp_registry`` from a snapshot.

    Replaces every entry rather than merging — the snapshot is the
    authoritative current state on the orchestrator side, and merging
    would leave stale entries around if the orchestrator removed a
    coworker while the gateway was offline. Names not in the snapshot
    are dropped.
    """
    # Local import: keeps the cache module importable from contexts
    # that don't actually want to bring in aiohttp transitively (e.g.
    # tests of just the wire format).
    from rolemesh.egress.reverse_proxy import (
        get_mcp_registry,
        register_mcp_server,
        unregister_mcp_server,
    )

    new_names = {e.name for e in entries}
    for stale in set(get_mcp_registry()) - new_names:
        unregister_mcp_server(stale)
    for entry in entries:
        register_mcp_server(
            entry.name, entry.url, entry.headers, entry.auth_mode
        )


def apply_change_event(event: dict[str, Any]) -> None:
    """Apply one ``egress.mcp.changed`` event to the registry.

    Event shape:
        {"action": "created" | "updated" | "deleted",
         "name": "<name>",
         "url": "<origin>",
         "headers": {...},
         "auth_mode": "user" | "service" | "both"}

    For ``deleted``, only ``name`` is required; other fields are
    ignored if present.
    """
    from rolemesh.egress.reverse_proxy import (
        register_mcp_server,
        unregister_mcp_server,
    )

    action = event.get("action")
    name = event.get("name")
    if not isinstance(name, str) or not name:
        # structlog reserves the `event` kwarg for the log message; use
        # `payload` to surface the offending dict without colliding.
        logger.warning("mcp_cache: change event missing 'name'", payload=event)
        return

    if action == "deleted":
        unregister_mcp_server(name)
        return

    if action not in ("created", "updated"):
        logger.warning("mcp_cache: unknown action", action=action, name=name)
        return

    try:
        entry = entry_from_dict(event)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "mcp_cache: malformed change event",
            error=str(exc),
            name=name,
        )
        return
    register_mcp_server(entry.name, entry.url, entry.headers, entry.auth_mode)


async def subscribe_mcp_changes(
    nats_client: nats.aio.client.Client,
) -> object:
    """Subscribe to ``MCP_CHANGED_SUBJECT`` and apply events as they arrive.

    Returns the subscription handle so the caller can ``await
    sub.unsubscribe()`` at shutdown. Modeled after
    ``policy_cache.subscribe_rule_changes``.
    """
    async def _handler(msg: object) -> None:
        try:
            event = json.loads(msg.data)  # type: ignore[attr-defined]
        except (ValueError, AttributeError) as exc:
            logger.warning("mcp_cache: non-JSON change event", error=str(exc))
            return
        if not isinstance(event, dict):
            logger.warning(
                "mcp_cache: change event not a dict",
                got=type(event).__name__,
            )
            return
        with contextlib.suppress(Exception):
            apply_change_event(event)

    sub = await nats_client.subscribe(MCP_CHANGED_SUBJECT, cb=_handler)  # type: ignore[attr-defined]
    logger.info("mcp_cache: subscribed", subject=MCP_CHANGED_SUBJECT)
    return sub
