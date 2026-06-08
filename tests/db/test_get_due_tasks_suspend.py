"""``get_due_tasks`` (the cross-tenant scheduler sweep) skips suspended tenants.

A suspended tenant's schedules pause — neither run nor failed/deleted — and
resume untouched once the tenant is reactivated. Pins both directions:
suspended tasks drop out of the sweep, and resume brings them back.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.types import ScheduledTask
from rolemesh.db import (
    create_coworker,
    create_task,
    create_tenant,
    get_due_tasks,
    get_task_by_id,
    set_tenant_status,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _tenant_with_due_task() -> tuple[str, str]:
    t = await create_tenant(name="Corp", slug=f"corp-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id, name="Bot", folder=f"bot-{uuid.uuid4().hex[:8]}"
    )
    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=t.id,
            coworker_id=cw.id,
            prompt="Past task",
            schedule_type="once",
            schedule_value="2020-01-01T00:00:00+00:00",
            context_mode="isolated",
            next_run="2020-01-01T00:00:00+00:00",
            status="active",
        )
    )
    return t.id, task_id


async def test_suspended_tenant_task_dropped_then_restored_on_resume():
    tid, task_id = await _tenant_with_due_task()

    # Active: the due task appears in the sweep.
    assert any(t.id == task_id for t in await get_due_tasks())

    # Suspended: it drops out — but the row is untouched (still active),
    # not cancelled/failed.
    await set_tenant_status(tid, "suspended")
    assert all(t.id != task_id for t in await get_due_tasks())
    row = await get_task_by_id(task_id, tenant_id=tid)
    assert row is not None
    assert row.status == "active"

    # Resume: it comes back, same row.
    await set_tenant_status(tid, "active")
    assert any(t.id == task_id for t in await get_due_tasks())


async def test_suspended_one_tenant_does_not_affect_another():
    tid_a, task_a = await _tenant_with_due_task()
    _tid_b, task_b = await _tenant_with_due_task()

    await set_tenant_status(tid_a, "suspended")
    due_ids = {t.id for t in await get_due_tasks()}
    assert task_a not in due_ids
    assert task_b in due_ids
