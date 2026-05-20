"""Delegation DB helpers — frontdesk v1.2.

Per handbook §6 Step 2.4 these helpers form the persistence layer
between the orchestrator's delegation handler (Phase B Step 5) and
the schema additions made in Step 1:

* ``get_or_create_internal_binding`` — idempotent ``internal`` channel
  binding per target coworker, relying on
  ``channel_bindings.UNIQUE (coworker_id, channel_type)``.
* ``find_child_conversation`` — exact-match lookup of an existing child
  conversation for a ``(parent, target, channel_chat_id)`` triple.
* ``create_child_conversation`` — INSERT ... ON CONFLICT for a sub-conv
  attached to the internal binding. ``requires_trigger`` defaults to
  False on purpose (see docstring).
* ``insert_delegation`` / ``update_delegation_terminal`` /
  ``cleanup_running_delegations`` — write paths for the ``delegations``
  audit table; terminal updates are conditional so late events cannot
  overwrite a finished row.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Literal

from rolemesh.db._pool import admin_conn, tenant_conn
from rolemesh.db.chat import _record_to_channel_binding, _record_to_conversation

if TYPE_CHECKING:
    from rolemesh.core.types import ChannelBinding, Conversation

__all__ = [
    "ChildConvMode",
    "cleanup_running_delegations",
    "create_child_conversation",
    "find_child_conversation",
    "get_or_create_internal_binding",
    "insert_delegation",
    "update_delegation_terminal",
]


ChildConvMode = Literal["sticky", "isolated"]


_INTERNAL_CHANNEL_TYPE = "internal"


async def get_or_create_internal_binding(
    *, tenant_id: str, coworker_id: str,
) -> ChannelBinding:
    """Return the ``internal`` channel binding for the given target coworker,
    creating it if absent.

    Idempotency relies on the ``channel_bindings.UNIQUE (coworker_id,
    channel_type)`` constraint: the INSERT is ``ON CONFLICT DO NOTHING
    RETURNING``; when the row already exists the INSERT returns no row
    and the fallback SELECT fetches it.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO channel_bindings (coworker_id, tenant_id, channel_type,
                credentials, bot_display_name)
            VALUES ($1::uuid, $2::uuid, $3, '{}'::jsonb, NULL)
            ON CONFLICT (coworker_id, channel_type) DO NOTHING
            RETURNING *
            """,
            coworker_id,
            tenant_id,
            _INTERNAL_CHANNEL_TYPE,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM channel_bindings "
                "WHERE coworker_id = $1::uuid AND channel_type = $2 "
                "AND tenant_id = $3::uuid",
                coworker_id,
                _INTERNAL_CHANNEL_TYPE,
                tenant_id,
            )
    assert row is not None, "internal binding lookup returned NULL after ON CONFLICT"
    return _record_to_channel_binding(row)


async def find_child_conversation(
    *,
    tenant_id: str,
    parent_conversation_id: str,
    target_coworker_id: str,
    channel_chat_id: str,
) -> Conversation | None:
    """Locate an existing child conversation by exact ``channel_chat_id``.

    Matching on ``channel_chat_id`` (not just the
    ``(parent, target)`` pair) is required: a prior isolated child has
    a UUID-suffixed chat_id and must NOT be picked up by a later sticky
    lookup. The unique key ``(channel_binding_id, channel_chat_id)`` on
    ``conversations`` makes the match safe.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations "
            "WHERE tenant_id = $1::uuid "
            "AND parent_conversation_id = $2::uuid "
            "AND coworker_id = $3::uuid "
            "AND channel_chat_id = $4 "
            "LIMIT 1",
            tenant_id,
            parent_conversation_id,
            target_coworker_id,
            channel_chat_id,
        )
    if row is None:
        return None
    return _record_to_conversation(row)


def _channel_chat_id(
    *, parent_conversation_id: str, target_coworker_id: str, mode: ChildConvMode,
) -> str:
    """Compute the conventional channel_chat_id for a child conversation.

    Format mirrors handbook §5.4:
      sticky:   ``internal:{parent_conv_id}:{target_coworker_id}``
      isolated: ``internal:{parent_conv_id}:{target_coworker_id}:{uuid4}``
    """
    base = f"internal:{parent_conversation_id}:{target_coworker_id}"
    if mode == "isolated":
        return f"{base}:{uuid.uuid4()}"
    return base


async def create_child_conversation(
    *,
    tenant_id: str,
    parent_conversation_id: str,
    target_coworker_id: str,
    target_internal_binding_id: str,
    user_id: str | None,
    mode: ChildConvMode,
    requires_trigger: bool = False,
) -> Conversation:
    """Create a delegation child conversation.

    ★ ``requires_trigger`` is an explicit named parameter (default
    False) on purpose. ``conversations.requires_trigger`` defaults to
    TRUE in schema, and ``db.chat.create_conversation`` threads the
    value through. If a child conv were ever created with
    ``requires_trigger=TRUE``, the orchestrator's ``_message_loop``
    would pick it up — collapsing the "child conv never enters
    ``_state``" invariant the §6 Step 2.5 audit depends on. Phase B
    Step 5.5 tests assert ``requires_trigger=False`` on the created
    row to catch regressions; the loader-exclusion test in
    ``tests/core/test_loader_excludes_children.py`` catches the
    same invariant from the other side.

    Sticky mode uses a fixed ``channel_chat_id`` per
    ``(parent, target)`` pair; concurrent sticky calls race on the
    ``UNIQUE (channel_binding_id, channel_chat_id)`` constraint and
    the loser falls through to the SELECT. Isolated mode includes a
    UUID suffix so every call gets a fresh row.
    """
    channel_chat_id = _channel_chat_id(
        parent_conversation_id=parent_conversation_id,
        target_coworker_id=target_coworker_id,
        mode=mode,
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (tenant_id, coworker_id, channel_binding_id,
                channel_chat_id, name, requires_trigger, user_id,
                parent_conversation_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, NULL, $5, $6::uuid, $7::uuid)
            ON CONFLICT (channel_binding_id, channel_chat_id) DO NOTHING
            RETURNING *
            """,
            tenant_id,
            target_coworker_id,
            target_internal_binding_id,
            channel_chat_id,
            requires_trigger,
            user_id,
            parent_conversation_id,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM conversations "
                "WHERE channel_binding_id = $1::uuid "
                "AND channel_chat_id = $2 "
                "AND tenant_id = $3::uuid",
                target_internal_binding_id,
                channel_chat_id,
                tenant_id,
            )
    assert row is not None, "child conv lookup returned NULL after ON CONFLICT"
    return _record_to_conversation(row)


async def insert_delegation(
    *,
    tenant_id: str,
    parent_conversation_id: str,
    child_conversation_id: str,
    from_coworker_id: str,
    target_coworker_id: str,
    user_id: str | None,
    prompt_sha256: str,
    context_mode: str,
) -> str:
    """Insert a new ``delegations`` row in status='running'. Returns the id."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO delegations (
                tenant_id, parent_conversation_id, child_conversation_id,
                from_coworker_id, target_coworker_id, user_id,
                prompt_sha256, context_mode, status
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6::uuid,
                    $7, $8, 'running')
            RETURNING id
            """,
            tenant_id,
            parent_conversation_id,
            child_conversation_id,
            from_coworker_id,
            target_coworker_id,
            user_id,
            prompt_sha256,
            context_mode,
        )
    assert row is not None
    return str(row["id"])


async def update_delegation_terminal(
    delegation_id: str,
    *,
    tenant_id: str,
    status: str,
    duration_ms: int,
    error_message: str | None = None,
) -> bool:
    """Conditionally flip a delegation to a terminal status.

    The ``WHERE status='running'`` guard is load-bearing: it makes the
    update idempotent (a second call with a different terminal status
    is a no-op) and protects against a late event overwriting a
    finished row. The boolean return lets callers detect the no-op.
    """
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            """
            UPDATE delegations SET
                status = $2,
                duration_ms = $3,
                error_message = $4,
                ended_at = now()
            WHERE id = $1::uuid AND status = 'running'
            """,
            delegation_id,
            status,
            duration_ms,
            error_message,
        )
    return result == "UPDATE 1"


async def cleanup_running_delegations() -> int:
    """Mark every still-``running`` row as ``error``. Returns the count.

    Intended to be called once at orchestrator startup BEFORE the
    delegation NATS subscriber comes up — any 'running' row at boot
    is stale from a prior crash and must be sealed off so audit
    history is unambiguous. Uses ``admin_conn`` to sweep across
    tenants in one statement; RLS does not apply because the
    orchestrator owns audit completeness, not any one tenant.
    """
    async with admin_conn() as conn:
        result = await conn.execute(
            """
            UPDATE delegations SET
                status = 'error',
                error_message = COALESCE(error_message, 'cleanup: orchestrator restarted'),
                ended_at = now()
            WHERE status = 'running'
            """,
        )
    parts = result.split()
    if len(parts) >= 2 and parts[0] == "UPDATE":
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0
