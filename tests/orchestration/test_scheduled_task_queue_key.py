"""Scheduler must use ``conversation_id`` as the GroupQueue key.

Regression for commit 058eb10 (``feat: add multi-tenant support``):
the initial implementation (38e79d8) keyed every queue interaction
on ``chat_jid``. The multi-tenant refactor introduced the
``Conversation`` model and migrated ``main.py``'s
``enqueue_message_check`` / ``notify_idle`` / ``request_shutdown`` to
use ``conversation_id`` as the queue key, but ``task_scheduler.py``
was updated to *derive* ``channel_chat_id`` from the conversation
and keep using that. The two halves ended up keyed on different
values for the same conversation, splitting ``GroupQueue._groups``
into two ``_GroupState`` entries:

* ``_groups[conversation_id]``  ← active container, ``idle_waiting=True``
* ``_groups[channel_chat_id]``  ← pending scheduled task

``notify_idle``'s preempt check
(``state.pending_tasks`` non-empty → ``request_shutdown``) runs on
the ``conversation_id`` entry, sees no tasks, doesn't fire. The
task waits on the ``channel_chat_id`` entry until the 30-min
``IDLE_TIMEOUT`` finally kills the warm container, frees the
per-coworker concurrency slot, and lets the task spawn its own
container.

For web conversations the symptom is "scheduled task never shows
up"; for Telegram the queue split exists too but Telegram's server
delivers the reminder out-of-band, masking the bug.

These tests pin the contract: the scheduler MUST enqueue under
``conversation_id`` (falling back to ``coworker_id`` only when no
conversation is bound). A regression that re-introduces
``channel_chat_id`` here would silently bring back the split.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from rolemesh.core.types import ScheduledTask
from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_task,
    create_tenant,
)
from rolemesh.orchestration.task_scheduler import (
    _compute_queue_key,
    _enqueue_due_tasks,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Pure helper: queue-key derivation
# ---------------------------------------------------------------------------


def _make_task(
    *, conversation_id: str | None, coworker_id: str = "cw-X"
) -> ScheduledTask:
    """Build a minimal in-memory ScheduledTask. The DB row schema has
    more fields but ``_compute_queue_key`` only reads two."""
    return ScheduledTask(
        id="t-1",
        tenant_id="t-1",
        coworker_id=coworker_id,
        conversation_id=conversation_id,
        prompt="ping",
        schedule_type="once",
        schedule_value="2026-01-01T00:00:00",
        next_run=None,
        last_run=None,
        last_result=None,
        status="active",
        created_at="",
        context_mode="group",
        created_by_user_id=None,
    )


def test_queue_key_uses_conversation_id_when_present() -> None:
    """Pinning the post-058eb10 fix: queue key must equal
    conversation_id (the value that main.py uses for
    enqueue_message_check / notify_idle / request_shutdown).
    Using channel_chat_id here would split GroupQueue state and
    silently break warm-container preemption."""
    task = _make_task(conversation_id="conv-A", coworker_id="cw-X")
    assert _compute_queue_key(task) == "conv-A"


def test_queue_key_falls_back_to_coworker_id_when_no_conversation() -> None:
    """Tasks created without a conversation binding (e.g. legacy
    coworker-scoped reminders) still need *some* queue key so the
    GroupQueue can serialize them. ``coworker_id`` is the only stable
    identifier available — fallback matches the pre-fix behaviour
    for this branch so we don't accidentally widen the scope."""
    task = _make_task(conversation_id=None, coworker_id="cw-X")
    assert _compute_queue_key(task) == "cw-X"

    # Empty string treated the same as None — matches DB shape where
    # the column is nullable but some callers might pass "".
    task_empty = _make_task(conversation_id="", coworker_id="cw-Y")
    assert _compute_queue_key(task_empty) == "cw-Y"


# ---------------------------------------------------------------------------
# Integration: drive _enqueue_due_tasks against a recording queue
# ---------------------------------------------------------------------------


@dataclass
class _RecordingQueue:
    """Stand-in for ``GroupQueue.enqueue_task``. Records the
    ``group_jid`` passed by the scheduler so the test can verify it
    matches the conversation_id, not channel_chat_id.
    """

    enqueued: list[tuple[str, str, str, str]] = field(default_factory=list)

    def enqueue_task(
        self,
        group_jid: str,
        task_id: str,
        fn,  # type: ignore[no-untyped-def]
        *,
        tenant_id: str = "",
        coworker_id: str = "",
    ) -> None:
        self.enqueued.append((group_jid, task_id, tenant_id, coworker_id))


@dataclass
class _DepsStub:
    """Minimal SchedulerDependencies surface for _enqueue_due_tasks.

    Only ``queue`` is exercised — the other Protocol members are needed
    so the closure created by ``_make_fn`` can reference ``deps``, but
    that closure is never invoked in these tests (we assert on the
    enqueue, not on container spawn).
    """

    queue: _RecordingQueue
    orchestrator_state: object = None
    transport: object = None
    executor: object = None

    def get_coworker(self, coworker_id: str) -> object:
        return None

    def get_session(self, conversation_id: str) -> str | None:
        return None

    def on_process(self, *args: object, **kwargs: object) -> None:
        return None

    async def send_message(self, *args: object, **kwargs: object) -> None:
        return None

    def get_executor(self, backend_name: str) -> object:
        return None


async def test_enqueue_due_tasks_uses_conversation_id_even_when_distinct_from_channel_chat_id() -> None:
    """End-to-end against real DB: when conversation_id and
    channel_chat_id are different (typical for web — both are UUIDs
    but distinct), the queue key MUST be conversation_id. Before the
    fix this branch would deterministically pass channel_chat_id and
    split the GroupQueue.
    """
    t = await create_tenant(name="T", slug=f"sk-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    binding = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id, channel_type="web"
    )
    # Deliberately make channel_chat_id != conversation_id so the
    # test fails loudly if the scheduler reverts to the old key.
    channel_chat_id = f"chat-{uuid.uuid4().hex[:8]}"
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=binding.id,
        channel_chat_id=channel_chat_id,
    )
    assert conv.id != channel_chat_id, (
        "test precondition: the two UUIDs must differ to exercise the bug"
    )

    task = ScheduledTask(
        id=str(uuid.uuid4()),
        tenant_id=t.id,
        coworker_id=cw.id,
        conversation_id=conv.id,
        prompt="ping",
        schedule_type="once",
        schedule_value="2026-01-01T00:00:00",
        next_run="2026-01-01T00:00:00",
        last_run=None,
        last_result=None,
        status="active",
        created_at="",
        context_mode="group",
        created_by_user_id=None,
    )
    await create_task(task)

    queue = _RecordingQueue()
    deps = _DepsStub(queue=queue)

    await _enqueue_due_tasks([task], deps)

    assert len(queue.enqueued) == 1
    group_jid, _task_id, _tid, _cwid = queue.enqueued[0]
    assert group_jid == conv.id, (
        f"queue key must be conversation_id ({conv.id!r}); got {group_jid!r}. "
        f"If you see {channel_chat_id!r} here, the 058eb10 multi-tenant "
        "key-split regression has returned."
    )


async def test_enqueue_due_tasks_skips_inactive_tasks() -> None:
    """Regression guard for the in-loop status re-check: between
    ``get_due_tasks`` and ``enqueue_task`` the DB row may have been
    cancelled or completed by another process. ``_enqueue_due_tasks``
    re-fetches and skips non-active rows so a status race doesn't
    fire an already-cancelled reminder. Pinned alongside the queue
    key contract because both live in the same loop body — a
    refactor that touches one tends to touch the other.
    """
    t = await create_tenant(name="T", slug=f"sk-skip-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        tenant_id=t.id,
        coworker_id=cw.id,
        conversation_id=None,
        prompt="ping",
        schedule_type="once",
        schedule_value="2026-01-01T00:00:00",
        next_run="2026-01-01T00:00:00",
        last_run=None,
        last_result=None,
        status="cancelled",  # ← not active
        created_at="",
        context_mode="group",
        created_by_user_id=None,
    )
    await create_task(task)

    queue = _RecordingQueue()
    deps = _DepsStub(queue=queue)

    await _enqueue_due_tasks([task], deps)

    assert queue.enqueued == [], (
        "tasks whose status is no longer 'active' (re-checked inside "
        "the loop after get_due_tasks) must not be enqueued"
    )
