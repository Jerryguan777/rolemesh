"""``runs`` row lifecycle — the only authorised writer surface.

Two operations:

* :func:`create_run` — INSERT with ``status='running'``; returns the
  new ``run_id`` synchronously so the WS handler can echo it back
  to the client and stamp the ``messages.run_id`` column on the
  same turn.
* :func:`update_run_terminal` — UPDATE to a terminal status, gated
  by ``WHERE status='running'``. The gate is the load-bearing
  invariant: once a run reaches ``completed`` / ``failed`` /
  ``cancelled``, **no path may rewrite it**, even if a redelivery
  arrives milliseconds later with a contradictory verdict. The
  gate makes that policy a SQL-level guarantee instead of an
  application-level promise.

A separate :func:`get_run` exists so 01b's reconnect path (design
§4 "reconnect") doesn't have to construct ad-hoc SELECTs.

Why ``conn`` is parameter-injected rather than acquired per-call:
``create_run`` is the first step in a transaction that also writes
the triggering ``messages`` row. Acquiring its own connection would
defeat the atomicity the WS handler depends on (without it, a
crash between INSERT and the message write strands the run). The
caller drives the transaction; we just expose the SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger()


TerminalStatus = Literal[
    "completed", "failed", "cancelled", "awaiting_reauth"
]
RunStatus = Literal["running"] | TerminalStatus

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "awaiting_reauth"}
)


async def create_run(
    *,
    tenant_id: str,
    conversation_id: str,
    conn: asyncpg.Connection,
) -> str:
    """INSERT a ``runs`` row with status='running'; return the ``id``.

    The caller is responsible for the surrounding transaction. The
    typical pattern (WS handler):

        async with tenant_conn(tenant_id) as conn:
            run_id = await create_run(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                conn=conn,
            )
            await store_message(..., run_id=run_id, ...)
            # commit on exit; orchestrator NATS publish happens after
            # the ``async with`` so the row is visible when the agent
            # process reads it back.

    ``tenant_id`` and ``conversation_id`` are both required by the
    schema; passing the conversation alone would force the helper to
    SELECT for the tenant_id, which the caller already has in scope.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO runs (tenant_id, conversation_id, status)
        VALUES ($1::uuid, $2::uuid, 'running')
        RETURNING id::text
        """,
        tenant_id,
        conversation_id,
    )
    assert row is not None  # INSERT ... RETURNING never returns 0 rows
    return row["id"]


async def update_run_terminal(
    *,
    run_id: str,
    status: TerminalStatus,
    usage: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    conn: asyncpg.Connection,
) -> bool:
    """Move a ``runs`` row to a terminal state, idempotently.

    Returns ``True`` if this call performed the update, ``False``
    if the row was already terminal (or absent). The ``WHERE
    status = 'running'`` clause is the only thing standing between
    INV-6 and the resurrection bug — keep it.

    ``status`` is constrained to the terminal set at the type level.
    The function deliberately does not let callers pass
    ``status='running'`` because that would let a misuse re-arm a
    completed run.

    ``usage`` and ``error`` are JSONB; the helper serialises to
    JSON text before passing to asyncpg so we don't depend on
    asyncpg's dict-to-JSONB codec being registered on the pool.
    """
    import json

    if status not in _TERMINAL_STATUSES:
        raise ValueError(
            f"update_run_terminal called with non-terminal status {status!r}; "
            f"allowed: {sorted(_TERMINAL_STATUSES)}"
        )
    usage_json = json.dumps(usage) if usage is not None else None
    error_json = json.dumps(error) if error is not None else None
    result = await conn.execute(
        """
        UPDATE runs
           SET status       = $2,
               completed_at = NOW(),
               usage        = COALESCE($3::jsonb, usage),
               error        = COALESCE($4::jsonb, error)
         WHERE id = $1::uuid
           AND status = 'running'
        """,
        run_id,
        status,
        usage_json,
        error_json,
    )
    # asyncpg returns 'UPDATE <n>'. We parse the count to distinguish
    # a real terminal-state-already vs a row-not-found — both are
    # acceptable but the log line differs.
    affected = int(result.split()[1]) if result.startswith("UPDATE ") else 0
    if affected == 0:
        logger.warning(
            "update_run_terminal noop (run absent or already terminal)",
            run_id=run_id,
            attempted_status=status,
        )
        return False
    return True


async def get_run(
    *,
    run_id: str,
    tenant_id: str,
    conn: asyncpg.Connection,
) -> dict[str, Any] | None:
    """Fetch a run snapshot.

    Tenant scoping is enforced at both layers (RLS via the GUC the
    caller sets on ``conn``, plus the explicit predicate here);
    matches the INV-1 belt-and-braces pattern used elsewhere.
    Returns ``None`` when the row doesn't exist or is in another
    tenant — the caller can't disambiguate, which is the point.
    """
    row = await conn.fetchrow(
        """
        SELECT id::text       AS id,
               conversation_id::text AS conversation_id,
               status,
               started_at,
               completed_at,
               usage,
               error
          FROM runs
         WHERE id = $1::uuid
           AND tenant_id = $2::uuid
        """,
        run_id,
        tenant_id,
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "status": row["status"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "completed_at": (
            row["completed_at"].isoformat() if row["completed_at"] else None
        ),
        "usage": _parse_jsonb(row["usage"]),
        "error": _parse_jsonb(row["error"]),
    }


def _parse_jsonb(value: Any) -> Any:
    """Normalise a JSONB column to a Python value.

    asyncpg ships JSONB as ``str`` unless a codec is registered on
    the pool — the runs lifecycle helper takes a caller-provided
    connection and shouldn't assume what the caller did. Parsing
    here means the snapshot returned to 01b's reconnect path is
    always ``dict | list | None``, never a bare JSON string the
    client has to re-parse.
    """
    import json

    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
