"""Publisher for ``egress.mcp.changed`` deltas from the v1 surface.

Publishes against a single ``MCPServerRow`` (the ``/mcp-servers``
surface). Owns the process-wide NATS handle the gateway hot-reload
broadcasts ride on: ``webui.main.lifespan`` installs it via
:func:`set_mcp_publisher` at boot and clears it on shutdown; callers
look it up lazily so they don't have to thread the handle through
every test fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.db import MCPServerRow

logger = get_logger()


__all__ = [
    "publish_mcp_server_changed",
    "publish_mcp_server_deleted",
    "set_mcp_publisher",
]


# Process-wide NATS client used for MCP registry-change broadcasts. Set
# from the WebUI bootstrap (``webui.main.lifespan``); ``None`` means
# hot-reload broadcasts are off (the gateway still gets a current
# snapshot at orchestrator boot, so functionality degrades gracefully —
# operators just wait for a gateway restart for tool edits to land).
_mcp_publisher: Any = None


def set_mcp_publisher(nc: Any) -> None:
    """Attach or detach the process-wide NATS client used for MCP
    registry-change broadcasts.

    Type stays ``Any`` to keep ``nats`` types out of this module's
    import surface; the caller in ``webui.main`` already has the typed
    handle.
    """
    global _mcp_publisher
    _mcp_publisher = nc


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
    """Return the process-wide NATS client, or None when unset."""
    return _mcp_publisher


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
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to publish egress.mcp.changed (deleted)",
            name=name,
            exc_info=True,
        )
