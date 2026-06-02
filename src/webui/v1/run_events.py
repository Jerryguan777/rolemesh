"""Publisher for ``web.run.cancel.*`` events to the orchestrator.

Mirrors :mod:`webui.v1.coworker_events` — the publish is best-effort
and the orchestrator-side subscriber owns the actual terminal UPDATE
(via :func:`rolemesh.runs.lifecycle.update_run_terminal`).

Why JetStream instead of core NATS: a missed cancel would leave a
ghost run (agent container still running, browser thinks it was
cancelled). JetStream gives at-least-once redelivery so transient
broker hiccups don't lose the cancel signal. The orchestrator's
handler is idempotent (the ``WHERE status='running'`` gate in the
lifecycle helper means a redelivery of an already-cancelled run is
a no-op).

Subject naming uses the ``web.`` prefix to fit the existing
``web-ipc`` JetStream stream (subjects pattern ``web.>``); the
original prompt's literal ``run.cancel.{run_id}`` would have
needed a new stream. The orchestrator subscribes with the wildcard
``web.run.cancel.>``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

logger = get_logger()


WEB_RUN_CANCEL_SUBJECT_PREFIX = "web.run.cancel"


_js: JetStreamContext | None = None


def set_jetstream(js: JetStreamContext | None) -> None:
    """Attach or detach the process-wide JetStream context."""
    global _js
    _js = js


def _subject_for(run_id: str) -> str:
    return f"{WEB_RUN_CANCEL_SUBJECT_PREFIX}.{run_id}"


async def publish_run_cancel(
    *,
    run_id: str,
    tenant_id: str,
    conversation_id: str,
) -> None:
    """Publish a ``web.run.cancel.{run_id}`` event to the orchestrator.

    Best-effort: a publish failure logs at WARN; the caller has
    already returned 202 to the client so a retry isn't possible
    from this side. Operators see the dangling ``status='running'``
    row and can intervene.
    """
    if _js is None:
        logger.debug(
            "run.cancel publisher unset; skipping broadcast",
            run_id=run_id,
            tenant_id=tenant_id,
        )
        return
    payload = json.dumps(
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
        }
    )
    try:
        await _js.publish(_subject_for(run_id), payload.encode("utf-8"))
    except Exception:
        logger.warning(
            "Failed to publish web.run.cancel; run may remain running",
            run_id=run_id,
            tenant_id=tenant_id,
            exc_info=True,
        )
