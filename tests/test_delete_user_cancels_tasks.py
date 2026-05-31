"""v6.1 §P1.8 — F-leaves-tenant soft-cancel (T1.13).

``delete_user`` MUST cancel the user's scheduled tasks in the same
transaction. Without that, ``ON DELETE SET NULL`` on
``scheduled_tasks.created_by_user_id`` would leave an active task
with a NULL user — the next scheduler tick would run a "user X
turn" that no longer belongs to any user, landing on the Phase-2
owner-FYI E path.

The tests pin three behaviours:

1. After ``delete_user``, the user's active tasks read as
   ``status='cancelled'`` (and ``created_by_user_id`` flips to NULL
   via the FK's SET NULL — order doesn't matter once the status
   filter has already taken the row out of the active queue).
2. Tasks owned by OTHER users are not touched — the cancel is
   scoped to (user_id, tenant_id).
3. Already-terminal tasks (paused / completed / cancelled) are not
   flipped — only ``status='active'`` is the cancel target.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rolemesh.core.types import ScheduledTask
from rolemesh.db import (
    _get_admin_pool,
    cancel_tasks_for_user,
    create_coworker,
    create_task,
    create_tenant,
    create_user,
    delete_user,
    get_all_tasks,
    get_task_by_id,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed(slug_tag: str) -> tuple[str, str, str]:
    """Returns (tenant_id, user_id, coworker_id)."""
    t = await create_tenant(
        name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}"
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    return t.id, u.id, cw.id


def _future_iso() -> str:
    return (datetime.now(UTC) + timedelta(minutes=10)).isoformat()


async def _make_task(
    *,
    tenant_id: str, coworker_id: str, created_by: str | None,
    status: str = "active",
) -> str:
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        prompt="ping",
        schedule_type="interval",
        schedule_value="60000",
        context_mode="isolated",
        status=status,  # type: ignore[arg-type]
        next_run=_future_iso(),
        created_by_user_id=created_by,
    )
    await create_task(task)
    return task.id


# ---------------------------------------------------------------------------
# T1.13 — delete_user same-transaction cancel-before-delete
# ---------------------------------------------------------------------------


async def test_delete_user_cancels_active_tasks_and_user_row_gone() -> None:
    """Happy path: user X owns one active task; delete_user removes
    the user AND flips the task to ``cancelled`` in one transaction.
    The scheduler's ``status='active'`` filter then skips the row.
    """
    tid, uid, cw_id = await _seed("del-cancel")
    task_id = await _make_task(
        tenant_id=tid, coworker_id=cw_id, created_by=uid
    )

    deleted = await delete_user(uid, tenant_id=tid)
    assert deleted is True

    # User row gone.
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT 1 FROM users WHERE id = $1::uuid", uid
        )
        assert users == []
        # Task row stays; status is cancelled; created_by_user_id is
        # NULL (SET NULL fired after the cancel landed — both states
        # are consistent with "user gone, task inert").
        row = await conn.fetchrow(
            "SELECT status, created_by_user_id "
            "FROM scheduled_tasks WHERE id = $1::uuid",
            task_id,
        )
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["created_by_user_id"] is None


async def test_delete_user_does_not_affect_other_users_tasks() -> None:
    """User A and User B share a tenant. Deleting A leaves B's tasks
    in ``active``. A regression that dropped the ``user_id`` predicate
    in ``cancel_tasks_for_user`` would surface here.
    """
    tid, uid_a, cw_id = await _seed("del-iso-a")
    u_b = await create_user(
        tenant_id=tid, name="B",
        email=f"b-{uuid.uuid4().hex[:6]}@x.com",
    )
    a_task = await _make_task(
        tenant_id=tid, coworker_id=cw_id, created_by=uid_a
    )
    b_task = await _make_task(
        tenant_id=tid, coworker_id=cw_id, created_by=u_b.id
    )

    await delete_user(uid_a, tenant_id=tid)

    a_row = await get_task_by_id(a_task, tenant_id=tid)
    b_row = await get_task_by_id(b_task, tenant_id=tid)
    assert a_row is not None and a_row.status == "cancelled"
    assert b_row is not None and b_row.status == "active"
    assert b_row.created_by_user_id == u_b.id


async def test_cancel_tasks_for_user_only_touches_active_rows() -> None:
    """The status filter is part of the SQL. Paused / completed /
    cancelled rows are NOT re-flipped to 'cancelled' on delete —
    otherwise we'd mark a paused task as cancelled, hiding the
    user's prior pause intent from audit.
    """
    tid, uid, cw_id = await _seed("status-filter")
    active = await _make_task(
        tenant_id=tid, coworker_id=cw_id, created_by=uid, status="active",
    )
    paused = await _make_task(
        tenant_id=tid, coworker_id=cw_id, created_by=uid, status="paused",
    )
    completed = await _make_task(
        tenant_id=tid, coworker_id=cw_id, created_by=uid, status="completed",
    )

    n = await cancel_tasks_for_user(uid, tid)
    assert n == 1  # only the active one

    rows = {
        t.id: t.status for t in await get_all_tasks(tenant_id=tid)
    }
    assert rows[active] == "cancelled"
    assert rows[paused] == "paused"
    assert rows[completed] == "completed"


async def test_delete_user_with_no_tasks_still_returns_true() -> None:
    """``cancel_tasks_for_user`` finds zero rows; the UPDATE returns
    "UPDATE 0" and the DELETE still proceeds. Without this, a user
    with no scheduled tasks could not be deleted (silly regression
    but easy to introduce with naive error handling).
    """
    tid, uid, _cw_id = await _seed("no-tasks")
    deleted = await delete_user(uid, tenant_id=tid)
    assert deleted is True


async def test_delete_user_cancels_atomically_via_transaction() -> None:
    """Cancel + DELETE share one ``conn.transaction()``. We pin that
    structurally — without the transaction, a scheduler tick between
    the two statements could fire the task against the just-cancelled
    user.

    Direct attempt: monkeypatch ``cancel_tasks_for_user`` to raise
    after the SELECT side, then assert the user row is NOT deleted
    (the abort propagates).
    """
    from rolemesh.db import task as task_mod

    tid, uid, _cw_id = await _seed("atomic")

    raised = False

    async def _explode(*_args: object, **_kwargs: object) -> int:
        nonlocal raised
        raised = True
        raise RuntimeError("simulated cancel failure")

    original = task_mod.cancel_tasks_for_user
    task_mod.cancel_tasks_for_user = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            await delete_user(uid, tenant_id=tid)
    finally:
        task_mod.cancel_tasks_for_user = original  # type: ignore[assignment]

    assert raised, "the cancel half MUST be called before the DELETE"

    # User row survives — DELETE never ran because cancel raised
    # inside the transaction context.
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT 1 FROM users WHERE id = $1::uuid", uid
        )
    assert users != [], "DELETE should NOT have run after cancel raised"
