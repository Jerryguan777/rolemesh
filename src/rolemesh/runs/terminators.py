"""Named entry points for each of INV-6's seven terminal paths.

Design §4 enumerates seven ways a ``runs`` row reaches terminal
state. INV-6 says "every one of them must UPDATE
``runs.{status, completed_at, usage}`` via the lifecycle helper —
no direct SQL UPDATE, no path-specific writer." This module is
the grep target.

Each function is a thin wrapper over
:func:`rolemesh.runs.lifecycle.update_run_terminal`. The wrappers
exist so production code can call the *named* path explicitly
(``terminate_run_via_ws_completed`` is unambiguous, where a bare
``update_run_terminal(status='completed')`` could come from any
of paths 1 / 4 — losing the audit trail). The lifecycle helper's
``WHERE status='running'`` gate is the only thing that makes
double-termination safe across these seven paths.

The pinned test ``tests/test_run_state_machine_all_paths.py``
parametrises over every function in this module and asserts that
the row reaches the expected terminal state. The "mutation"
guarantee — comment out the ``update_run_terminal`` call inside
one wrapper, watch pytest turn red — is what makes this list
load-bearing rather than informational.

Wire-up status (2026-05-20, post 01b):

* Paths 1, 2 (WS completed / error) — orchestrator NATS handler
  + 01b WS forwarding path (see :mod:`webui.v1.ws_stream`). The
  webui forwards stream events; the orchestrator-side terminal
  writer calls one of these wrappers.
* Path 3 (HTTP cancel) — :mod:`webui.v1.runs` publishes
  ``web.run.cancel.{run_id}``; the orchestrator-side subscriber
  calls :func:`terminate_run_via_user_cancel`.
* Path 4 (scheduled) — code path *present* (wrapper callable
  from a Phase-2 scheduler), but no production caller wired yet.
* Path 5 (approval reject) — wrapper present; the approval
  engine in 03a will call it when a reject terminates the parent
  run.
* Path 6 (container crash) — wrapper present; the orchestrator
  container monitor calls it on a die event with non-zero exit.
* Path 7 (user-mode MCP reauth) — wrapper present; the
  credential_proxy interception in 02c calls it on a 401 from
  the vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rolemesh.runs.lifecycle import update_run_terminal

if TYPE_CHECKING:
    import asyncpg


__all__ = [
    "terminate_run_via_ws_completed",
    "terminate_run_via_ws_error",
    "terminate_run_via_user_cancel",
    "terminate_run_via_scheduled_completion",
    "terminate_run_via_approval_reject",
    "terminate_run_via_container_crash",
    "terminate_run_via_reauth_required",
]


async def terminate_run_via_ws_completed(
    *,
    run_id: str,
    usage: dict[str, Any] | None,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 1 — agent finished a turn and emitted a completed event."""
    return await update_run_terminal(
        run_id=run_id, status="completed", usage=usage, conn=conn
    )


async def terminate_run_via_ws_error(
    *,
    run_id: str,
    error: dict[str, Any],
    usage: dict[str, Any] | None = None,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 2 — agent emitted an error event over the WS stream."""
    return await update_run_terminal(
        run_id=run_id,
        status="failed",
        usage=usage,
        error=error,
        conn=conn,
    )


async def terminate_run_via_user_cancel(
    *,
    run_id: str,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 3 — user POSTed /cancel; orchestrator stopped the container."""
    return await update_run_terminal(
        run_id=run_id, status="cancelled", conn=conn
    )


async def terminate_run_via_scheduled_completion(
    *,
    run_id: str,
    success: bool,
    usage: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 4 — scheduled run finished (success or failure).

    Phase 2's scheduler will call this; 01b only wires the code
    path so the pinned test can prove the wrapper exists. The
    ``success`` flag selects ``completed`` vs ``failed``; routing
    through the same wrapper keeps the audit consistent.
    """
    status = "completed" if success else "failed"
    return await update_run_terminal(
        run_id=run_id,
        status=status,
        usage=usage,
        error=error,
        conn=conn,
    )


async def terminate_run_via_approval_reject(
    *,
    run_id: str,
    approval_id: str,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 5 — an approval reject terminates the parent run.

    The ``error`` body carries the originating ``approval_id`` so
    the SPA can link the failed run back to the reject UI event.
    """
    return await update_run_terminal(
        run_id=run_id,
        status="failed",
        error={
            "code": "APPROVAL_REJECTED",
            "approval_id": approval_id,
        },
        conn=conn,
    )


async def terminate_run_via_container_crash(
    *,
    run_id: str,
    exit_code: int,
    signal: str | None = None,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 6 — coworker container crashed / OOM'd / timed out.

    Orchestrator's container monitor receives a die event with a
    non-zero exit code (or a signal name when killed). Both end
    up here so the failure attribution is unambiguous in the
    audit trail.
    """
    error: dict[str, Any] = {
        "code": "CONTAINER_CRASH",
        "exit_code": exit_code,
    }
    if signal is not None:
        error["signal"] = signal
    return await update_run_terminal(
        run_id=run_id,
        status="failed",
        error=error,
        conn=conn,
    )


async def terminate_run_via_reauth_required(
    *,
    run_id: str,
    reason: str,
    conn: "asyncpg.Connection",
) -> bool:
    """INV-6 path 7 — user-mode MCP credential is unrecoverable.

    Triggered by credential_proxy on a 401 from the token vault.
    ``reason`` differentiates ``refresh_token_expired`` vs
    ``user_revoked`` so the SPA banner can render an appropriate
    re-login affordance. Per 01b Open Question 2 (locked) this is
    a *terminal* state — the user must re-auth and start a fresh
    run; no resume.
    """
    return await update_run_terminal(
        run_id=run_id,
        status="awaiting_reauth",
        error={"code": "REAUTH_REQUIRED", "reason": reason},
        conn=conn,
    )
