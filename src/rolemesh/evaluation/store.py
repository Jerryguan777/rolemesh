"""Persistence layer for eval_runs.

Every function takes ``tenant_id`` and goes through ``tenant_conn`` so
RLS binds — the eval framework intentionally never imports ``admin_conn``
(see ``tests/evaluation/test_no_admin_conn.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rolemesh.db import tenant_conn

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class EvalRunRow:
    """Read-side projection of an ``eval_runs`` row."""

    id: str
    tenant_id: str
    coworker_id: str | None
    coworker_config: dict[str, Any]
    coworker_config_sha256: str
    dataset_path: str
    dataset_sha256: str
    eval_log_uri: str | None
    metrics: dict[str, Any] | None
    status: str
    created_by: str | None
    started_at: datetime
    finished_at: datetime | None


def _row_to_eval_run(row: Any) -> EvalRunRow:
    cfg_raw = row["coworker_config"]
    cfg = cfg_raw if isinstance(cfg_raw, dict) else json.loads(cfg_raw)
    metrics_raw = row["metrics"]
    metrics: dict[str, Any] | None
    if metrics_raw is None:
        metrics = None
    elif isinstance(metrics_raw, dict):
        metrics = metrics_raw
    else:
        metrics = json.loads(metrics_raw)
    return EvalRunRow(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]) if row["coworker_id"] else None,
        coworker_config=cfg,
        coworker_config_sha256=row["coworker_config_sha256"],
        dataset_path=row["dataset_path"],
        dataset_sha256=row["dataset_sha256"],
        eval_log_uri=row["eval_log_uri"],
        metrics=metrics,
        status=row["status"],
        created_by=str(row["created_by"]) if row["created_by"] else None,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


async def create_eval_run(
    *,
    tenant_id: str,
    coworker_id: str | None,
    coworker_config: dict[str, Any],
    coworker_config_sha256: str,
    dataset_path: str,
    dataset_sha256: str,
    created_by: str | None = None,
) -> EvalRunRow:
    """Insert a new ``running`` eval run; caller updates status later."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO eval_runs (
                tenant_id, coworker_id, coworker_config,
                coworker_config_sha256, dataset_path, dataset_sha256,
                created_by, status
            )
            VALUES ($1::uuid, $2::uuid, $3::jsonb, $4, $5, $6, $7::uuid, 'running')
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            json.dumps(coworker_config),
            coworker_config_sha256,
            dataset_path,
            dataset_sha256,
            created_by,
        )
    assert row is not None
    return _row_to_eval_run(row)


async def finalize_eval_run(
    run_id: str,
    *,
    tenant_id: str,
    status: str,
    metrics: dict[str, Any] | None,
    eval_log_uri: str | None,
) -> EvalRunRow | None:
    """Mark a run terminal — status in (completed, failed, aborted)."""
    if status not in ("completed", "failed", "aborted"):
        msg = f"invalid terminal status: {status!r}"
        raise ValueError(msg)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE eval_runs
               SET status = $1,
                   metrics = $2::jsonb,
                   eval_log_uri = $3,
                   finished_at = now()
             WHERE id = $4::uuid AND tenant_id = $5::uuid
            RETURNING *
            """,
            status,
            json.dumps(metrics) if metrics is not None else None,
            eval_log_uri,
            run_id,
            tenant_id,
        )
    return _row_to_eval_run(row) if row else None


async def get_eval_run(run_id: str, *, tenant_id: str) -> EvalRunRow | None:
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM eval_runs WHERE id = $1::uuid AND tenant_id = $2::uuid",
            run_id,
            tenant_id,
        )
    return _row_to_eval_run(row) if row else None


async def list_eval_runs(
    *,
    tenant_id: str,
    coworker_id: str | None = None,
    limit: int = 50,
) -> list[EvalRunRow]:
    """Recent runs for a tenant, optionally filtered to one coworker."""
    if limit <= 0:
        return []
    async with tenant_conn(tenant_id) as conn:
        if coworker_id is None:
            rows = await conn.fetch(
                """
                SELECT * FROM eval_runs
                 WHERE tenant_id = $1::uuid
                 ORDER BY started_at DESC
                 LIMIT $2
                """,
                tenant_id,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM eval_runs
                 WHERE tenant_id = $1::uuid AND coworker_id = $2::uuid
                 ORDER BY started_at DESC
                 LIMIT $3
                """,
                tenant_id,
                coworker_id,
                limit,
            )
    return [_row_to_eval_run(r) for r in rows]
