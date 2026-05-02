"""Legacy migration helpers — used only by the migration script.

Kept isolated from the main CRUD modules so a future "delete legacy"
sweep has a single file to remove. Includes pre-multi-tenant
``RegisteredGroup`` / ``sessions_legacy`` helpers and the
``drop_legacy_tables`` cleanup.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rolemesh.core.group_folder import is_valid_group_folder
from rolemesh.core.logger import get_logger
from rolemesh.core.types import RegisteredGroup
from rolemesh.db._pool import DEFAULT_TENANT, admin_conn
from rolemesh.db.coworker import _parse_container_config
from rolemesh.db.schema import _create_schema

if TYPE_CHECKING:
    import asyncpg

logger = get_logger()

__all__ = [
    "drop_legacy_tables",
    "get_all_registered_groups",
    "get_all_sessions_legacy",
    "get_session_legacy",
    "set_registered_group",
    "set_session_legacy",
]


# ---------------------------------------------------------------------------
# Legacy migration helpers (only used by migration script)
# After migration these tables are dropped; functions kept for script compat.
# ---------------------------------------------------------------------------


def _parse_registered_group_record(
    row: asyncpg.Record,
) -> tuple[str, RegisteredGroup] | None:
    """Parse a registered_groups row into (jid, RegisteredGroup)."""
    jid: str = row["jid"]
    folder: str = row["folder"]

    if not is_valid_group_folder(folder):
        logger.warn("Skipping registered group with invalid folder", jid=jid, folder=folder)
        return None

    container_config = _parse_container_config(row["container_config"])
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

    async with admin_conn() as conn:
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
    async with admin_conn() as conn:
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


# ---------------------------------------------------------------------------
# Legacy session helpers (for migration)
# ---------------------------------------------------------------------------


async def get_session_legacy(group_folder: str) -> str | None:
    """Get session from legacy sessions table (old 'sessions' or 'sessions_legacy')."""
    async with admin_conn() as conn:
        # Try the old sessions table first (pre-migration)
        has_old = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sessions' AND column_name='group_folder')"
        )
        table = "sessions" if has_old else "sessions_legacy"
        row = await conn.fetchrow(
            f"SELECT session_id FROM {table} WHERE tenant_id = $1 AND group_folder = $2",
            DEFAULT_TENANT,
            group_folder,
        )
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


async def set_session_legacy(group_folder: str, session_id: str) -> None:
    """Set session in legacy sessions table."""
    async with admin_conn() as conn:
        has_old = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sessions' AND column_name='group_folder')"
        )
        table = "sessions" if has_old else "sessions_legacy"
        await conn.execute(
            f"""
            INSERT INTO {table} (tenant_id, group_folder, session_id) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, group_folder) DO UPDATE SET session_id = EXCLUDED.session_id
            """,
            DEFAULT_TENANT,
            group_folder,
            session_id,
        )


async def get_all_sessions_legacy() -> dict[str, str]:
    """Get all legacy session mappings (group_folder -> session_id)."""
    async with admin_conn() as conn:
        has_old = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sessions' AND column_name='group_folder')"
        )
        table = "sessions" if has_old else "sessions_legacy"
        rows = await conn.fetch(
            f"SELECT group_folder, session_id FROM {table} WHERE tenant_id = $1",
            DEFAULT_TENANT,
        )
    return {row["group_folder"]: row["session_id"] for row in rows}


# ---------------------------------------------------------------------------
# Drop legacy tables
# ---------------------------------------------------------------------------


async def drop_legacy_tables() -> None:
    """Drop legacy tables and recreate shared tables in new format."""
    async with admin_conn() as conn:
        # Drop legacy-only tables
        await conn.execute("DROP TABLE IF EXISTS router_state CASCADE")
        await conn.execute("DROP TABLE IF EXISTS registered_groups CASCADE")
        await conn.execute("DROP TABLE IF EXISTS chats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions_legacy CASCADE")
        # Drop old-format shared tables (messages, sessions, scheduled_tasks, task_run_logs)
        await conn.execute("DROP TABLE IF EXISTS task_run_logs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS scheduled_tasks CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
        # Recreate in new format (now legacy_exists check will be False)
        await _create_schema(conn)
    logger.info("Legacy tables dropped and new-format tables created")


