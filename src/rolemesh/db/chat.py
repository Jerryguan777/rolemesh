"""Channel bindings, conversations, sessions, and messages.

These four entities form a single chat-data flow: a ChannelBinding hosts
Conversations; each Conversation has at most one Session and a stream
of Messages. Cross-entity invariants (``store_message`` updates the
parent Conversation's ``last_invocation``; Session is one-to-one with
Conversation) are easier to reason about co-located here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rolemesh.core.types import ChannelBinding, Conversation, NewMessage
from rolemesh.db._pool import _to_dt, admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "create_channel_binding",
    "create_conversation",
    "delete_channel_binding",
    "delete_conversation",
    "get_all_channel_bindings",
    "get_all_conversations",
    "get_all_sessions",
    "get_channel_binding",
    "get_channel_binding_for_coworker",
    "get_channel_bindings_for_coworker",
    "get_conversation",
    "get_conversation_by_binding_and_chat",
    "get_conversation_for_notification",
    "get_conversations_for_coworker",
    "get_messages_since",
    "get_new_messages_for_conversations",
    "get_session",
    "set_session",
    "store_message",
    "update_channel_binding",
    "update_conversation_last_invocation",
    "update_conversation_user_id",
]


# ---------------------------------------------------------------------------
# ChannelBinding CRUD
# ---------------------------------------------------------------------------


async def create_channel_binding(
    coworker_id: str,
    tenant_id: str,
    channel_type: str,
    credentials: dict[str, str] | None = None,
    bot_display_name: str | None = None,
) -> ChannelBinding:
    """Create a channel binding."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO channel_bindings (coworker_id, tenant_id, channel_type, credentials, bot_display_name)
            VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, $5)
            RETURNING id, coworker_id, tenant_id, channel_type, credentials, bot_display_name, status, created_at
            """,
            coworker_id,
            tenant_id,
            channel_type,
            json.dumps(credentials or {}),
            bot_display_name,
        )
    assert row is not None
    return _record_to_channel_binding(row)


def _record_to_channel_binding(row: asyncpg.Record) -> ChannelBinding:
    creds = row["credentials"]
    return ChannelBinding(
        id=str(row["id"]),
        coworker_id=str(row["coworker_id"]),
        tenant_id=str(row["tenant_id"]),
        channel_type=row["channel_type"],
        credentials=creds if isinstance(creds, dict) else json.loads(creds) if creds else {},
        bot_display_name=row["bot_display_name"],
        status=row["status"] or "active",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )


async def get_channel_binding(binding_id: str, *, tenant_id: str) -> ChannelBinding | None:
    """Fetch a channel binding by id, scoped to ``tenant_id``.

    See ``get_user`` for the tenant-filter rationale.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM channel_bindings WHERE id = $1::uuid AND tenant_id = $2::uuid",
            binding_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def get_channel_binding_for_coworker(
    coworker_id: str, channel_type: str, *, tenant_id: str
) -> ChannelBinding | None:
    """Get the channel binding for a coworker and channel type."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM channel_bindings "
            "WHERE coworker_id = $1::uuid AND channel_type = $2 "
            "AND tenant_id = $3::uuid",
            coworker_id,
            channel_type,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def get_all_channel_bindings() -> list[ChannelBinding]:
    """Get all channel bindings."""
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT * FROM channel_bindings ORDER BY tenant_id, coworker_id")
    return [_record_to_channel_binding(row) for row in rows]


async def get_channel_bindings_for_coworker(
    coworker_id: str, *, tenant_id: str
) -> list[ChannelBinding]:
    """Get all channel bindings for a coworker."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM channel_bindings "
            "WHERE coworker_id = $1::uuid AND tenant_id = $2::uuid",
            coworker_id,
            tenant_id,
        )
    return [_record_to_channel_binding(row) for row in rows]


async def update_channel_binding(
    binding_id: str,
    *,
    tenant_id: str,
    credentials: dict[str, str] | None = None,
    bot_display_name: str | None = None,
    status: str | None = None,
) -> ChannelBinding | None:
    """Update selected fields on a channel binding, scoped to ``tenant_id``."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if credentials is not None:
        fields.append(f"credentials = ${param_idx}::jsonb")
        values.append(json.dumps(credentials))
        param_idx += 1
    if bot_display_name is not None:
        fields.append(f"bot_display_name = ${param_idx}")
        values.append(bot_display_name)
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1

    if not fields:
        return await get_channel_binding(binding_id, tenant_id=tenant_id)

    values.append(binding_id)
    values.append(tenant_id)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"UPDATE channel_bindings SET {', '.join(fields)} "
            f"WHERE id = ${param_idx}::uuid AND tenant_id = ${param_idx + 1}::uuid "
            f"RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def delete_channel_binding(binding_id: str, *, tenant_id: str) -> bool:
    """Delete a channel binding by ID, scoped to ``tenant_id``."""
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM channel_bindings WHERE id = $1::uuid AND tenant_id = $2::uuid",
            binding_id,
            tenant_id,
        )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------


async def create_conversation(
    tenant_id: str,
    coworker_id: str,
    channel_binding_id: str,
    channel_chat_id: str,
    name: str | None = None,
    requires_trigger: bool = True,
    user_id: str | None = None,
) -> Conversation:
    """Create a conversation."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (tenant_id, coworker_id, channel_binding_id, channel_chat_id, name, requires_trigger, user_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7::uuid)
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            channel_binding_id,
            channel_chat_id,
            name,
            requires_trigger,
            user_id,
        )
    assert row is not None
    return _record_to_conversation(row)


def _record_to_conversation(row: asyncpg.Record) -> Conversation:
    lai = row["last_agent_invocation"]
    uid = row.get("user_id")
    return Conversation(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        channel_binding_id=str(row["channel_binding_id"]),
        channel_chat_id=row["channel_chat_id"],
        name=row["name"],
        requires_trigger=bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True,
        last_agent_invocation=lai.isoformat() if lai else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        user_id=str(uid) if uid else None,
    )


async def get_conversation(conversation_id: str, *, tenant_id: str) -> Conversation | None:
    """Fetch a conversation by id, scoped to ``tenant_id``.

    See ``get_user`` for the tenant-filter rationale.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations WHERE id = $1::uuid AND tenant_id = $2::uuid",
            conversation_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_conversation(row)


async def get_conversation_for_notification(conversation_id: str) -> Conversation | None:
    """Look up a conversation by id alone, intentionally cross-tenant.

    System path. Called from the approval notification fan-out
    (``_OrchestratorChannelSender`` and ``NotificationTargetResolver``)
    where the only inputs are a ``conversation_id`` resolved by the
    engine from an ``ApprovalRequest`` it already trusts. The
    ``ChannelSender`` protocol carries no tenant context, so this
    function exists as the explicit, named admin escape rather than
    silently bypassing tenant scoping.

    DO NOT use this from REST handlers — use the tenant-scoped
    ``get_conversation`` for any path where the conversation_id can
    come from user input.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations WHERE id = $1::uuid",
            conversation_id,
        )
    if row is None:
        return None
    return _record_to_conversation(row)


async def get_conversations_for_coworker(
    coworker_id: str, *, tenant_id: str
) -> list[Conversation]:
    """Get all conversations for a coworker."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM conversations "
            "WHERE coworker_id = $1::uuid AND tenant_id = $2::uuid "
            "ORDER BY created_at",
            coworker_id,
            tenant_id,
        )
    return [_record_to_conversation(row) for row in rows]


async def get_all_conversations() -> list[Conversation]:
    """Get all conversations."""
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT * FROM conversations ORDER BY tenant_id, coworker_id")
    return [_record_to_conversation(row) for row in rows]


async def get_conversation_by_binding_and_chat(
    channel_binding_id: str, channel_chat_id: str, *, tenant_id: str
) -> Conversation | None:
    """Get a conversation by binding and chat ID."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations "
            "WHERE channel_binding_id = $1::uuid AND channel_chat_id = $2 "
            "AND tenant_id = $3::uuid",
            channel_binding_id,
            channel_chat_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_conversation(row)


async def delete_conversation(conversation_id: str, *, tenant_id: str) -> bool:
    """Delete a conversation by ID, scoped to ``tenant_id``."""
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM conversations WHERE id = $1::uuid AND tenant_id = $2::uuid",
            conversation_id,
            tenant_id,
        )
    return result == "DELETE 1"


async def update_conversation_last_invocation(
    conversation_id: str, timestamp: str, *, tenant_id: str
) -> None:
    """Update the last_agent_invocation timestamp for a conversation."""
    ts = datetime.fromisoformat(timestamp) if timestamp else None
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            "UPDATE conversations SET last_agent_invocation = $1 "
            "WHERE id = $2::uuid AND tenant_id = $3::uuid",
            ts,
            conversation_id,
            tenant_id,
        )


async def update_conversation_user_id(
    conversation_id: str, user_id: str, *, tenant_id: str
) -> None:
    """Set the user_id on a conversation (binds a user to a web conversation)."""
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            "UPDATE conversations SET user_id = $1::uuid "
            "WHERE id = $2::uuid AND tenant_id = $3::uuid",
            user_id,
            conversation_id,
            tenant_id,
        )


# ---------------------------------------------------------------------------
# Sessions (new: per-conversation)
# ---------------------------------------------------------------------------


async def get_session(conversation_id: str, *, tenant_id: str) -> str | None:
    """Get the session ID for a conversation, scoped to ``tenant_id``."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT session_id FROM sessions "
            "WHERE conversation_id = $1::uuid AND tenant_id = $2::uuid",
            conversation_id,
            tenant_id,
        )
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


async def set_session(conversation_id: str, tenant_id: str, coworker_id: str, session_id: str) -> None:
    """Set the session ID for a conversation."""
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO sessions (conversation_id, tenant_id, coworker_id, session_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4)
            ON CONFLICT (conversation_id) DO UPDATE SET session_id = EXCLUDED.session_id
            """,
            conversation_id,
            tenant_id,
            coworker_id,
            session_id,
        )


async def get_all_sessions() -> dict[str, str]:
    """Get all session mappings (conversation_id -> session_id)."""
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT conversation_id, session_id FROM sessions")
    return {str(row["conversation_id"]): row["session_id"] for row in rows}


# ---------------------------------------------------------------------------
# Messages (new: per-conversation with TIMESTAMPTZ)
# ---------------------------------------------------------------------------


async def store_message(
    tenant_id: str,
    conversation_id: str,
    msg_id: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool = False,
    is_bot_message: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost_usd: float | None = None,
    model_id: str | None = None,
) -> None:
    """Store a message.

    Token-usage parameters are optional; legacy callers (user-side
    messages on inbound channels, channels without a usage carrier)
    leave them None and the corresponding columns stay NULL. Only
    assistant replies coming back from a backend with a usage snapshot
    populate them. The ON CONFLICT branch deliberately leaves the token
    columns alone — a re-store of the same message id (e.g. a retry on
    the inbound path) must not blank out usage that an earlier write
    already recorded.
    """
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO messages (
                tenant_id, conversation_id, id, sender, sender_name,
                content, timestamp, is_from_me, is_bot_message,
                input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, cost_usd, model_id
            )
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15)
            ON CONFLICT (tenant_id, id, conversation_id) DO UPDATE SET
                content = EXCLUDED.content,
                timestamp = EXCLUDED.timestamp
            """,
            tenant_id,
            conversation_id,
            msg_id,
            sender,
            sender_name,
            content,
            _to_dt(timestamp),
            is_from_me,
            is_bot_message,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            cost_usd,
            model_id,
        )


def _record_to_new_message(row: asyncpg.Record, chat_jid: str = "") -> NewMessage:
    """Convert an asyncpg.Record to a NewMessage dataclass."""
    ts = row["timestamp"]
    return NewMessage(
        id=row["id"],
        chat_jid=chat_jid,
        sender=row["sender"] or "",
        sender_name=row["sender_name"] or "",
        content=row["content"] or "",
        timestamp=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        is_from_me=bool(row["is_from_me"]),
        is_bot_message=bool(row.get("is_bot_message", False)) if hasattr(row, "get") else False,
    )


async def get_messages_since(
    tenant_id: str,
    conversation_id: str,
    since_timestamp: str,
    bot_name: str,
    limit: int = 200,
    chat_jid: str = "",
) -> list[NewMessage]:
    """Get messages since a timestamp for a specific conversation."""
    # Handle empty timestamp by using epoch
    ts = since_timestamp if since_timestamp else "1970-01-01T00:00:00+00:00"
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM (
                SELECT id, sender, sender_name, content, timestamp, is_from_me, is_bot_message
                FROM messages
                WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND timestamp > $3
                    AND is_bot_message = FALSE AND content NOT LIKE $4
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT $5
            ) sub ORDER BY timestamp
            """,
            tenant_id,
            conversation_id,
            _to_dt(ts),
            f"{bot_name}:%",
            limit,
        )
    return [_record_to_new_message(row, chat_jid) for row in rows]


async def get_new_messages_for_conversations(
    tenant_id: str,
    conversation_ids: list[str],
    since_timestamp: str,
    bot_name: str,
    limit: int = 200,
) -> list[tuple[str, NewMessage]]:
    """Get new messages across multiple conversations.

    Returns list of (conversation_id, message) tuples.
    """
    if not conversation_ids:
        return []
    ts = since_timestamp if since_timestamp else "1970-01-01T00:00:00+00:00"
    async with tenant_conn(tenant_id) as conn:
        placeholders = ", ".join(f"${i + 3}::uuid" for i in range(len(conversation_ids)))
        sql = f"""
            SELECT * FROM (
                SELECT id, conversation_id, sender, sender_name, content, timestamp, is_from_me, is_bot_message
                FROM messages
                WHERE tenant_id = $1::uuid AND timestamp > $2
                    AND conversation_id IN ({placeholders})
                    AND is_bot_message = FALSE
                    AND content NOT LIKE ${len(conversation_ids) + 3}
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ${len(conversation_ids) + 4}
            ) sub ORDER BY timestamp
        """
        params: list[Any] = [tenant_id, _to_dt(ts), *conversation_ids, f"{bot_name}:%", limit]
        rows = await conn.fetch(sql, *params)

    result: list[tuple[str, NewMessage]] = []
    for row in rows:
        conv_id = str(row["conversation_id"])
        ts_val = row["timestamp"]
        result.append(
            (
                conv_id,
                NewMessage(
                    id=row["id"],
                    chat_jid="",
                    sender=row["sender"] or "",
                    sender_name=row["sender_name"] or "",
                    content=row["content"] or "",
                    timestamp=ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val),
                    is_from_me=bool(row["is_from_me"]),
                    is_bot_message=bool(row.get("is_bot_message", False)) if hasattr(row, "get") else False,
                ),
            )
        )
    return result


