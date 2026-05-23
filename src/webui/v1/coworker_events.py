"""Publisher for ``web.coworker.*`` hot-reload events.

Lives in :mod:`webui.v1` (next to the handler that triggers it) so
the lifecycle test for the PATCH handler can swap the publisher
without poking globals across packages. The webui process owns the
NATS / JetStream connection (created in ``webui.main.lifespan``);
this module is the thin shim that lets the handler stay free of
NATS imports.

A ``None`` publisher silently degrades — operators still see the
config in the DB and a full orchestrator restart picks it up. The
subscriber on the orchestrator side is the load-bearing path; this
publisher is best-effort.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger
from rolemesh.orchestration.coworker_hot_reload import (
    WEB_COWORKER_MCP_CHANGED_SUBJECT,
    WEB_COWORKER_RESTART_SUBJECT,
    WEB_COWORKER_SKILLS_CHANGED_SUBJECT,
)

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

logger = get_logger()


_js: "JetStreamContext | None" = None


def set_jetstream(js: "JetStreamContext | None") -> None:
    """Attach or detach the process-wide JetStream context.

    Called from ``webui.main.lifespan`` after the connection comes
    up, and again with ``None`` on shutdown so tests that mount the
    router after a previous suite tore the connection down don't
    leak references to a closed context.
    """
    global _js
    _js = js


async def publish_coworker_restart(
    *, coworker_id: str, tenant_id: str
) -> None:
    """Publish ``web.coworker.restart`` for ``(coworker_id, tenant_id)``.

    Best-effort: a publish failure logs at WARN but does not abort
    the caller. The PATCH handler that triggers this has already
    committed the DB change; a missed broadcast just means operators
    see the new config on the next full state refresh.
    """
    if _js is None:
        logger.debug(
            "coworker.restart publisher unset; skipping broadcast",
            coworker_id=coworker_id,
            tenant_id=tenant_id,
        )
        return
    payload = json.dumps({"coworker_id": coworker_id, "tenant_id": tenant_id})
    try:
        await _js.publish(WEB_COWORKER_RESTART_SUBJECT, payload.encode("utf-8"))
    except Exception:
        logger.warning(
            "Failed to publish web.coworker.restart; relying on next restart",
            coworker_id=coworker_id,
            tenant_id=tenant_id,
            exc_info=True,
        )


async def publish_coworker_skills_changed(
    *, coworker_id: str, tenant_id: str,
) -> None:
    """Publish ``web.coworker.skills_changed`` for ``(coworker_id, tenant_id)``.

    Fires from the v1 skills + ``coworker_skills`` mutating endpoints.
    Same best-effort posture as :func:`publish_coworker_restart`. The
    orchestrator-side subscriber lives in
    :func:`rolemesh.orchestration.coworker_hot_reload.subscribe_coworker_skills_changed`
    and refreshes ``CoworkerState.skills`` from the per-tenant
    catalog so the next container spawn sees the new projection.
    """
    if _js is None:
        logger.debug(
            "coworker.skills_changed publisher unset; skipping broadcast",
            coworker_id=coworker_id,
            tenant_id=tenant_id,
        )
        return
    payload = json.dumps({"coworker_id": coworker_id, "tenant_id": tenant_id})
    try:
        await _js.publish(
            WEB_COWORKER_SKILLS_CHANGED_SUBJECT, payload.encode("utf-8"),
        )
    except Exception:
        logger.warning(
            "Failed to publish web.coworker.skills_changed",
            coworker_id=coworker_id,
            tenant_id=tenant_id,
            exc_info=True,
        )


async def publish_coworker_mcp_changed(
    *, coworker_id: str, tenant_id: str,
) -> None:
    """Publish ``web.coworker.mcp_changed`` for ``(coworker_id, tenant_id)``.

    Fires from the relation layer (bind / unbind / patch enabled_tools).
    Same best-effort posture as :func:`publish_coworker_restart`. The
    orchestrator-side subscriber lives in
    :func:`rolemesh.orchestration.coworker_hot_reload.subscribe_coworker_mcp_changed`
    and refreshes ``CoworkerState.mcp_configs`` from the relation
    layer so the next message routed at the coworker sees the new
    bindings without a process restart.
    """
    if _js is None:
        logger.debug(
            "coworker.mcp_changed publisher unset; skipping broadcast",
            coworker_id=coworker_id,
            tenant_id=tenant_id,
        )
        return
    payload = json.dumps({"coworker_id": coworker_id, "tenant_id": tenant_id})
    try:
        await _js.publish(
            WEB_COWORKER_MCP_CHANGED_SUBJECT, payload.encode("utf-8"),
        )
    except Exception:
        logger.warning(
            "Failed to publish web.coworker.mcp_changed",
            coworker_id=coworker_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
