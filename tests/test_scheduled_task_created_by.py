"""v6.1 §P1.7 — schedule_task `userId` end-to-end passthrough.

Four touchpoints have to line up for the run-time turn of a
scheduled task to carry the creator's user_id:

1. ``agent_runner.tools.rolemesh_tools.schedule_task`` forwards
   ``ctx.user_id`` as ``userId`` in the IPC publish payload.
2. ``ipc.task_handler.process_task_ipc`` unpacks ``userId`` and
   passes it to ``create_task``.
3. ``db.task.create_task`` writes the column;
   ``_record_to_scheduled_task`` reads it back.
4. ``orchestration.task_scheduler._run_task`` stamps
   ``AgentInput.user_id`` from ``task.created_by_user_id``.

We exercise (2)+(3) end-to-end against real Postgres via
``process_task_ipc`` and verify (1)+(4) by inspecting code-level
payloads with a synthetic ToolContext / stub executor. T1.10 / T1.11
collapse into a single test ("any inbound channel turn") because the
user_id source is the same — what we're really pinning is that
``ctx.user_id`` reaches the row.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from rolemesh.agent.executor import AgentInput, AgentOutput
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.orchestrator_state import OrchestratorState
from rolemesh.core.types import Coworker, ScheduledTask
from rolemesh.db import (
    create_coworker,
    create_task,
    create_tenant,
    create_user,
    get_task_by_id,
)
from rolemesh.ipc.task_handler import process_task_ipc

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# (1) tool side — ``schedule_task`` publishes ``userId``
# ---------------------------------------------------------------------------


async def test_schedule_task_tool_publishes_user_id_from_ctx() -> None:
    """The schedule_task tool must include ``userId`` in the NATS
    publish so the orchestrator's task handler has something to
    write into ``created_by_user_id``. A regression that drops the
    field would make every scheduled task look anonymous in audit.
    """
    from agent_runner.tools.context import ToolContext
    from agent_runner.tools.rolemesh_tools import schedule_task

    published: list[tuple[str, dict[str, Any]]] = []

    class _JsStub:
        async def publish(self, subject: str, payload: bytes) -> None:
            import json
            published.append((subject, json.loads(payload)))

    ctx = ToolContext(
        js=_JsStub(),  # type: ignore[arg-type]
        nc=None,  # type: ignore[arg-type]  # schedule_task uses js.publish, never nc.request
        job_id="job-1",
        chat_jid="chat",
        group_folder="folder",
        permissions={"task_schedule": True},
        tenant_id=str(uuid.uuid4()),
        coworker_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
        user_id="user-abc-123",
    )
    result = await schedule_task(
        {
            "prompt": "ping",
            "schedule_type": "interval",
            "schedule_value": "60000",
        },
        ctx,
    )
    # Wait for the fire-and-forget publish to settle.
    if ctx._bg_tasks:
        await asyncio.gather(*ctx._bg_tasks)
    assert result.get("is_error") is not True
    assert len(published) == 1
    _, body = published[0]
    assert body["userId"] == "user-abc-123"
    assert body["type"] == "schedule_task"


async def test_schedule_task_tool_publishes_empty_user_id_for_bootstrap() -> None:
    """A system / bootstrap turn legitimately carries no user_id.
    The tool publishes an empty string; the handler will then store
    NULL (covered in the next test). We pin "publishes the empty
    string" here so a future "default to coworker name" hack would
    surface immediately.
    """
    from agent_runner.tools.context import ToolContext
    from agent_runner.tools.rolemesh_tools import schedule_task

    published: list[tuple[str, dict[str, Any]]] = []

    class _JsStub:
        async def publish(self, subject: str, payload: bytes) -> None:
            import json
            published.append((subject, json.loads(payload)))

    ctx = ToolContext(
        js=_JsStub(),  # type: ignore[arg-type]
        nc=None,  # type: ignore[arg-type]  # schedule_task uses js.publish, never nc.request
        job_id="job-1",
        chat_jid="chat",
        group_folder="folder",
        permissions={"task_schedule": True},
        tenant_id=str(uuid.uuid4()),
        coworker_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
        user_id="",  # bootstrap / system actor
    )
    await schedule_task(
        {"prompt": "x", "schedule_type": "interval", "schedule_value": "60000"},
        ctx,
    )
    if ctx._bg_tasks:
        await asyncio.gather(*ctx._bg_tasks)
    _, body = published[0]
    assert body["userId"] == ""


# ---------------------------------------------------------------------------
# (2)+(3) handler + DB — ``userId`` lands on ``created_by_user_id``
# ---------------------------------------------------------------------------


@dataclass
class _DepsStub:
    """Minimal IpcDeps stub. We only exercise on_tasks_changed; the
    proposal / auto-intercept hooks are not relevant to schedule_task.
    """

    on_tasks_changed_calls: int = 0

    async def send_message(self, jid: str, text: str) -> None: ...
    async def on_tasks_changed(self) -> None:
        self.on_tasks_changed_calls += 1
    async def on_proposal(self, *_a: object, **_kw: object) -> None: ...
    async def on_auto_intercept(self, *_a: object, **_kw: object) -> None: ...


async def _seed_scheduling_ground(slug_tag: str) -> tuple[str, str, str]:
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


async def test_process_task_ipc_writes_created_by_user_id() -> None:
    """T1.10/T1.11 collapse — schedule_task from ANY channel turn
    that carries a real user_id ends with the row stamped. Whether
    the turn started as a Telegram inbound (Checkpoint 4 admission)
    or a Web WS turn, the user_id reaches ``ctx.user_id`` which
    reaches the IPC payload which reaches the DB row.
    """
    tid, uid, cw_id = await _seed_scheduling_ground("ipc-stamp")
    deps = _DepsStub()
    permissions = AgentPermissions(
        task_schedule=True,
        task_manage_others=False,
        agent_delegate=False,
    )
    task_id = str(uuid.uuid4())
    await process_task_ipc(
        data={
            "type": "schedule_task",
            "taskId": task_id,
            "prompt": "ping",
            "schedule_type": "interval",
            "schedule_value": "60000",
            "targetCoworkerId": cw_id,
            "userId": uid,
        },
        source_group="cw-folder",
        permissions=permissions,
        deps=deps,
        tenant_id=tid,
        coworker_id=cw_id,
    )
    task = await get_task_by_id(task_id, tenant_id=tid)
    assert task is not None
    assert task.created_by_user_id == uid
    assert deps.on_tasks_changed_calls == 1


async def test_process_task_ipc_with_empty_user_id_stores_null() -> None:
    """A bootstrap / system-driven turn passes ``userId=""`` (the
    default on ToolContext). The handler must store NULL so the
    column distinguishes "system" from "user X" — a misencoded empty
    string would crash on the UUID cast.
    """
    tid, _uid, cw_id = await _seed_scheduling_ground("ipc-null")
    deps = _DepsStub()
    permissions = AgentPermissions(
        task_schedule=True,
        task_manage_others=False, agent_delegate=False,
    )
    task_id = str(uuid.uuid4())
    await process_task_ipc(
        data={
            "type": "schedule_task",
            "taskId": task_id,
            "prompt": "ping",
            "schedule_type": "interval",
            "schedule_value": "60000",
            "targetCoworkerId": cw_id,
            "userId": "",  # system actor
        },
        source_group="cw-folder",
        permissions=permissions,
        deps=deps,
        tenant_id=tid,
        coworker_id=cw_id,
    )
    task = await get_task_by_id(task_id, tenant_id=tid)
    assert task is not None
    assert task.created_by_user_id is None


# ---------------------------------------------------------------------------
# (4) scheduler — ``_run_task`` plumbs ``created_by_user_id`` into AgentInput
# ---------------------------------------------------------------------------


async def test_run_task_stamps_agent_input_user_id_from_task() -> None:
    """T1.12 — ``_run_task`` constructs ``AgentInput`` with
    ``user_id=task.created_by_user_id``. A regression here would
    leave the run with no attributed requester.
    """
    from rolemesh.orchestration.task_scheduler import _run_task

    tid, uid, cw_id = await _seed_scheduling_ground("run-task")
    coworker = Coworker(
        id=cw_id, tenant_id=tid, name="CW", folder="folder",
        agent_backend="claude", system_prompt=None,
        container_config=None, max_concurrent=1, status="active",
        permissions=AgentPermissions(
            task_schedule=True,
            task_manage_others=False, agent_delegate=False,
        ),
    )

    captured: dict[str, AgentInput] = {}

    class _Executor:
        async def execute(
            self,
            inp: AgentInput,
            on_process: Any,
            on_output: Any,
        ) -> AgentOutput:
            captured["input"] = inp
            return AgentOutput(status="success", result="ok", is_final=True)

    class _Queue:
        def notify_idle(self, *_a: object, **_kw: object) -> None: ...
        def request_shutdown(self, *_a: object, **_kw: object) -> None: ...

    deps = SimpleNamespace(
        get_coworker=lambda _id: coworker,
        get_executor=lambda _backend: _Executor(),
        executor=_Executor(),
        transport=None,
        orchestrator_state=OrchestratorState(),
        get_session=lambda _conv_id: None,
        send_message=lambda *_a, **_kw: asyncio.sleep(0),
        on_process=lambda *_a, **_kw: None,
        queue=_Queue(),
    )

    from datetime import UTC, datetime, timedelta
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        tenant_id=tid,
        coworker_id=cw_id,
        prompt="run me",
        schedule_type="interval",
        schedule_value="60000",
        context_mode="isolated",
        status="active",
        next_run=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        created_by_user_id=uid,
    )
    # _run_task writes task_run_logs which FKs scheduled_tasks; persist first.
    await create_task(task)
    await _run_task(task, deps)  # type: ignore[arg-type]
    assert captured["input"].user_id == uid


async def test_run_task_passes_empty_string_when_task_has_null_creator() -> None:
    """Tasks whose creator was hard-deleted (``ON DELETE SET NULL``)
    have ``created_by_user_id=None``. The scheduler MUST coerce to
    an empty string (the AgentInput default), not pass None — the
    Phase-2 E path checks for falsiness rather than ``is None`` and
    we want the same semantics either way.
    """
    from rolemesh.orchestration.task_scheduler import _run_task

    tid, _uid, cw_id = await _seed_scheduling_ground("run-null")
    coworker = Coworker(
        id=cw_id, tenant_id=tid, name="CW", folder="folder",
        agent_backend="claude", system_prompt=None,
        container_config=None, max_concurrent=1, status="active",
        permissions=AgentPermissions(
            task_schedule=True,
            task_manage_others=False, agent_delegate=False,
        ),
    )
    captured: dict[str, AgentInput] = {}

    class _Executor:
        async def execute(self, inp: AgentInput, *_a: object) -> AgentOutput:
            captured["input"] = inp
            return AgentOutput(status="success", result="ok", is_final=True)

    class _Queue:
        def notify_idle(self, *_a: object, **_kw: object) -> None: ...
        def request_shutdown(self, *_a: object, **_kw: object) -> None: ...

    deps = SimpleNamespace(
        get_coworker=lambda _id: coworker,
        get_executor=lambda _backend: _Executor(),
        executor=_Executor(),
        transport=None,
        orchestrator_state=OrchestratorState(),
        get_session=lambda _conv_id: None,
        send_message=lambda *_a, **_kw: asyncio.sleep(0),
        on_process=lambda *_a, **_kw: None,
        queue=_Queue(),
    )
    from datetime import UTC, datetime, timedelta
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        tenant_id=tid,
        coworker_id=cw_id,
        prompt="orphan",
        schedule_type="interval",
        schedule_value="60000",
        context_mode="isolated",
        status="active",
        next_run=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        created_by_user_id=None,
    )
    await create_task(task)
    await _run_task(task, deps)  # type: ignore[arg-type]
    assert captured["input"].user_id == ""
