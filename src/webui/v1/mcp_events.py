"""Publisher for ``egress.mcp.changed`` deltas from the v1 surface.

Mirrors :func:`webui.admin._publish_mcp_for_coworker` but works
against a single ``MCPServerRow`` rather than a coworker's tool
list. Reuses the same core-NATS connection ``webui.main.lifespan``
already installs via :func:`webui.admin.set_mcp_publisher` — we
look it up lazily so callers don't have to thread the handle
through every test fixture.
"""

from __future__ import annotations

from urllib.parse import urlparse

from rolemesh.core.logger import get_logger
from rolemesh.db import MCPServerRow

logger = get_logger()


__all__ = [
    "publish_mcp_server_changed",
    "publish_mcp_server_deleted",
]


def _build_entry(row: MCPServerRow):
    """Translate a DB row into the wire shape consumed by the gateway."""
    from rolemesh.container.runtime import rewrite_loopback_to_host_gateway
    from rolemesh.egress.mcp_cache import McpEntry

    parsed = urlparse(row.url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else row.url
    return McpEntry(
        name=row.name,
        url=rewrite_loopback_to_host_gateway(origin),
        headers={str(k): str(v) for k, v in (row.extra_headers or {}).items()},
        auth_mode=row.auth_mode,
    )


def _get_publisher():
    """Return the NATS client the admin module already owns, or None.

    Lazy import keeps a circular reference at bay (admin imports from
    v1.coworkers in turn).
    """
    from webui import admin

    return admin._mcp_publisher  # noqa: SLF001


async def publish_mcp_server_changed(*, action: str, row: MCPServerRow) -> None:
    """Publish ``egress.mcp.changed`` for a created / updated MCP server.

    Best-effort: a publish failure logs at WARN but does not break
    the HTTP response — the DB row is the source of truth and the
    gateway's snapshot fetch on next boot reconciles.
    """
    nc = _get_publisher()
    if nc is None:
        logger.debug(
            "mcp_events: publisher unset; skipping",
            action=action,
            mcp_id=row.id,
        )
        return
    from rolemesh.egress.orch_glue import publish_mcp_registry_changed

    try:
        await publish_mcp_registry_changed(
            nc, action=action, entry=_build_entry(row),
        )
    except Exception:
        logger.warning(
            "Failed to publish egress.mcp.changed",
            action=action,
            mcp_id=row.id,
            exc_info=True,
        )


async def publish_mcp_server_deleted(*, name: str) -> None:
    """Publish ``egress.mcp.changed action=deleted`` for ``name``."""
    nc = _get_publisher()
    if nc is None:
        return
    from rolemesh.egress.orch_glue import publish_mcp_registry_changed

    try:
        await publish_mcp_registry_changed(nc, action="deleted", name=name)
    except Exception:
        logger.warning(
            "Failed to publish egress.mcp.changed (deleted)",
            name=name,
            exc_info=True,
        )
