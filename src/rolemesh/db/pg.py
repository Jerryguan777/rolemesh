"""PostgreSQL database operations using asyncpg."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from rolemesh.core.config import DATABASE_URL
from rolemesh.core.group_folder import is_valid_group_folder
from rolemesh.core.logger import get_logger
from rolemesh.core.types import (
    ContainerConfig,
    NewMessage,
    RegisteredGroup,
    ScheduledTask,
    TaskRunLog,
)

logger = get_logger()

_pool: asyncpg.Pool[asyncpg.Record] | None = None
DEFAULT_TENANT: str = "default"


@dataclass(frozen=True)
class ChatInfo:
    """Chat metadata record."""

    jid: str
    name: str
    last_message_time: str
    channel: str | None
    is_group: bool


def _get_pool() -> asyncpg.Pool[asyncpg.Record]:
    """Return the module-level connection pool, asserting it is initialized."""
    assert _pool is not None, "Database not initialized. Call await init_database() first."
    return _pool


async def _create_schema(conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record]) -> None:
    """Create tables and indexes."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            jid TEXT NOT NULL,
            name TEXT,
            last_message_time TEXT,
            channel TEXT,
            is_group BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (tenant_id, jid)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            id TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            sender TEXT,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT NOT NULL,
            is_from_me BOOLEAN DEFAULT FALSE,
            is_bot_message BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (tenant_id, id, chat_jid)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(tenant_id, timestamp)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            id TEXT PRIMARY KEY,
            group_folder TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT NOT NULL,
            context_mode TEXT DEFAULT 'isolated',
            next_run TEXT,
            last_run TEXT,
            last_result TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next ON scheduled_tasks(tenant_id, next_run)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(tenant_id, status)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS task_run_logs (
            id SERIAL PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
            run_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS router_state (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (tenant_id, key)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            group_folder TEXT NOT NULL,
            session_id TEXT NOT NULL,
            PRIMARY KEY (tenant_id, group_folder)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS registered_groups (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            jid TEXT NOT NULL,
            name TEXT NOT NULL,
            folder TEXT NOT NULL,
            trigger_pattern TEXT NOT NULL,
            added_at TEXT NOT NULL,
            container_config JSONB,
            requires_trigger BOOLEAN DEFAULT TRUE,
            is_main BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (tenant_id, jid),
            UNIQUE (tenant_id, folder)
        )
    """)


async def init_database(database_url: str | None = None) -> None:
    """Initialize PostgreSQL connection pool and create schema."""
    global _pool
    url = database_url or DATABASE_URL
    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await _create_schema(conn)


async def _init_test_database(database_url: str) -> None:
    """Initialize a test database with a fresh schema."""
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        # Drop all tables for a clean slate
        await conn.execute("DROP TABLE IF EXISTS task_run_logs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS scheduled_tasks CASCADE")
        await conn.execute("DROP TABLE IF EXISTS chats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
        await conn.execute("DROP TABLE IF EXISTS registered_groups CASCADE")
        await conn.execute("DROP TABLE IF EXISTS router_state CASCADE")
        await _create_schema(conn)


async def close_database() -> None:
    """Close the connection pool. Call on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Chat metadata
# ---------------------------------------------------------------------------


async def store_chat_metadata(
    chat_jid: str,
    timestamp: str,
    name: str | None = None,
    channel: str | None = None,
    is_group: bool | None = None,
) -> None:
    """Store chat metadata only (no message content)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        if name:
            await conn.execute(
                """
                INSERT INTO chats (tenant_id, jid, name, last_message_time, channel, is_group)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, jid) DO UPDATE SET
                    name = EXCLUDED.name,
                    last_message_time = GREATEST(chats.last_message_time, EXCLUDED.last_message_time),
                    channel = COALESCE(EXCLUDED.channel, chats.channel),
                    is_group = COALESCE(EXCLUDED.is_group, chats.is_group)
                """,
                DEFAULT_TENANT,
                chat_jid,
                name,
                timestamp,
                channel,
                is_group,
            )
        else:
            await conn.execute(
                """
                INSERT INTO chats (tenant_id, jid, name, last_message_time, channel, is_group)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, jid) DO UPDATE SET
                    last_message_time = GREATEST(chats.last_message_time, EXCLUDED.last_message_time),
                    channel = COALESCE(EXCLUDED.channel, chats.channel),
                    is_group = COALESCE(EXCLUDED.is_group, chats.is_group)
                """,
                DEFAULT_TENANT,
                chat_jid,
                chat_jid,
                timestamp,
                channel,
                is_group,
            )


async def update_chat_name(chat_jid: str, name: str) -> None:
    """Update chat name without changing timestamp for existing chats."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (tenant_id, jid, name, last_message_time)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET name = EXCLUDED.name
            """,
            DEFAULT_TENANT,
            chat_jid,
            name,
            datetime.now(UTC).isoformat(),
        )


async def get_all_chats() -> list[ChatInfo]:
    """Get all known chats, ordered by most recent activity."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT jid, name, last_message_time, channel, is_group
            FROM chats
            WHERE tenant_id = $1
            ORDER BY last_message_time DESC
            """,
            DEFAULT_TENANT,
        )
    return [
        ChatInfo(
            jid=row["jid"],
            name=row["name"],
            last_message_time=row["last_message_time"],
            channel=row["channel"],
            is_group=bool(row["is_group"]) if row["is_group"] is not None else False,
        )
        for row in rows
    ]


async def get_last_group_sync() -> str | None:
    """Get timestamp of last group metadata sync."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_message_time FROM chats WHERE tenant_id = $1 AND jid = '__group_sync__'",
            DEFAULT_TENANT,
        )
    if row is None:
        return None
    return row["last_message_time"] or None


async def set_last_group_sync() -> None:
    """Record that group metadata was synced."""
    pool = _get_pool()
    now = datetime.now(UTC).isoformat()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (tenant_id, jid, name, last_message_time)
            VALUES ($1, '__group_sync__', '__group_sync__', $2)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET last_message_time = EXCLUDED.last_message_time
            """,
            DEFAULT_TENANT,
            now,
        )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def store_message(msg: NewMessage) -> None:
    """Store a message with full content."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (tenant_id, id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (tenant_id, id, chat_jid) DO UPDATE SET
                content = EXCLUDED.content,
                timestamp = EXCLUDED.timestamp
            """,
            DEFAULT_TENANT,
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            msg.is_from_me,
            msg.is_bot_message,
        )


async def store_message_direct(
    *,
    id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    is_bot_message: bool = False,
) -> None:
    """Store a message directly."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (tenant_id, id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (tenant_id, id, chat_jid) DO UPDATE SET
                content = EXCLUDED.content,
                timestamp = EXCLUDED.timestamp
            """,
            DEFAULT_TENANT,
            id,
            chat_jid,
            sender,
            sender_name,
            content,
            timestamp,
            is_from_me,
            is_bot_message,
        )


def _record_to_new_message(row: asyncpg.Record) -> NewMessage:
    """Convert an asyncpg.Record to a NewMessage dataclass."""
    return NewMessage(
        id=row["id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row["sender_name"],
        content=row["content"],
        timestamp=row["timestamp"],
        is_from_me=bool(row["is_from_me"]),
        is_bot_message=bool(row.get("is_bot_message", False)) if hasattr(row, "get") else False,
    )


async def get_new_messages(
    jids: list[str],
    last_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
) -> tuple[list[NewMessage], str]:
    """Get new messages since last_timestamp for the given JIDs.

    Returns (messages, new_timestamp).
    """
    if not jids:
        return [], last_timestamp

    pool = _get_pool()
    async with pool.acquire() as conn:
        # Build numbered placeholders for jids: $3, $4, $5, ...
        jid_placeholders = ", ".join(f"${i + 3}" for i in range(len(jids)))
        sql = f"""
            SELECT * FROM (
                SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
                FROM messages
                WHERE tenant_id = $1 AND timestamp > $2
                    AND chat_jid IN ({jid_placeholders})
                    AND is_bot_message = FALSE
                    AND content NOT LIKE ${len(jids) + 3}
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ${len(jids) + 4}
            ) sub ORDER BY timestamp
        """
        params: list[Any] = [DEFAULT_TENANT, last_timestamp, *jids, f"{bot_prefix}:%", limit]
        rows = await conn.fetch(sql, *params)

    messages = [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]

    new_timestamp = last_timestamp
    for msg in messages:
        if msg.timestamp > new_timestamp:
            new_timestamp = msg.timestamp

    return messages, new_timestamp


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
) -> list[NewMessage]:
    """Get messages since a timestamp for a specific chat."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM (
                SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
                FROM messages
                WHERE tenant_id = $1 AND chat_jid = $2 AND timestamp > $3
                    AND is_bot_message = FALSE AND content NOT LIKE $4
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT $5
            ) sub ORDER BY timestamp
            """,
            DEFAULT_TENANT,
            chat_jid,
            since_timestamp,
            f"{bot_prefix}:%",
            limit,
        )
    return [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


async def create_task(task: ScheduledTask) -> None:
    """Create a new scheduled task (without last_run / last_result)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_tasks (tenant_id, id, group_folder, chat_jid, prompt, schedule_type, schedule_value, context_mode, next_run, status, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            DEFAULT_TENANT,
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            task.next_run,
            task.status,
            task.created_at,
        )


def _record_to_scheduled_task(row: asyncpg.Record) -> ScheduledTask:
    """Convert an asyncpg.Record to a ScheduledTask dataclass."""
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def get_task_by_id(id: str) -> ScheduledTask | None:
    """Get a task by its ID, or None if not found."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM scheduled_tasks WHERE tenant_id = $1 AND id = $2",
            DEFAULT_TENANT,
            id,
        )
    if row is None:
        return None
    return _record_to_scheduled_task(row)


async def get_tasks_for_group(group_folder: str) -> list[ScheduledTask]:
    """Get all tasks for a specific group folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM scheduled_tasks WHERE tenant_id = $1 AND group_folder = $2 ORDER BY created_at DESC",
            DEFAULT_TENANT,
            group_folder,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def get_all_tasks() -> list[ScheduledTask]:
    """Get all scheduled tasks."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM scheduled_tasks WHERE tenant_id = $1 ORDER BY created_at DESC",
            DEFAULT_TENANT,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task(
    id: str,
    *,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_value: str | None = None,
    next_run: str | None = None,
    status: str | None = None,
) -> None:
    """Update selected fields on a scheduled task."""
    fields: list[str] = []
    values: list[Any] = [DEFAULT_TENANT]
    param_idx = 2  # $1 is tenant_id

    if prompt is not None:
        fields.append(f"prompt = ${param_idx}")
        values.append(prompt)
        param_idx += 1
    if schedule_type is not None:
        fields.append(f"schedule_type = ${param_idx}")
        values.append(schedule_type)
        param_idx += 1
    if schedule_value is not None:
        fields.append(f"schedule_value = ${param_idx}")
        values.append(schedule_value)
        param_idx += 1
    if next_run is not None:
        fields.append(f"next_run = ${param_idx}")
        values.append(next_run)
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1

    if not fields:
        return

    values.append(id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE tenant_id = $1 AND id = ${param_idx}",
            *values,
        )


async def delete_task(id: str) -> None:
    """Delete a task and its run logs (CASCADE handles task_run_logs)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM scheduled_tasks WHERE tenant_id = $1 AND id = $2",
            DEFAULT_TENANT,
            id,
        )


async def get_due_tasks() -> list[ScheduledTask]:
    """Get all active tasks whose next_run is in the past."""
    pool = _get_pool()
    now = datetime.now(UTC).isoformat()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM scheduled_tasks
            WHERE tenant_id = $1 AND status = 'active' AND next_run IS NOT NULL AND next_run <= $2
            ORDER BY next_run
            """,
            DEFAULT_TENANT,
            now,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task_after_run(
    id: str,
    next_run: str | None,
    last_result: str,
) -> None:
    """Update task state after execution."""
    pool = _get_pool()
    now = datetime.now(UTC).isoformat()
    new_status = "completed" if next_run is None else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE scheduled_tasks
            SET next_run = $1::text, last_run = $2, last_result = $3,
                status = COALESCE($4::text, status)
            WHERE tenant_id = $5 AND id = $6
            """,
            next_run,
            now,
            last_result,
            new_status,
            DEFAULT_TENANT,
            id,
        )


async def log_task_run(log: TaskRunLog) -> None:
    """Insert a task run log entry."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_run_logs (tenant_id, task_id, run_at, duration_ms, status, result, error)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            DEFAULT_TENANT,
            log.task_id,
            log.run_at,
            log.duration_ms,
            log.status,
            log.result,
            log.error,
        )


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


async def get_router_state(key: str) -> str | None:
    """Get a value from the router_state table."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM router_state WHERE tenant_id = $1 AND key = $2",
            DEFAULT_TENANT,
            key,
        )
    if row is None:
        return None
    return row["value"]  # type: ignore[no-any-return]


async def set_router_state(key: str, value: str) -> None:
    """Set a value in the router_state table."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO router_state (tenant_id, key, value) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value
            """,
            DEFAULT_TENANT,
            key,
            value,
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def get_session(group_folder: str) -> str | None:
    """Get the session ID for a group folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_id FROM sessions WHERE tenant_id = $1 AND group_folder = $2",
            DEFAULT_TENANT,
            group_folder,
        )
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


async def set_session(group_folder: str, session_id: str) -> None:
    """Set the session ID for a group folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (tenant_id, group_folder, session_id) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, group_folder) DO UPDATE SET session_id = EXCLUDED.session_id
            """,
            DEFAULT_TENANT,
            group_folder,
            session_id,
        )


async def get_all_sessions() -> dict[str, str]:
    """Get all session mappings."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT group_folder, session_id FROM sessions WHERE tenant_id = $1",
            DEFAULT_TENANT,
        )
    return {row["group_folder"]: row["session_id"] for row in rows}


# ---------------------------------------------------------------------------
# Registered groups
# ---------------------------------------------------------------------------


def _parse_registered_group_record(
    row: asyncpg.Record,
) -> tuple[str, RegisteredGroup] | None:
    """Parse a registered_groups row into (jid, RegisteredGroup).

    Returns None if the folder is invalid.
    """
    jid: str = row["jid"]
    folder: str = row["folder"]

    if not is_valid_group_folder(folder):
        logger.warn("Skipping registered group with invalid folder", jid=jid, folder=folder)
        return None

    container_config_raw: dict[str, Any] | str | None = row["container_config"]
    container_config: ContainerConfig | None = None
    if container_config_raw:
        # JSONB is returned as dict by asyncpg
        parsed = container_config_raw if isinstance(container_config_raw, dict) else json.loads(container_config_raw)
        container_config = ContainerConfig(**parsed) if isinstance(parsed, dict) else None

    requires_trigger = bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True
    is_main = bool(row["is_main"]) if row["is_main"] is not None else False

    return jid, RegisteredGroup(
        name=row["name"],
        folder=folder,
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        container_config=container_config,
        requires_trigger=requires_trigger,
        is_main=is_main,
    )


async def get_registered_group(jid: str) -> RegisteredGroup | None:
    """Get a registered group by JID, or None if not found / invalid folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM registered_groups WHERE tenant_id = $1 AND jid = $2",
            DEFAULT_TENANT,
            jid,
        )
    if row is None:
        return None
    result = _parse_registered_group_record(row)
    if result is None:
        return None
    return result[1]


async def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    """Insert or replace a registered group."""
    if not is_valid_group_folder(group.folder):
        raise ValueError(f'Invalid group folder "{group.folder}" for JID {jid}')

    container_config_json: str | None = None
    if group.container_config:
        container_config_json = json.dumps(
            {
                "additional_mounts": [
                    {"host_path": m.host_path, "container_path": m.container_path, "readonly": m.readonly}
                    for m in group.container_config.additional_mounts
                ],
                "timeout": group.container_config.timeout,
            }
        )

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO registered_groups (tenant_id, jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET
                name = EXCLUDED.name,
                folder = EXCLUDED.folder,
                trigger_pattern = EXCLUDED.trigger_pattern,
                added_at = EXCLUDED.added_at,
                container_config = EXCLUDED.container_config,
                requires_trigger = EXCLUDED.requires_trigger,
                is_main = EXCLUDED.is_main
            """,
            DEFAULT_TENANT,
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            container_config_json,
            group.requires_trigger,
            group.is_main,
        )


async def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    """Get all registered groups as a dict keyed by JID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM registered_groups WHERE tenant_id = $1",
            DEFAULT_TENANT,
        )
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        parsed = _parse_registered_group_record(row)
        if parsed is not None:
            result[parsed[0]] = parsed[1]
    return result
