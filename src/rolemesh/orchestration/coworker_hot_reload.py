"""Hot-reload pipeline for coworker config changes from the WebUI.

Design §7 lists ``web.coworker.restart`` as the JetStream event that
fires when a v1 PATCH changes the ``model_id`` (or any field that
forces the running agent container to be re-spawned). The webui
process is the publisher (see :mod:`webui.v1.coworker_events`); this
module is the subscriber inside the orchestrator process.

The minimum responsibility for v1.1 Phase 1 is:

* Re-read the coworker row from DB.
* Replace the cached ``CoworkerState.config`` in
  :class:`rolemesh.core.orchestrator_state.OrchestratorState`.

Stopping any currently-active container is *not* required to make a
``model_id`` swap take effect — the next request that wakes the
coworker uses the refreshed config. Container kill-on-reload is a
nice-to-have that would shorten the gap between "PATCH returns 200"
and "the new model is actually answering", but doing it cleanly
requires draining in-flight requests; pushed to a separate session
because nothing in 01a needs the latency floor.

Failure handling: a hot-reload that can't find the coworker row in
DB (deleted between publish and consume) is logged at WARN and the
message is ack'd anyway — re-delivery would loop forever. Anything
else (NATS hiccup, asyncpg blip) raises and the JetStream consumer's
redelivery retries the operation.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Awaitable, Callable

from rolemesh.core.logger import get_logger
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState

if TYPE_CHECKING:
    from nats.aio.msg import Msg as NatsMsg
    from nats.js.client import JetStreamContext
    from nats.js.subscription import Subscription

    from rolemesh.core.types import Coworker, McpServerConfig, Skill

logger = get_logger()


WEB_COWORKER_RESTART_SUBJECT = "web.coworker.restart"
# Coworker <-> MCP binding mutations. Published by the v1 relation
# endpoint (bind / unbind / patch enabled_tools); subscribed here so
# the in-memory ``CoworkerState.mcp_configs`` projection follows
# DB writes without waiting for the next process restart.
WEB_COWORKER_MCP_CHANGED_SUBJECT = "web.coworker.mcp_changed"
# Coworker <-> skill binding mutations + catalog edits. Published by
# the v1 skills + coworker_skills endpoints; subscribed here so the
# in-memory ``CoworkerState.skills`` projection follows DB writes
# without waiting for the next process restart (design §7 hot-load
# matrix). Same shape as ``mcp_changed`` — single-coworker scoped
# event with ``coworker_id`` and ``tenant_id``.
WEB_COWORKER_SKILLS_CHANGED_SUBJECT = "web.coworker.skills_changed"
_DURABLE = "orch-web-coworker-restart"
_MCP_DURABLE = "orch-web-coworker-mcp-changed"
_SKILLS_DURABLE = "orch-web-coworker-skills-changed"


async def reload_coworker_into_state(
    *,
    coworker_id: str,
    tenant_id: str,
    state: OrchestratorState,
    fetch_coworker: Callable[[str, str], Awaitable["Coworker | None"]],
    fetch_mcp_configs: (
        Callable[[str, str], Awaitable[list["McpServerConfig"]]] | None
    ) = None,
) -> bool:
    """Re-fetch the coworker row and replace the in-memory config.

    Returns ``True`` on success, ``False`` when the row is gone (the
    event refers to a coworker that has since been DELETEd). The
    boolean is consumed by the JetStream callback to decide whether to
    log at INFO or WARN; both paths still ack the message.

    The replacement preserves runtime-only state — conversations and
    channel_bindings — by mutating ``.config`` rather than swapping
    the ``CoworkerState`` instance whole. Swapping would orphan every
    in-flight conversation reference held by ``_message_loop``.

    When ``fetch_mcp_configs`` is provided the cached
    ``CoworkerState.mcp_configs`` projection is refreshed too; the
    callable is kept optional so callers that only swap the coworker
    row (model_id change) don't have to pay a second query.
    """
    cw = await fetch_coworker(coworker_id, tenant_id)
    if cw is None:
        return False

    mcp_configs: list["McpServerConfig"] | None = None
    if fetch_mcp_configs is not None:
        mcp_configs = await fetch_mcp_configs(coworker_id, tenant_id)

    cached = state.coworkers.get(coworker_id)
    if cached is None:
        # First time we hear about this coworker — fresh
        # ``CoworkerState`` is the only thing we can do. Conversations
        # / bindings will repopulate on first message via the existing
        # auto-create paths.
        state.coworkers[coworker_id] = CoworkerState.from_coworker(
            cw, mcp_configs=mcp_configs,
        )
        return True

    cached.config = cw
    if mcp_configs is not None:
        cached.mcp_configs = list(mcp_configs)
    # ``trigger_pattern`` is derived from the coworker's name; if a
    # PATCH changed the name we'd need to recompute. The v1 schema
    # forbids renaming via the same event path today, but rebuilding
    # is cheap — keeps the cache honest if the WebUI grows broader
    # hot-reload triggers.
    from rolemesh.core.orchestrator_state import build_trigger_pattern

    cached.trigger_pattern = build_trigger_pattern(cw.name)
    return True


async def reload_coworker_skills_into_state(
    *,
    coworker_id: str,
    tenant_id: str,
    state: OrchestratorState,
    fetch_skills: Callable[
        [str, str], Awaitable[list["Skill"]]
    ],
) -> bool:
    """Refresh only ``CoworkerState.skills`` for ``coworker_id``.

    Sibling of :func:`reload_coworker_mcp_into_state` for the
    ``web.coworker.skills_changed`` event — the coworker row itself
    didn't change, just the catalog skills bound to it. Skips the row
    fetch entirely. Returns ``False`` if the coworker isn't in state
    yet (the event would have been preceded by a ``restart`` if it
    were a brand-new coworker); the caller logs and acks regardless.
    """
    cached = state.coworkers.get(coworker_id)
    if cached is None:
        return False
    cached.skills = list(await fetch_skills(coworker_id, tenant_id))
    return True


async def reload_coworker_mcp_into_state(
    *,
    coworker_id: str,
    tenant_id: str,
    state: OrchestratorState,
    fetch_mcp_configs: Callable[
        [str, str], Awaitable[list["McpServerConfig"]]
    ],
) -> bool:
    """Refresh only ``CoworkerState.mcp_configs`` for ``coworker_id``.

    Sibling of :func:`reload_coworker_into_state` for the narrower
    ``web.coworker.mcp_changed`` event — the coworker row itself
    didn't change, just its junction-table bindings. Skips the row
    fetch entirely. Returns ``False`` if the coworker isn't in state
    yet (the event would have been preceded by a ``restart`` if it
    were a brand-new coworker); the caller logs and acks regardless.
    """
    cached = state.coworkers.get(coworker_id)
    if cached is None:
        return False
    cached.mcp_configs = list(
        await fetch_mcp_configs(coworker_id, tenant_id)
    )
    return True


async def subscribe_coworker_restart(
    js: "JetStreamContext",
    *,
    state: OrchestratorState,
    fetch_coworker: Callable[[str, str], Awaitable["Coworker | None"]],
    fetch_mcp_configs: (
        Callable[[str, str], Awaitable[list["McpServerConfig"]]] | None
    ) = None,
) -> "Subscription":
    """Subscribe to ``web.coworker.restart`` on JetStream.

    The caller owns the returned ``Subscription`` and is responsible
    for unsubscribing during shutdown. Manual ack ensures redelivery
    on transient failure; the callback ack's only after the in-memory
    reload completes (or is conclusively skipped).

    When ``fetch_mcp_configs`` is provided, the restart event also
    refreshes the cached MCP projection so a PATCH that swaps
    ``model_id`` plus tweaks the relation table in the same admin
    flow doesn't leave the cache half-stale.
    """

    async def _on_message(msg: "NatsMsg") -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "web.coworker.restart payload not JSON; dropping",
                data=msg.data[:128],
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        coworker_id = payload.get("coworker_id")
        tenant_id = payload.get("tenant_id")
        if not (isinstance(coworker_id, str) and isinstance(tenant_id, str)):
            logger.warning(
                "web.coworker.restart missing coworker_id/tenant_id; dropping",
                payload=payload,
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            ok = await reload_coworker_into_state(
                coworker_id=coworker_id,
                tenant_id=tenant_id,
                state=state,
                fetch_coworker=fetch_coworker,
                fetch_mcp_configs=fetch_mcp_configs,
            )
        except Exception:
            logger.exception(
                "web.coworker.restart handler failed; leaving for redelivery",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
            return

        if ok:
            logger.info(
                "Coworker config hot-reloaded from DB",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
        else:
            logger.warning(
                "web.coworker.restart for unknown coworker; skipping",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
        with contextlib.suppress(Exception):
            await msg.ack()

    return await js.subscribe(
        WEB_COWORKER_RESTART_SUBJECT,
        durable=_DURABLE,
        cb=_on_message,
        manual_ack=True,
    )


async def subscribe_coworker_mcp_changed(
    js: "JetStreamContext",
    *,
    state: OrchestratorState,
    fetch_mcp_configs: Callable[
        [str, str], Awaitable[list["McpServerConfig"]]
    ],
) -> "Subscription":
    """Subscribe to ``web.coworker.mcp_changed``.

    Mirrors :func:`subscribe_coworker_restart` but only refreshes the
    junction projection — the coworker row itself didn't change. A
    redelivery on transient failure leaves the cache at whatever
    state the previous handler observed; the next message that wakes
    the coworker re-reads anyway, so a missed broadcast degrades to
    "small staleness window" rather than "permanent divergence".
    """

    async def _on_message(msg: "NatsMsg") -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "web.coworker.mcp_changed payload not JSON; dropping",
                data=msg.data[:128],
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        coworker_id = payload.get("coworker_id")
        tenant_id = payload.get("tenant_id")
        if not (isinstance(coworker_id, str) and isinstance(tenant_id, str)):
            logger.warning(
                "web.coworker.mcp_changed missing ids; dropping",
                payload=payload,
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            ok = await reload_coworker_mcp_into_state(
                coworker_id=coworker_id,
                tenant_id=tenant_id,
                state=state,
                fetch_mcp_configs=fetch_mcp_configs,
            )
        except Exception:
            logger.exception(
                "web.coworker.mcp_changed handler failed; "
                "leaving for redelivery",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
            return

        if ok:
            logger.info(
                "Coworker MCP projection hot-reloaded",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
        else:
            logger.warning(
                "web.coworker.mcp_changed for unknown coworker; skipping",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
        with contextlib.suppress(Exception):
            await msg.ack()

    return await js.subscribe(
        WEB_COWORKER_MCP_CHANGED_SUBJECT,
        durable=_MCP_DURABLE,
        cb=_on_message,
        manual_ack=True,
    )


async def subscribe_coworker_skills_changed(
    js: "JetStreamContext",
    *,
    state: OrchestratorState,
    fetch_skills: Callable[[str, str], Awaitable[list["Skill"]]],
) -> "Subscription":
    """Subscribe to ``web.coworker.skills_changed``.

    Mirrors :func:`subscribe_coworker_mcp_changed` but refreshes the
    skills projection instead of the MCP one. The catalog skill or
    binding may have been mutated (create / edit / enable / disable /
    delete); the subscriber re-reads the JOIN view that the
    projection consumer (container spawn) cares about. Same
    best-effort posture: a missed broadcast degrades to "small
    staleness window" until the next request reads through the
    in-memory cache or a process restart re-seeds it.
    """

    async def _on_message(msg: "NatsMsg") -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "web.coworker.skills_changed payload not JSON; dropping",
                data=msg.data[:128],
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        coworker_id = payload.get("coworker_id")
        tenant_id = payload.get("tenant_id")
        if not (isinstance(coworker_id, str) and isinstance(tenant_id, str)):
            logger.warning(
                "web.coworker.skills_changed missing ids; dropping",
                payload=payload,
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            ok = await reload_coworker_skills_into_state(
                coworker_id=coworker_id,
                tenant_id=tenant_id,
                state=state,
                fetch_skills=fetch_skills,
            )
        except Exception:
            logger.exception(
                "web.coworker.skills_changed handler failed; "
                "leaving for redelivery",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
            return

        if ok:
            logger.info(
                "Coworker skills projection hot-reloaded",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
        else:
            logger.warning(
                "web.coworker.skills_changed for unknown coworker; skipping",
                coworker_id=coworker_id,
                tenant_id=tenant_id,
            )
        with contextlib.suppress(Exception):
            await msg.ack()

    return await js.subscribe(
        WEB_COWORKER_SKILLS_CHANGED_SUBJECT,
        durable=_SKILLS_DURABLE,
        cb=_on_message,
        manual_ack=True,
    )
