"""INV-6 happy-path UPDATE wiring (smoke-discovered gap).

Live smoke after 01c found that ``terminate_run_via_ws_completed``
was *defined* in ``rolemesh.runs.terminators`` but **never called**
from production code. Result: every Phase-1 chat run stayed at
``status='running'`` even after the orchestrator emitted ``done``.

Fix lives in ``webui.v1.ws_stream``: ``_terminate_run_completed`` /
``_terminate_run_errored`` helpers run the lifecycle UPDATE inside
a tenant-scoped txn, called from ``_forward_stream`` whenever a
``web.stream.*`` chunk of type ``done`` / ``safety_blocked`` is
seen.

These tests pin the helper round-trip against a real test DB
(see ``test_db`` fixture in ``conftest``). They are deliberately
NOT exercised through ``TestClient.websocket_connect`` — that
path runs the handler in a threadpool and crosses asyncio event
loops with the asyncpg pool (same issue ``test_ws_v1_handshake``
sidesteps by stubbing ``get_conversation``). The helper-level
tests cover the lifecycle gap the smoke caught; the end-to-end
wiring of the helpers into ``_forward_stream`` is verified by
live smoke (see ``docs/webui-backend-v1.1-sessions/01c-frontend-chat.md``
Findings).

Anti-mirror: assertions read the ``runs`` table directly and
check ``status`` + ``error`` JSONB values, not which Python
function name was on the call stack.
"""

from __future__ import annotations

import json
import uuid

import pytest

from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    tenant_conn,
)
from rolemesh.runs import create_run
from webui.v1.ws_stream import _terminate_run_completed, _terminate_run_errored

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_run() -> tuple[str, str]:
    """Spin up a tenant + coworker + conversation + run row.

    Returns ``(tenant_id, run_id)`` — caller passes these into the
    helper under test. A coworker is needed because the
    ``channel_bindings.coworker_id`` FK is enforced; the binding
    itself isn't used by the lifecycle helpers but the conversation
    row points at it.

    Note: deliberately does NOT touch the ``WS_TICKET_SECRET`` env
    var — the helpers under test don't use ws_ticket at all, and
    setting it would leak across to ``test_v1_auth_endpoints`` which
    asserts on the production secret.
    """
    t = await create_tenant(
        name=f"T-{uuid.uuid4().hex[:6]}",
        slug=f"inv6-{uuid.uuid4().hex[:6]}",
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name=f"cw-{uuid.uuid4().hex[:6]}",
        folder=f"folder-{uuid.uuid4().hex[:6]}",
        agent_backend="claude",
    )
    binding = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="web",
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=binding.id,
        channel_chat_id="chat-inv6",
        name=None,
    )
    async with tenant_conn(t.id) as conn:
        run_id = await create_run(
            tenant_id=t.id, conversation_id=conv.id, conn=conn
        )
    return t.id, run_id


async def _read_run(tenant_id: str, run_id: str) -> dict:
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, completed_at, usage, error FROM runs "
            "WHERE id = $1::uuid",
            run_id,
        )
    assert row is not None, "seeded run row vanished"
    return dict(row)


@pytest.mark.asyncio
async def test_completed_helper_flips_status_and_records_usage() -> None:
    """``_terminate_run_completed`` writes status='completed' + usage JSONB.

    Smoke gap reproducer: before the fix, this UPDATE never ran and
    every Phase-1 happy-path run stayed at ``status='running'``.
    The terminator's usage column accepts any JSON payload from the
    orchestrator; we assert that a non-dict input is dropped (the
    helper only persists actual dicts) — that's the boundary we
    care about given the WS chunk shape is loose.
    """
    tenant_id, run_id = await _seed_run()
    await _terminate_run_completed(
        run_id=run_id,
        tenant_id=tenant_id,
        usage={"tokens_in": 12, "tokens_out": 8, "total_cost_usd": 0.0001},
    )
    row = await _read_run(tenant_id, run_id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None, (
        "completed_at must be stamped — used by GET /runs/{id} clients"
    )
    usage = row["usage"]
    if isinstance(usage, str):
        usage = json.loads(usage)
    assert usage == {"tokens_in": 12, "tokens_out": 8, "total_cost_usd": 0.0001}


@pytest.mark.asyncio
async def test_completed_helper_with_no_usage_still_writes_terminal() -> None:
    """No usage payload on the chunk -> status='completed' + usage NULL.

    Many ``done`` chunks won't carry usage today (the orchestrator
    only attaches it when the backend reports it); the helper must
    not strand the run row over a missing optional.
    """
    tenant_id, run_id = await _seed_run()
    await _terminate_run_completed(
        run_id=run_id, tenant_id=tenant_id, usage=None
    )
    row = await _read_run(tenant_id, run_id)
    assert row["status"] == "completed"
    assert row["usage"] is None


@pytest.mark.asyncio
async def test_completed_helper_drops_non_dict_usage_silently() -> None:
    """Hostile / accidentally non-JSON usage is coerced to NULL.

    The orchestrator emits chunks with arbitrary content. A bad
    payload should not crash the helper *and* should not poison the
    DB with a non-dict ``usage`` value — the row stays
    ``completed`` with ``usage=NULL``, which is the design's open
    schema fallback.
    """
    tenant_id, run_id = await _seed_run()
    await _terminate_run_completed(
        run_id=run_id, tenant_id=tenant_id, usage="not-a-dict",
    )
    row = await _read_run(tenant_id, run_id)
    assert row["status"] == "completed"
    assert row["usage"] is None


@pytest.mark.asyncio
async def test_completed_helper_idempotent_against_redelivery() -> None:
    """Two calls in a row must not double-flip / regress the row.

    NATS chunk redelivery on consumer reconnect can cause the
    forwarder to see the same ``done`` chunk twice. The lifecycle
    helper's ``WHERE status='running'`` guard is the idempotency
    seat — the second call no-ops and leaves the original
    ``completed_at`` intact.
    """
    tenant_id, run_id = await _seed_run()
    await _terminate_run_completed(
        run_id=run_id, tenant_id=tenant_id, usage={"x": 1}
    )
    first = await _read_run(tenant_id, run_id)
    await _terminate_run_completed(
        run_id=run_id, tenant_id=tenant_id, usage={"x": 999}
    )
    second = await _read_run(tenant_id, run_id)
    assert second["status"] == "completed"
    assert second["completed_at"] == first["completed_at"], (
        "redelivery must not overwrite completed_at"
    )
    # Usage on the original row stays — second UPDATE was a no-op.
    usage_first = first["usage"]
    if isinstance(usage_first, str):
        usage_first = json.loads(usage_first)
    usage_second = second["usage"]
    if isinstance(usage_second, str):
        usage_second = json.loads(usage_second)
    assert usage_first == usage_second


@pytest.mark.asyncio
async def test_errored_helper_records_safety_block_metadata() -> None:
    """``_terminate_run_errored`` lands status='failed' + structured error.

    Safety-block chunks carry the rule_id / stage that fired; the
    helper must persist them so a future ``GET /runs/{id}`` can
    surface "blocked by rule X at stage Y" without re-querying the
    safety_decisions table.
    """
    tenant_id, run_id = await _seed_run()
    await _terminate_run_errored(
        run_id=run_id,
        tenant_id=tenant_id,
        error={
            "code": "SAFETY_BLOCKED",
            "message": "policy violation",
            "stage": "input_prompt",
            "rule_id": "rule-42",
        },
    )
    row = await _read_run(tenant_id, run_id)
    assert row["status"] == "failed"
    err = row["error"]
    if isinstance(err, str):
        err = json.loads(err)
    assert err["code"] == "SAFETY_BLOCKED"
    assert err["stage"] == "input_prompt"
    assert err["rule_id"] == "rule-42"


@pytest.mark.asyncio
async def test_helpers_swallow_db_errors_to_keep_forwarder_alive() -> None:
    """Smoke contract: a missing run_id must NOT bring down ``_forward_stream``.

    The helper wraps its DB work in ``except Exception`` because the
    forwarder loop is the only consumer of NATS ``web.stream.*``
    chunks; a single bad chunk killing it would strand every other
    run on the same WS. We assert that calling with a non-existent
    run_id is a silent no-op (logged but not raised).
    """
    tenant_id, _ = await _seed_run()
    # Should not raise.
    await _terminate_run_completed(
        run_id=str(uuid.uuid4()), tenant_id=tenant_id, usage=None
    )
    await _terminate_run_errored(
        run_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        error={"code": "X"},
    )
