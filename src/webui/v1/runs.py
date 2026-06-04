"""``/api/v1/runs/{id}`` and ``/api/v1/runs/{id}/cancel``.

GET surfaces the lifecycle helper's snapshot — used by the SPA's
reconnect path (design §4 "重连"). POST cancel is fire-and-forget:
it publishes a JetStream event for the orchestrator and returns
202 immediately. The orchestrator stops the agent container and
the lifecycle helper writes ``status='cancelled'`` once the
container has actually halted — the client polls / re-fetches via
GET to observe the terminal state.

Why not synchronous-cancel: writing ``status='cancelled'`` from
the WebUI while the agent container is still running would leave
a ghost (container active, DB says cancelled). The orchestrator is
the only party that knows when the container has actually stopped,
so the write authority stays there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.db import tenant_conn
from rolemesh.runs import get_run
from webui.dependencies import get_current_user, require_action
from webui.schemas_v1 import Run
from webui.v1 import run_events
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

router = APIRouter(prefix="/runs", tags=["Runs"])


_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "awaiting_reauth"}
)


def _run_to_response(snapshot: dict[str, object]) -> Run:
    return Run(
        id=str(snapshot["id"]),
        conversation_id=str(snapshot["conversation_id"]),
        status=snapshot["status"],  # type: ignore[arg-type]
        usage=_normalise_jsonb_field(snapshot.get("usage")),
        error=_normalise_jsonb_field(snapshot.get("error")),
        started_at=_str_or_none(snapshot.get("started_at")),
        completed_at=_str_or_none(snapshot.get("completed_at")),
    )


def _normalise_jsonb_field(value: object) -> dict[str, object] | None:
    """Coerce a JSONB column to ``dict | None`` for the wire model.

    ``get_run`` already json.loads-es strings; this guard catches
    the residual case where the column held a non-object JSON value
    (e.g. a bare list) by collapsing it to ``None`` rather than
    leaking a list through a field typed ``dict``.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


async def _load_run_or_404(
    run_id: str, tenant_id: str
) -> dict[str, object]:
    async with tenant_conn(tenant_id) as conn:
        try:
            snapshot = await get_run(
                run_id=run_id, tenant_id=tenant_id, conn=conn
            )
        except asyncpg.DataError:
            snapshot = None
    if snapshot is None:
        raise_error_response(
            "NOT_FOUND",
            "Run not found.",
            status_code=404,
            details={"run_id": run_id},
        )
    return snapshot


@router.get("/{run_id}", response_model=Run)
async def get_run_endpoint(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Run:
    snapshot = await _load_run_or_404(run_id, user.tenant_id)
    return _run_to_response(snapshot)


@router.post("/{run_id}/cancel", response_model=Run, status_code=202)
async def cancel_run_endpoint(
    run_id: str,
    response: Response,
    user: AuthenticatedUser = Depends(require_action("agent.use")),
) -> Run:
    """Request cancellation of an in-flight run.

    Fire-and-forget: the orchestrator owns the actual UPDATE.
    Returns the current (still ``running``) snapshot so the SPA
    has something to render while it waits — the next GET reflects
    the terminal status once the orchestrator has stopped the
    container.

    A run already in a terminal state returns 409 +
    ``code="ALREADY_TERMINAL"`` and *no* NATS publish happens —
    publishing for a terminal run would create a noisy stream of
    no-op cancels at the orchestrator side.
    """
    snapshot = await _load_run_or_404(run_id, user.tenant_id)
    status = str(snapshot["status"])
    if status in _TERMINAL_STATUSES:
        raise_error_response(
            "ALREADY_TERMINAL",
            f"Run is already in terminal state {status!r}.",
            status_code=409,
            details={"run_id": run_id, "status": status},
        )
    await run_events.publish_run_cancel(
        run_id=run_id,
        tenant_id=user.tenant_id,
        conversation_id=str(snapshot["conversation_id"]),
    )
    response.status_code = 202
    return _run_to_response(snapshot)
