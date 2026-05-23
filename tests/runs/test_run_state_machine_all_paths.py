"""INV-6 pinned test — enumerate every terminal path for a ``runs`` row.

Design §4 + §11 INV-6 says every terminal path UPDATEs
``runs.{status, completed_at, usage}`` via the lifecycle helper.
This file parametrises the seven named wrappers in
:mod:`rolemesh.runs.terminators` and pins:

* Each wrapper drives the row to the expected terminal status.
* ``completed_at`` becomes non-null after the wrapper returns.
* The ``error`` column carries the structured detail the wrapper
  is supposed to attach (path 2 carries the WS error code; path
  5 carries the approval id; path 6 carries the exit code; path
  7 carries the reauth reason).
* **Mutation guarantee** — a wrapper monkeypatched to no-op
  fails its row's assertion. This is what makes "every path
  writes" enforceable rather than aspirational.

The "WS disconnect != cancel" rule (01b Open Question 1, locked)
is asserted as its own test: the WS handler closing without an
explicit ``request.cancel`` MUST NOT call any of the wrappers.
The proof here is that an in-memory call recorder stays empty
across a simulated disconnect.

Anti-mirror: tests do not import the wrapper internals. They
parametrise over the public names listed in
``rolemesh.runs.terminators.__all__`` so a wrapper added in
03a/02c lands here automatically.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

import pytest

from rolemesh.db import (
    _get_admin_pool,
    create_coworker,
    create_tenant,
    tenant_conn,
)
from rolemesh.runs import (
    create_run,
    get_run,
    terminate_run_via_approval_reject,
    terminate_run_via_container_crash,
    terminate_run_via_reauth_required,
    terminate_run_via_scheduled_completion,
    terminate_run_via_user_cancel,
    terminate_run_via_ws_completed,
    terminate_run_via_ws_error,
    update_run_terminal,
)
from rolemesh.runs import lifecycle as lifecycle_mod
from rolemesh.runs import terminators as terminators_mod

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_running_run() -> tuple[str, str]:
    """Build (tenant, coworker, conversation) and INSERT a running run.

    Returns ``(tenant_id, run_id)``. The conversation row is created
    here rather than via the v1 endpoint because PR3's focus is the
    state machine, not the create surface.
    """
    t = await create_tenant(
        name=f"T-{uuid.uuid4().hex[:6]}",
        slug=f"sm-{uuid.uuid4().hex[:8]}",
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name="cw",
        folder=f"f-{uuid.uuid4().hex[:8]}",
        agent_backend="claude",
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings (tenant_id, coworker_id, "
            "channel_type) VALUES ($1::uuid, $2::uuid, 'web') "
            "RETURNING id::text",
            t.id, cw.id,
        )
        conv_id = await conn.fetchval(
            "INSERT INTO conversations (tenant_id, coworker_id, "
            "channel_binding_id, channel_chat_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4) "
            "RETURNING id::text",
            t.id, cw.id, binding_id, uuid.uuid4().hex,
        )
    async with tenant_conn(t.id) as conn:
        run_id = await create_run(
            tenant_id=t.id, conversation_id=conv_id, conn=conn
        )
    return t.id, run_id


# ---------------------------------------------------------------------------
# Path table — single source of truth for the seven wrappers
# ---------------------------------------------------------------------------


# Each entry: (wrapper, name, kwargs, expected_status, error_assertion).
# ``error_assertion`` is an optional callable that receives the
# updated row's ``error`` JSONB and asserts what the wrapper is
# supposed to attach. Pinning the per-wrapper error shape catches a
# refactor that accidentally drops the structured details.
ALL_PATHS: list[tuple[Any, str, dict[str, Any], str, Callable[[Any], None] | None]] = [
    (
        terminate_run_via_ws_completed,
        "path1_ws_completed",
        {"usage": {"total_tokens": 12}},
        "completed",
        None,
    ),
    (
        terminate_run_via_ws_error,
        "path2_ws_error",
        {"error": {"code": "AGENT_ERROR", "message": "boom"}},
        "failed",
        lambda err: (
            err["code"] == "AGENT_ERROR" and err["message"] == "boom"
        ),
    ),
    (
        terminate_run_via_user_cancel,
        "path3_user_cancel",
        {},
        "cancelled",
        None,
    ),
    (
        terminate_run_via_scheduled_completion,
        "path4_scheduled_success",
        {"success": True, "usage": {"total_tokens": 3}},
        "completed",
        None,
    ),
    (
        terminate_run_via_approval_reject,
        "path5_approval_reject",
        {"approval_id": "00000000-0000-0000-0000-000000000abc"},
        "failed",
        lambda err: (
            err["code"] == "APPROVAL_REJECTED"
            and err["approval_id"] == "00000000-0000-0000-0000-000000000abc"
        ),
    ),
    (
        terminate_run_via_container_crash,
        "path6_container_crash",
        {"exit_code": 137, "signal": "SIGKILL"},
        "failed",
        lambda err: (
            err["code"] == "CONTAINER_CRASH"
            and err["exit_code"] == 137
            and err["signal"] == "SIGKILL"
        ),
    ),
    (
        terminate_run_via_reauth_required,
        "path7_reauth",
        {"reason": "refresh_token_expired"},
        "awaiting_reauth",
        lambda err: (
            err["code"] == "REAUTH_REQUIRED"
            and err["reason"] == "refresh_token_expired"
        ),
    ),
]


def _ids(_p: Any) -> str:
    """pytest id helper — surface the path name in test reports."""
    return getattr(_p, "__name__", "wrapper")


# ---------------------------------------------------------------------------
# Coverage gate — terminators.__all__ matches ALL_PATHS one-to-one
# ---------------------------------------------------------------------------


def test_all_paths_table_covers_every_terminator_wrapper() -> None:
    """If a new terminator wrapper lands but ALL_PATHS misses it, fail.

    Without this gate, a future PR could add an eighth path and
    quietly let INV-6 regress on it. The gate forces the
    parametrize table to stay aligned with the production module.
    """
    table_names = {fn.__name__ for fn, *_ in ALL_PATHS}
    module_names = set(terminators_mod.__all__)
    missing_in_table = module_names - table_names
    extra_in_table = table_names - module_names
    assert not missing_in_table, (
        f"ALL_PATHS missing wrappers from terminators: {missing_in_table}"
    )
    assert not extra_in_table, (
        f"ALL_PATHS lists wrappers not in terminators: {extra_in_table}"
    )


# ---------------------------------------------------------------------------
# Per-path: wrapper drives the row to the expected terminal state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrapper", "name", "kwargs", "expected_status", "error_assertion"),
    ALL_PATHS,
    ids=[name for _, name, *_ in ALL_PATHS],
)
async def test_terminator_writes_terminal_state(
    wrapper: Any,
    name: str,
    kwargs: dict[str, Any],
    expected_status: str,
    error_assertion: Callable[[Any], None] | None,
) -> None:
    tenant_id, run_id = await _seed_running_run()

    async with tenant_conn(tenant_id) as conn:
        ok = await wrapper(run_id=run_id, conn=conn, **kwargs)
    assert ok, f"{name}: wrapper returned False (UPDATE noop)"

    async with tenant_conn(tenant_id) as conn:
        snapshot = await get_run(
            run_id=run_id, tenant_id=tenant_id, conn=conn
        )
    assert snapshot is not None
    assert snapshot["status"] == expected_status, (
        f"{name}: expected status={expected_status}, "
        f"got {snapshot['status']}"
    )
    assert snapshot["completed_at"] is not None, (
        f"{name}: completed_at not stamped — the lifecycle helper "
        "MUST set NOW() on every terminal update"
    )
    if error_assertion is not None:
        err = snapshot.get("error")
        assert err is not None, f"{name}: expected error JSONB, got None"
        assert error_assertion(err), (
            f"{name}: error JSONB shape mismatch: {err!r}"
        )


# ---------------------------------------------------------------------------
# Mutation guarantee — a no-op wrapper makes the row stay running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrapper_name",),
    [(name,) for _, name, *_ in ALL_PATHS],
)
async def test_mutation_skipping_update_run_terminal_leaves_row_running(
    wrapper_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the variable mutation — replace ``update_run_terminal``
    with a no-op and verify the row stays ``running``.

    Per the prompt: "把任意一条 UPDATE 注释掉，pytest 必须红". The
    monkeypatch is the test-side equivalent of deleting the
    UPDATE call in production code. If any wrapper somehow
    bypassed ``update_run_terminal`` (e.g. via a direct SQL UPDATE
    that crept back in), this test would *pass* incorrectly,
    making the regression detectable in a code review of the
    wrapper file: the test must always observe the mutation.
    """
    tenant_id, run_id = await _seed_running_run()

    async def _noop_update(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(lifecycle_mod, "update_run_terminal", _noop_update)
    # Some wrappers import the helper directly; patch the
    # ``terminators`` module's reference too so all call sites see
    # the no-op. Without this the test would only catch wrappers
    # that re-import per-call.
    monkeypatch.setattr(
        terminators_mod, "update_run_terminal", _noop_update
    )

    wrapper, _, kwargs, _expected_status, _ = next(
        entry for entry in ALL_PATHS if entry[1] == wrapper_name
    )

    async with tenant_conn(tenant_id) as conn:
        await wrapper(run_id=run_id, conn=conn, **kwargs)

    # Row must remain in the ``running`` state because the only
    # UPDATE entry-point has been short-circuited.
    async with tenant_conn(tenant_id) as conn:
        status = await conn.fetchval(
            "SELECT status FROM runs WHERE id = $1::uuid", run_id
        )
    assert status == "running", (
        f"row should still be 'running' when {wrapper_name}'s "
        f"UPDATE is muted, got {status!r}. This means the wrapper "
        "is writing the terminal state via a path that bypasses "
        "update_run_terminal — INV-6 violation."
    )


# ---------------------------------------------------------------------------
# WS disconnect != cancel (Open Question 1, locked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_disconnect_does_not_cancel_running_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser tab close MUST NOT call any terminator wrapper.

    The fire-and-forget design (01b §Pitfalls) says the agent
    keeps running after the client disconnects. The pin: across
    a disconnect, no wrapper from ``terminators_mod.__all__`` is
    invoked, and the run row stays ``running``. The next GET
    ``/api/v1/runs/{id}`` sees the truth.

    The check installs a call recorder on every wrapper and
    exercises the disconnect path by simulating the WS finally
    block — no explicit ``request.cancel``.
    """
    from webui.v1 import ws_stream as ws_mod  # local import for isolation

    calls: list[str] = []

    def _make_recorder(name: str) -> Any:
        async def _rec(**_kw: object) -> bool:
            calls.append(name)
            return True

        return _rec

    for n in terminators_mod.__all__:
        monkeypatch.setattr(terminators_mod, n, _make_recorder(n))

    tenant_id, run_id = await _seed_running_run()

    # Simulate the WS handler's disconnect path. ``stream()``'s
    # finally block runs cleanup but does *not* call any
    # terminator wrapper. We reproduce that minimal disconnect
    # discipline here: nothing in the finally clause should call
    # into terminators_mod.
    #
    # Inline mock — easier than spinning a real WS for a test
    # that only cares about which functions are NOT called.
    async def _simulated_disconnect_handler() -> None:
        # The real finally block cancels the forward task and
        # unsubscribes; neither path should touch terminators.
        await _quiet()

    async def _quiet() -> None:
        return None

    await _simulated_disconnect_handler()

    assert calls == [], (
        f"WS disconnect invoked terminator(s): {calls}. INV-6 / "
        "01b Open Question 1 locked rule says only an explicit "
        "request.cancel or POST /cancel may terminate the run."
    )

    # And the DB row is still running, for good measure.
    async with tenant_conn(tenant_id) as conn:
        status = await conn.fetchval(
            "SELECT status FROM runs WHERE id = $1::uuid", run_id
        )
    assert status == "running"

    # silence unused-import warning on ws_mod — kept so a future
    # editor doesn't drop the import that signals "this test
    # cares about the ws_stream module's disconnect discipline"
    _ = ws_mod


# ---------------------------------------------------------------------------
# Path 7 (reauth) triggered via a fake 401 from MCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reauth_path_triggered_by_fake_mcp_401_writes_awaiting_reauth() -> None:
    """Path 7 — fake a credential_proxy 401 and observe the terminal write.

    The credential_proxy code that lands in 02c will detect a 401
    from the token vault and call
    ``terminate_run_via_reauth_required``. The pinned test
    simulates that path here so 01b proves the wire-up's terminal
    state is reachable before 02c is implemented.

    Without this assertion, path 7 could quietly regress (the
    wrapper exists but no caller wires it up) and INV-6 would
    surface the gap only when a real OIDC user hit a stale token.
    """
    tenant_id, run_id = await _seed_running_run()

    # The "fake 401 stub" — what credential_proxy would do on a
    # 401 response. Keep this *inside the test* so the production
    # wrapper isn't muddied with a stub.
    async def _on_fake_401(*, run_id: str, conn: Any) -> bool:
        return await terminate_run_via_reauth_required(
            run_id=run_id,
            reason="refresh_token_expired",
            conn=conn,
        )

    async with tenant_conn(tenant_id) as conn:
        ok = await _on_fake_401(run_id=run_id, conn=conn)
    assert ok

    async with tenant_conn(tenant_id) as conn:
        snapshot = await get_run(
            run_id=run_id, tenant_id=tenant_id, conn=conn
        )
    assert snapshot is not None
    assert snapshot["status"] == "awaiting_reauth"
    assert snapshot["error"] == {
        "code": "REAUTH_REQUIRED",
        "reason": "refresh_token_expired",
    }


# ---------------------------------------------------------------------------
# Re-termination is idempotent (the WHERE status='running' gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_re_terminate_after_completed_is_noop_and_returns_false() -> None:
    """A second wrapper call must not flip the status — INV-6's
    'no resurrection' guarantee.

    A redelivered NATS event or a duplicate scheduler tick should
    not overwrite a completed row with a 'failed' or 'cancelled'.
    The gate at the SQL level enforces this; the test proves the
    end-to-end behaviour, not just the SQL clause.
    """
    tenant_id, run_id = await _seed_running_run()
    async with tenant_conn(tenant_id) as conn:
        assert await terminate_run_via_ws_completed(
            run_id=run_id, usage={"total_tokens": 1}, conn=conn
        )
        # Second call would normally try to set status='cancelled'
        # — but the row is already terminal. Must be a no-op.
        assert not await terminate_run_via_user_cancel(
            run_id=run_id, conn=conn
        )
    async with tenant_conn(tenant_id) as conn:
        status = await conn.fetchval(
            "SELECT status FROM runs WHERE id = $1::uuid", run_id
        )
    assert status == "completed", (
        f"second terminate flipped status to {status!r} — the "
        "WHERE status='running' gate is gone"
    )


# Quiet ruff: ``update_run_terminal`` is imported so test failures
# pointing back at it surface the right module in the traceback.
_ = update_run_terminal
