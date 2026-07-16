"""Orchestrator-side run termination + explicit terminal chunks (phase B).

The single-writer refactor makes ``rolemesh.main`` the ONLY terminal
writer for INV-6 paths 1/2 and moves the "frame mirrors the
authoritative row" logic (born as the #128 WS stopgap) to the writer
itself: ``_terminate_and_emit_run_terminal`` writes the row first, then
publishes a ``run_completed`` / ``run_error`` stream chunk that the WS
projects verbatim. The frame therefore can never contradict
``GET /api/v1/runs/{id}``.

The contract under test:

* ``_terminate_run_safe`` reports the lifecycle outcome (True moved the
  row, False lost to an earlier terminal writer, None nothing to do).
* ``_run_terminal_error_or_none`` mirrors the row when the write lost —
  failed/awaiting_reauth report the recorded error; a redelivered write
  on a completed/cancelled row does NOT fabricate a phantom error.
* ``_terminate_and_emit_run_terminal`` publishes the chunk that matches
  the row — including the field-trace case: a run failed mid-batch by a
  content-filter kill must end the stream with ``run_error`` even
  though the batch-final marker tried to certify success.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> Path:
    monkeypatch.setattr("rolemesh.core.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("rolemesh.core.config.GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr("rolemesh.core.config.STORE_DIR", tmp_path / "store")

    from rolemesh.db import _init_test_database

    await _init_test_database(pg_url)

    yield tmp_path

    from rolemesh.db import close_database

    await close_database()


async def _seed_run() -> tuple[str, str, str, str]:
    """Tenant + web-bound coworker + conversation + running run.

    Returns ``(tenant_id, binding_id, chat_id, run_id)`` — enough to
    both drive the terminal helpers and assert on the stream subject
    the gateway publishes to.
    """
    from rolemesh.db import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
        tenant_conn,
    )
    from rolemesh.runs import create_run

    t = await create_tenant(
        name=f"T-{uuid.uuid4().hex[:6]}", slug=f"sw-{uuid.uuid4().hex[:6]}"
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name=f"cw-{uuid.uuid4().hex[:6]}",
        folder=f"folder-{uuid.uuid4().hex[:6]}",
    )
    binding = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id, channel_type="web"
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=binding.id,
        channel_chat_id="chat-sw",
    )
    async with tenant_conn(t.id) as conn:
        run_id = await create_run(
            tenant_id=t.id, conversation_id=conv.id, conn=conn
        )
    return t.id, binding.id, "chat-sw", run_id


async def _read_run(tenant_id: str, run_id: str) -> dict[str, Any]:
    from rolemesh.db import tenant_conn

    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, completed_at, error FROM runs WHERE id = $1::uuid",
            run_id,
        )
    assert row is not None
    return dict(row)


class _CapturingJs:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, json.loads(data)))


class _CapturingTransport:
    def __init__(self) -> None:
        self.js = _CapturingJs()


def _make_gateway() -> tuple[Any, _CapturingJs]:
    from rolemesh.channels.web_nats_gateway import WebNatsGateway

    async def _noop(*args: Any) -> None:
        pass

    transport = _CapturingTransport()
    gw = WebNatsGateway(on_message=_noop, transport=transport)  # type: ignore[arg-type]
    return gw, transport.js


class _Binding:
    def __init__(self, binding_id: str) -> None:
        self.id = binding_id


def _stream_chunks(js: _CapturingJs, binding_id: str, chat_id: str) -> list[dict[str, Any]]:
    subject = f"web.stream.{binding_id}.{chat_id}"
    return [payload for subj, payload in js.published if subj == subject]


# ---------------------------------------------------------------------------
# _terminate_run_safe outcome reporting
# ---------------------------------------------------------------------------


async def test_terminate_reports_transition_and_loss(env: Path) -> None:
    import rolemesh.main as m

    tenant_id, _b, _c, run_id = await _seed_run()
    first = await m._terminate_run_safe(run_id, tenant_id, success=True)
    assert first is True
    row = await _read_run(tenant_id, run_id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None

    # Redelivered/duplicate write loses on the WHERE status='running' guard.
    second = await m._terminate_run_safe(run_id, tenant_id, success=True)
    assert second is False


async def test_terminate_none_run_id_is_noop(env: Path) -> None:
    import rolemesh.main as m

    assert await m._terminate_run_safe(None, str(uuid.uuid4()), success=True) is None


async def test_terminate_error_records_structured_error(env: Path) -> None:
    import rolemesh.main as m

    tenant_id, _b, _c, run_id = await _seed_run()
    outcome = await m._terminate_run_safe(
        run_id,
        tenant_id,
        success=False,
        error={
            "code": "SAFETY_BLOCKED",
            "message": "policy violation",
            "stage": "input_prompt",
            "rule_id": "rule-42",
        },
    )
    assert outcome is True
    row = await _read_run(tenant_id, run_id)
    assert row["status"] == "failed"
    err = row["error"]
    if isinstance(err, str):
        err = json.loads(err)
    assert err["code"] == "SAFETY_BLOCKED"
    assert err["rule_id"] == "rule-42"


# ---------------------------------------------------------------------------
# _run_terminal_error_or_none — the relocated #128 frame selection
# ---------------------------------------------------------------------------


async def test_frame_selection_write_won_keeps_intent(env: Path) -> None:
    import rolemesh.main as m

    assert (
        await m._run_terminal_error_or_none(
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            transitioned=True,
            intent_error=None,
        )
        is None
    ), "no row lookup on the happy path — intent is the truth"


async def test_frame_selection_lost_to_failure_mirrors_row(env: Path) -> None:
    """The field-trace case: a completed write that lost to an earlier
    AGENT_ERROR must report the row's recorded error, not success."""
    import rolemesh.main as m

    tenant_id, _b, _c, run_id = await _seed_run()
    await m._terminate_run_safe(
        run_id,
        tenant_id,
        success=False,
        error={
            "code": "AGENT_ERROR",
            "message": "Output blocked by content filtering policy",
        },
    )
    transitioned = await m._terminate_run_safe(run_id, tenant_id, success=True)
    assert transitioned is False

    err = await m._run_terminal_error_or_none(
        run_id, tenant_id, transitioned=transitioned, intent_error=None
    )
    assert err is not None
    assert err["code"] == "AGENT_ERROR"
    assert "content filtering" in str(err["message"])


async def test_frame_selection_redelivery_on_completed_stays_completed(
    env: Path,
) -> None:
    """A duplicate completed write on an already-completed row must NOT
    fabricate a phantom error."""
    import rolemesh.main as m

    tenant_id, _b, _c, run_id = await _seed_run()
    assert await m._terminate_run_safe(run_id, tenant_id, success=True) is True
    redelivered = await m._terminate_run_safe(run_id, tenant_id, success=True)
    assert redelivered is False
    assert (
        await m._run_terminal_error_or_none(
            run_id, tenant_id, transitioned=redelivered, intent_error=None
        )
        is None
    )


async def test_frame_selection_snapshot_miss_falls_back_to_intent(env: Path) -> None:
    import rolemesh.main as m

    tenant_id, _b, _c, _run_id = await _seed_run()
    intent = {"code": "CONFIG_ERROR", "message": "bad tool name"}
    err = await m._run_terminal_error_or_none(
        str(uuid.uuid4()), tenant_id, transitioned=False, intent_error=intent
    )
    assert err == intent


# ---------------------------------------------------------------------------
# _terminate_and_emit_run_terminal — write first, then the mirroring chunk
# ---------------------------------------------------------------------------


async def test_success_emits_run_completed_chunk(env: Path) -> None:
    import rolemesh.main as m

    tenant_id, binding_id, chat_id, run_id = await _seed_run()
    gw, js = _make_gateway()

    await m._terminate_and_emit_run_terminal(
        gw,
        _Binding(binding_id),
        chat_id,
        run_id=run_id,
        tenant_id=tenant_id,
        success=True,
    )

    assert (await _read_run(tenant_id, run_id))["status"] == "completed"
    chunks = _stream_chunks(js, binding_id, chat_id)
    assert len(chunks) == 1
    assert chunks[0]["type"] == "run_completed"
    assert json.loads(chunks[0]["content"]) == {"run_id": run_id}


async def test_success_after_failure_emits_run_error_chunk(env: Path) -> None:
    """The end-to-end trace case at the single writer: the run already
    failed (content-filter kill mid-batch); the batch-final marker's
    completed write loses and the emitted chunk mirrors the failure."""
    import rolemesh.main as m

    tenant_id, binding_id, chat_id, run_id = await _seed_run()
    await m._terminate_run_safe(
        run_id,
        tenant_id,
        success=False,
        error={
            "code": "AGENT_ERROR",
            "message": "Output blocked by content filtering policy",
        },
    )
    gw, js = _make_gateway()

    await m._terminate_and_emit_run_terminal(
        gw,
        _Binding(binding_id),
        chat_id,
        run_id=run_id,
        tenant_id=tenant_id,
        success=True,
    )

    assert (await _read_run(tenant_id, run_id))["status"] == "failed"
    chunks = _stream_chunks(js, binding_id, chat_id)
    assert len(chunks) == 1
    assert chunks[0]["type"] == "run_error"
    inner = json.loads(chunks[0]["content"])
    assert inner["run_id"] == run_id
    assert inner["error"]["code"] == "AGENT_ERROR"


async def test_error_write_emits_run_error_chunk(env: Path) -> None:
    """The non-retryable-config-error site: container dead, no batch
    marker coming — the site itself both writes and notifies."""
    import rolemesh.main as m

    tenant_id, binding_id, chat_id, run_id = await _seed_run()
    gw, js = _make_gateway()

    error = {"code": "CONFIG_ERROR", "message": "tool name too long"}
    await m._terminate_and_emit_run_terminal(
        gw,
        _Binding(binding_id),
        chat_id,
        run_id=run_id,
        tenant_id=tenant_id,
        success=False,
        error=error,
    )

    assert (await _read_run(tenant_id, run_id))["status"] == "failed"
    chunks = _stream_chunks(js, binding_id, chat_id)
    assert len(chunks) == 1
    assert chunks[0]["type"] == "run_error"
    assert json.loads(chunks[0]["content"])["error"] == error


async def test_no_run_id_publishes_nothing(env: Path) -> None:
    import rolemesh.main as m

    _tenant_id, binding_id, chat_id, _run_id = await _seed_run()
    gw, js = _make_gateway()

    await m._terminate_and_emit_run_terminal(
        gw,
        _Binding(binding_id),
        chat_id,
        run_id=None,
        tenant_id=_tenant_id,
        success=True,
    )
    assert _stream_chunks(js, binding_id, chat_id) == []


async def test_non_web_gateway_writes_db_but_skips_chunk(env: Path) -> None:
    """IM channels terminal-write the run (a Telegram turn can carry a
    run in the future) but have no stream to notify."""
    import rolemesh.main as m

    tenant_id, binding_id, chat_id, run_id = await _seed_run()

    await m._terminate_and_emit_run_terminal(
        object(),  # not a WebNatsGateway
        _Binding(binding_id),
        chat_id,
        run_id=run_id,
        tenant_id=tenant_id,
        success=True,
    )
    assert (await _read_run(tenant_id, run_id))["status"] == "completed"
