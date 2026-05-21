"""Coworker <-> MCP server projection helpers.

The relation lives in two tables (``mcp_servers`` for the tenant-scoped
config, ``coworker_mcp_servers`` for the per-coworker binding +
``enabled_tools`` tri-state). Every reader downstream of the orchestrator
just wants "give me this coworker's effective MCP config as a list of
``McpServerConfig`` ready to hand to the container" — so we centralise
that JOIN + projection here.

The write helper ``replace_coworker_mcp_configs`` is a transactional
convenience used by the admin endpoint and test fixtures that historically
created a coworker with an inline ``tools`` JSONB. It upserts
``mcp_servers`` rows by ``(tenant_id, name)`` and rewrites the junction
in one ``conn.transaction()`` so a half-write cannot leave the
orchestrator with a stale partial view.

The low-level binding API (``bind_coworker_mcp_server``) is **not** an
auto-upsert path: it requires ``mcp_server_id`` to already exist. The
convenience helper here is only acceptable because the admin endpoint is
historically the entry point that *seeds* mcp_servers — the v1 relation
layer keeps the strict shape.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from rolemesh.core.types import McpServerConfig
from rolemesh.db._pool import tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "list_coworker_mcp_configs",
    "replace_coworker_mcp_configs",
]


def _row_to_mcp_config(row: "asyncpg.Record") -> McpServerConfig:
    """Project a JOINed ``mcp_servers`` row into a ``McpServerConfig``.

    The auth_mode column is constrained by the v1 routes to the
    ``user`` | ``service`` | ``both`` triple; if an older row carries
    something outside that set we fall back to ``user`` rather than
    raising — the dataclass field is typed ``str`` but readers downstream
    only branch on the three known values.
    """
    headers_raw = row["extra_headers"]
    if isinstance(headers_raw, str):
        try:
            headers_parsed = json.loads(headers_raw) if headers_raw else {}
        except json.JSONDecodeError:
            headers_parsed = {}
    elif isinstance(headers_raw, dict):
        headers_parsed = headers_raw
    else:
        headers_parsed = {}
    headers: dict[str, str] = {
        str(k): str(v) for k, v in headers_parsed.items()
    }

    rev_raw = row["tool_reversibility"]
    if isinstance(rev_raw, str):
        try:
            rev_parsed = json.loads(rev_raw) if rev_raw else {}
        except json.JSONDecodeError:
            rev_parsed = {}
    elif isinstance(rev_raw, dict):
        rev_parsed = rev_raw
    else:
        rev_parsed = {}
    tool_reversibility: dict[str, bool] = {
        str(k): bool(v) for k, v in rev_parsed.items()
    }

    auth_mode = row["auth_mode"] or "user"
    if auth_mode not in ("user", "service", "both"):
        auth_mode = "user"

    return McpServerConfig(
        name=row["name"],
        type=row["type"],
        url=row["url"],
        headers=headers,
        auth_mode=auth_mode,
        tool_reversibility=tool_reversibility,
    )


async def list_coworker_mcp_configs(
    coworker_id: str, *, tenant_id: str,
) -> list[McpServerConfig]:
    """Return the effective MCP config list for ``coworker_id``.

    JOINs ``coworker_mcp_servers`` with ``mcp_servers``, ordered by
    server name so consumers (executor MCP spec list, orchestrator
    register loop, snapshot publishers) see a stable order.

    The ``enabled_tools`` tri-state on the junction is **not** applied
    here — the projection returns every bound server. Filtering the
    tool list per-binding happens further downstream (the container's
    MCP client sees the full server, and ``enabled_tools`` is a hook
    layer concern handled by the SDK's tool-allow list). Future:
    populate a per-tool allowlist on ``McpServerSpec`` if we need to
    enforce ``enabled_tools`` at the orchestrator boundary too.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT m.name, m.type, m.url, m.auth_mode,
                   m.extra_headers, m.tool_reversibility,
                   cms.enabled_tools
            FROM coworker_mcp_servers cms
            JOIN mcp_servers m ON m.id = cms.mcp_server_id
            JOIN coworkers c ON c.id = cms.coworker_id
            WHERE cms.coworker_id = $1::uuid
              AND c.tenant_id = $2::uuid
              AND m.tenant_id = $2::uuid
            ORDER BY m.name
            """,
            coworker_id, tenant_id,
        )
    return [_row_to_mcp_config(r) for r in rows]


async def replace_coworker_mcp_configs(
    coworker_id: str,
    *,
    tenant_id: str,
    mcp_configs: Sequence[McpServerConfig],
) -> None:
    """Atomically rewrite the (coworker, mcp_servers) bindings.

    Steps inside a single transaction:

      1. DELETE every existing ``coworker_mcp_servers`` row for this
         coworker. Half-states are invisible to other readers because
         the surrounding transaction is the only one with visibility.
      2. For each entry: ``INSERT ... ON CONFLICT (tenant_id, name)
         DO UPDATE`` on ``mcp_servers``. The upsert is the legacy
         admin convenience — callers passing through the v1 relation
         API never reach here; they POST to ``/api/v1/mcp-servers``
         explicitly and then bind by id.
      3. ``INSERT`` the junction row with ``enabled_tools=NULL`` (the
         tri-state "all tools enabled" default per 02a §enabled_tools).

    No-op when ``mcp_configs`` is empty (step 1 still runs so callers
    can clear a coworker's bindings by passing ``[]``).
    """
    async with tenant_conn(tenant_id) as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM coworker_mcp_servers "
                "WHERE coworker_id = $1::uuid",
                coworker_id,
            )
            for cfg in mcp_configs:
                mcp_id_row = await conn.fetchrow(
                    """
                    INSERT INTO mcp_servers (
                        tenant_id, name, type, url, auth_mode,
                        extra_headers, tool_reversibility
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4, $5,
                        $6::jsonb, $7::jsonb
                    )
                    ON CONFLICT (tenant_id, name) DO UPDATE SET
                        type = EXCLUDED.type,
                        url = EXCLUDED.url,
                        auth_mode = EXCLUDED.auth_mode,
                        extra_headers = EXCLUDED.extra_headers,
                        tool_reversibility = EXCLUDED.tool_reversibility,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    tenant_id,
                    cfg.name,
                    cfg.type,
                    cfg.url,
                    cfg.auth_mode,
                    json.dumps(dict(cfg.headers)),
                    json.dumps(dict(cfg.tool_reversibility)),
                )
                assert mcp_id_row is not None
                await conn.execute(
                    "INSERT INTO coworker_mcp_servers "
                    "(coworker_id, mcp_server_id, enabled_tools) "
                    "VALUES ($1::uuid, $2::uuid, NULL)",
                    coworker_id, mcp_id_row["id"],
                )
