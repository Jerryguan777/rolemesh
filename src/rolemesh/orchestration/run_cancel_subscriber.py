"""Orchestrator-side subscriber for ``web.run.cancel.*`` events.

01b PR1 published these events from the WebUI; this subscriber is
the matching half — without it, ``POST /api/v1/runs/{id}/cancel``
is a "fake success" (event lands on the bus but no one stops the
container or UPDATEs the row).

Flow per event:

1. Decode ``{run_id, tenant_id, conversation_id}``.
2. Look up the *active* container name via the caller-injected
   ``fetch_active_container`` (typically a closure into
   :class:`rolemesh.container.scheduler.GroupQueue`'s
   ``get_active_container_name``). ``None`` means the container
   has already exited.
3. If a name was found, call ``runtime.stop(name, timeout=...)``.
   Failures here are logged but do **not** abort step 4: the
   request to stop has already been seen, retrying it would loop
   on the same docker daemon hiccup and never let the row reach
   terminal state. The risk of leaving the container alive while
   the DB says cancelled is bounded — the next orphan-sweep on
   container cleanup would catch it.
4. Call :func:`rolemesh.runs.terminate_run_via_user_cancel`
   inside a ``tenant_conn`` transaction. The wrapper is
   idempotent (``WHERE status='running'`` gate in the lifecycle
   helper); if the run already reached a terminal state via some
   other path the call returns ``False`` and we proceed.
5. Manual ack.

Why ``manual_ack=True`` + ``max_deliver=3`` + ``ack_wait=30s``:
container stop can be slow (docker daemon under load); a default
``ack_wait`` of a few seconds would trigger NATS redelivery while
we're still trying to stop. ``max_deliver=3`` bounds the blast
radius for genuinely broken events (malformed payload, missing
tenant_id) — we ack-and-drop the malformed ones synchronously
inside the callback, but a transient PG / runtime hiccup gets two
retries before NATS gives up.

Wire-up status (2026-05-20):

* WebUI publishes via :mod:`webui.v1.run_events`
  (subject ``web.run.cancel.{run_id}``).
* Stream ``web-ipc`` (subjects ``web.>``) is idempotently
  registered on the orchestrator boot path (see
  :func:`rolemesh.main` `_start_message_loop_with_jetstream`); we
  re-use it instead of carving a new one.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from nats.js.api import ConsumerConfig

from rolemesh.core.logger import get_logger
from rolemesh.db import tenant_conn
from rolemesh.runs import terminate_run_via_user_cancel

if TYPE_CHECKING:
    from collections.abc import Callable

    from nats.aio.msg import Msg as NatsMsg
    from nats.js.client import JetStreamContext
    from nats.js.subscription import Subscription

    from rolemesh.container.runtime import ContainerRuntime

logger = get_logger()


WEB_RUN_CANCEL_SUBJECT_FILTER = "web.run.cancel.>"
_DURABLE = "orch-web-run-cancel"
_ACK_WAIT_S = 30
_MAX_DELIVER = 3


async def _handle_cancel_event(
    *,
    payload: dict[str, object],
    runtime: ContainerRuntime,
    fetch_active_container: Callable[[str], str | None],
) -> None:
    """Core handler — separated from the NATS callback so tests can
    drive it directly without spinning up JetStream for unit-style
    assertions on order-of-operations.
    """
    run_id = payload.get("run_id")
    tenant_id = payload.get("tenant_id")
    conversation_id = payload.get("conversation_id")
    if not (
        isinstance(run_id, str)
        and isinstance(tenant_id, str)
        and isinstance(conversation_id, str)
    ):
        logger.warning(
            "web.run.cancel missing required fields; dropping",
            payload=payload,
        )
        return

    container_name = fetch_active_container(conversation_id)
    if container_name is not None:
        try:
            await runtime.stop(container_name)
        except Exception:  # noqa: BLE001
            # runtime.stop can fail for benign reasons — container
            # already gone, docker daemon hiccup, etc. We do NOT
            # raise: the state machine still needs to advance to
            # 'cancelled'. The next orphan sweep handles leftover
            # containers.
            logger.warning(
                "runtime.stop failed during run cancel; "
                "proceeding to UPDATE anyway",
                run_id=run_id,
                container_name=container_name,
                exc_info=True,
            )
    else:
        # The agent container may have exited on its own between
        # the user's cancel click and our handler running. The
        # lifecycle helper's WHERE clause handles the
        # already-terminal case; we still issue the call so the
        # state is durably written by *one* of the seven paths
        # (INV-6 is about coverage, not exclusivity).
        logger.debug(
            "run.cancel saw no active container; "
            "proceeding to terminator call",
            run_id=run_id,
            conversation_id=conversation_id,
        )

    async with tenant_conn(tenant_id) as conn:
        was_running = await terminate_run_via_user_cancel(
            run_id=run_id, conn=conn
        )
    if not was_running:
        # Already terminal — common when the user clicks Cancel
        # right as the agent emits ``done``. The lifecycle helper
        # logged a noop already; we just record the path here.
        logger.info(
            "run.cancel saw run already terminal; UPDATE skipped",
            run_id=run_id,
        )


async def subscribe_run_cancel(
    js: JetStreamContext,
    *,
    runtime: ContainerRuntime,
    fetch_active_container: Callable[[str], str | None],
) -> Subscription:
    """Subscribe to ``web.run.cancel.>`` on JetStream.

    ``fetch_active_container`` is injected (rather than reading
    directly from a global ``GroupQueue``) so the subscriber stays
    testable without standing up the entire orchestrator state
    graph. Production wires this to
    ``GroupQueue.get_active_container_name`` in
    :mod:`rolemesh.main`.

    The caller owns the returned ``Subscription`` and is
    responsible for ``await sub.unsubscribe()`` on shutdown —
    same lifetime contract as ``subscribe_coworker_restart``.
    """

    async def _on_message(msg: NatsMsg) -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "web.run.cancel payload not JSON; dropping",
                data=msg.data[:128],
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        if not isinstance(payload, dict):
            logger.warning(
                "web.run.cancel payload not an object; dropping",
                payload=payload,
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            await _handle_cancel_event(
                payload=payload,
                runtime=runtime,
                fetch_active_container=fetch_active_container,
            )
        except Exception:
            # Genuine exception path — leave for redelivery. NATS
            # will retry up to ``max_deliver``; after that the
            # message lands in the dead-letter (or simply expires
            # if no DLQ is configured) and an operator notices
            # via the WARN log line below.
            logger.exception(
                "web.run.cancel handler raised; leaving for redelivery",
                payload=payload,
            )
            with contextlib.suppress(Exception):
                await msg.nak()
            return

        with contextlib.suppress(Exception):
            await msg.ack()

    return await js.subscribe(
        WEB_RUN_CANCEL_SUBJECT_FILTER,
        durable=_DURABLE,
        cb=_on_message,
        manual_ack=True,
        config=ConsumerConfig(
            ack_wait=_ACK_WAIT_S,
            max_deliver=_MAX_DELIVER,
        ),
    )


__all__ = [
    "WEB_RUN_CANCEL_SUBJECT_FILTER",
    "subscribe_run_cancel",
]
