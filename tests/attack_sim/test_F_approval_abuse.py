"""F. Approval flow abuse — modeling adversarial approver behavior.

Each test names the attacker, their goal, and what MUST prevent
success. These tests repackage the approval module's E2E coverage as
attack narratives so a security reviewer can see "approval defends X
by doing Y" in one glance.

If any of these starts failing, a defense regressed:
  F1. Non-approver decide                      → ForbiddenError
  F2. Two approvers race                       → exactly one wins
  F3. Self-promotion via policy edit           → prior snapshot frozen
  F4. NATS decided-event replay                → atomic claim prevents
                                                 double-execute
  F5. MCP JSON-RPC application error as success → worker reclassifies
  F6. Stop vs proposal NATS race               → orphan reaped by expiry
  F7. Concurrent expire + approve              → CAS wins once
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from rolemesh.approval.engine import (
    ApprovalEngine,
    ConflictError,
    ForbiddenError,
)
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.db import pg

from .conftest import VictimTenant, seed_victim

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolver() -> NotificationTargetResolver:
    async def _convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    async def _conv(cid: str) -> object | None:
        return await pg.get_conversation(cid)

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_conv,
        webui_base_url=None,
    )


def _engine(pub, ch) -> ApprovalEngine:
    return ApprovalEngine(publisher=pub, channel_sender=ch, resolver=_resolver())


async def _seed_approver_and_pending(
    victim: VictimTenant,
    engine: ApprovalEngine,
) -> tuple[str, str]:
    """Create a policy with the owner as approver, then generate a
    pending approval row. Returns (request_id, approver_id)."""
    await pg.create_approval_policy(
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[victim.owner_user_id],
    )
    await engine.handle_proposal(
        {
            "tenantId": victim.tenant_id,
            "coworkerId": victim.coworker_id,
            "conversationId": victim.conversation_id,
            "jobId": "attack-job",
            "userId": victim.owner_user_id,
            "rationale": "benign proposal",
            "actions": [
                {"mcp_server": "erp", "tool_name": "refund", "params": {"a": 1}},
            ],
        },
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
    )
    reqs = await pg.list_approval_requests(victim.tenant_id, status="pending")
    assert len(reqs) == 1
    return reqs[0].id, victim.owner_user_id


# ---------------------------------------------------------------------------
# F1. Non-approver decide
# ---------------------------------------------------------------------------


async def test_F1_non_approver_cannot_decide(fake_publisher, fake_channel) -> None:
    """Attacker: a user in the same tenant but not named in the policy.
    Goal: approve a request they were not authorised to decide.
    Defense: atomic CAS requires ``actor_user_id IN resolved_approvers``
    and raises ForbiddenError when the row is pending but the approver
    is unrecognised."""
    victim = await seed_victim()
    engine = _engine(fake_publisher, fake_channel)
    request_id, _ = await _seed_approver_and_pending(victim, engine)

    attacker = await pg.create_user(
        tenant_id=victim.tenant_id,
        name="attacker",
        email="mal@x.com",
        role="admin",
    )

    with pytest.raises(ForbiddenError):
        await engine.handle_decision(
            request_id=request_id,
            tenant_id=victim.tenant_id,
            action="approve",
            user_id=attacker.id,
        )

    fresh = await pg.get_approval_request(request_id, tenant_id=victim.tenant_id)
    assert fresh is not None and fresh.status == "pending"


# ---------------------------------------------------------------------------
# F2. Concurrent approve race
# ---------------------------------------------------------------------------


async def test_F2_concurrent_approve_wins_once(fake_publisher, fake_channel) -> None:
    """Attacker: a compromised approver account + a legitimate approver,
    both issuing decisions simultaneously (via timing attack or MITM).
    Goal: produce inconsistent state (row both approved and rejected,
    or the worker firing twice).
    Defense: atomic pending→approved|rejected CAS — exactly one wins,
    other sees ConflictError."""
    victim = await seed_victim()
    other_approver = await pg.create_user(
        tenant_id=victim.tenant_id,
        name="other",
        email="o@x.com",
        role="admin",
    )
    await pg.create_approval_policy(
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[victim.owner_user_id, other_approver.id],
    )
    engine = _engine(fake_publisher, fake_channel)
    await engine.handle_proposal(
        {
            "tenantId": victim.tenant_id,
            "coworkerId": victim.coworker_id,
            "conversationId": victim.conversation_id,
            "jobId": "job-concurrent",
            "userId": victim.owner_user_id,
            "rationale": "r",
            "actions": [{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
        },
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
    )
    req = (await pg.list_approval_requests(victim.tenant_id, status="pending"))[0]

    results = await asyncio.gather(
        engine.handle_decision(
            request_id=req.id, tenant_id=victim.tenant_id, action="approve", user_id=victim.owner_user_id
        ),
        engine.handle_decision(
            request_id=req.id, tenant_id=victim.tenant_id, action="reject", user_id=other_approver.id
        ),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(successes) == 1, f"expected 1 winner; got {results}"
    assert len(conflicts) == 1, f"expected 1 conflict; got {results}"

    audit = await pg.list_approval_audit(req.id, tenant_id=victim.tenant_id)
    terminals = [e for e in audit if e.action in ("approved", "rejected")]
    assert len(terminals) == 1, (
        "atomic CAS must emit exactly one terminal audit; got "
        f"{[e.action for e in audit]}"
    )


# ---------------------------------------------------------------------------
# F3. Self-promotion via policy edit
# ---------------------------------------------------------------------------


async def test_F3_self_promotion_cannot_reach_prior_pending(
    fake_publisher, fake_channel
) -> None:
    """Attacker: a tenant admin with REST access. Target: a pending
    approval whose policy originally did NOT list them. Attacker
    edits policy to add themselves, then tries /decide.
    Defense: ``resolved_approvers`` is a snapshot taken at request
    creation; later policy edits do not widen authority on open
    requests."""
    victim = await seed_victim()
    engine = _engine(fake_publisher, fake_channel)

    original_approver = victim.owner_user_id
    attacker = await pg.create_user(
        tenant_id=victim.tenant_id,
        name="mallory",
        email="m@x.com",
        role="admin",
    )
    policy = await pg.create_approval_policy(
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[original_approver],
    )
    await engine.handle_proposal(
        {
            "tenantId": victim.tenant_id,
            "coworkerId": victim.coworker_id,
            "conversationId": victim.conversation_id,
            "jobId": "self-promote",
            "userId": original_approver,
            "rationale": "r",
            "actions": [{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
        },
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
    )
    req = (await pg.list_approval_requests(victim.tenant_id, status="pending"))[0]
    assert req.resolved_approvers == [original_approver]

    # Attacker edits the policy to add themselves.
    await pg.update_approval_policy(
        policy.id,
        tenant_id=victim.tenant_id,
        approver_user_ids=[original_approver, attacker.id],
    )

    with pytest.raises(ForbiddenError):
        await engine.handle_decision(
            request_id=req.id, tenant_id=victim.tenant_id, action="approve", user_id=attacker.id
        )


# ---------------------------------------------------------------------------
# F4. NATS decided-event replay
# ---------------------------------------------------------------------------


async def test_F4_decided_event_replay_does_not_double_execute(
    fake_publisher, fake_channel
) -> None:
    """Attacker vector: captures an ``approval.decided`` NATS message
    and replays it, attempting to make the worker execute the same
    action twice.
    Defense: worker claim is ``approved → executing`` atomic. Once
    the row is past 'approved', the second claim returns None and
    the worker ack's and drops."""
    victim = await seed_victim()
    engine = _engine(fake_publisher, fake_channel)
    request_id, approver_id = await _seed_approver_and_pending(victim, engine)
    await engine.handle_decision(
        request_id=request_id, tenant_id=victim.tenant_id, action="approve", user_id=approver_id
    )

    first = await pg.claim_approval_for_execution(request_id, tenant_id=victim.tenant_id)
    assert first is not None and first.status == "executing"

    second = await pg.claim_approval_for_execution(request_id, tenant_id=victim.tenant_id)
    assert second is None, (
        "atomic claim must return None on replay — worker drops "
        "replayed messages silently"
    )


# ---------------------------------------------------------------------------
# F5. MCP JSON-RPC error disguised as success
# ---------------------------------------------------------------------------


def test_F5_jsonrpc_error_classified_as_failure() -> None:
    """Attacker vector: a misbehaving or compromised MCP server returns
    HTTP 200 with a JSON-RPC error body. If the worker treated HTTP
    200 as unconditional success, the admin UI would show green on a
    failed (or hostile) operation.
    Defense: ``_execute_actions`` inspects the JSON-RPC body for
    ``error`` regardless of HTTP status.

    This is a pure classification test — mirror the worker's logic
    here so a refactor either updates this test or the behavior
    contract breaks visibly.
    """

    def _classify(resp_status: int, body_text: str) -> dict[str, object]:
        if resp_status >= 400:
            return {"error": f"MCP {resp_status}: {body_text[:200]}"}
        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError:
            return {"ok": True, "response": {"raw": body_text}}
        if (
            isinstance(parsed, dict)
            and parsed.get("error") is not None
            and parsed.get("result") is None
        ):
            err = parsed["error"]
            if isinstance(err, dict):
                code = err.get("code")
                return {
                    "error": (
                        f"MCP error{f' {code}' if code is not None else ''}: "
                        f"{err.get('message') or str(err)}"
                    ),
                    "jsonrpc_error": err,
                }
            return {"error": f"MCP error: {err!r}"}
        return {"ok": True, "response": parsed}

    good = _classify(
        200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    )
    assert good.get("ok") is True

    bad = _classify(
        200,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        ),
    )
    assert "error" in bad, (
        "JSON-RPC error body with HTTP 200 must be classified as per-action "
        "error, not ok — otherwise admin UI shows green on a failed call"
    )
    assert "Invalid Request" in str(bad["error"])


# ---------------------------------------------------------------------------
# F6. Stop vs proposal race — orphan reaped by expiry (known limitation)
# ---------------------------------------------------------------------------


async def test_F6_stop_race_orphan_reaped_by_expiry(
    fake_publisher, fake_channel
) -> None:
    """Attacker vector: exploit NATS cross-subject ordering non-guarantee
    to issue cancel BEFORE the proposal it should cancel. Goal: leave
    an untracked pending request that executes later.
    Current design (documented limitation in
    docs/approval-architecture.md §Known Gaps): orphan pending reaped
    by the expiry loop within ``auto_expire_minutes``. No execution
    happens from the orphan state (pending never calls MCP)."""
    victim = await seed_victim()
    engine = _engine(fake_publisher, fake_channel)
    await pg.create_approval_policy(
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[victim.owner_user_id],
        auto_expire_minutes=1,
    )

    # Cancel arrives first → finds nothing.
    cancelled = await engine.cancel_for_job("race-job")
    assert cancelled == []

    # Proposal lands late.
    await engine.handle_proposal(
        {
            "tenantId": victim.tenant_id,
            "coworkerId": victim.coworker_id,
            "conversationId": victim.conversation_id,
            "jobId": "race-job",
            "userId": victim.owner_user_id,
            "rationale": "r",
            "actions": [{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
        },
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
    )
    rows = await pg.list_approval_requests(victim.tenant_id)
    assert len(rows) == 1
    orphan = rows[0]
    assert orphan.status == "pending"

    # Force past deadline and run expiry.
    pool = pg._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE approval_requests SET expires_at = $1 WHERE id = $2::uuid",
            datetime.now(UTC) - timedelta(seconds=1),
            orphan.id,
        )
    await engine.expire_stale_requests()
    after = await pg.get_approval_request(orphan.id, tenant_id=victim.tenant_id)
    assert after is not None and after.status == "expired"


# ---------------------------------------------------------------------------
# F7. Concurrent expire + approve
# ---------------------------------------------------------------------------


async def test_F7_expire_and_approve_race_wins_once(
    fake_publisher, fake_channel
) -> None:
    """Attacker vector: race the approve against expiry. Goal: end up
    with both ``approved`` and ``expired`` audit rows (nonsensical),
    or a worker firing after expiry.
    Defense: both transitions use ``WHERE status = 'pending'`` CAS,
    exactly one wins."""
    victim = await seed_victim()
    engine = _engine(fake_publisher, fake_channel)
    request_id, approver_id = await _seed_approver_and_pending(victim, engine)

    pool = pg._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE approval_requests SET expires_at = now() - interval '1 second' "
            "WHERE id = $1::uuid",
            request_id,
        )

    async def _approver() -> Exception | None:
        try:
            await engine.handle_decision(
                request_id=request_id, tenant_id=victim.tenant_id, action="approve", user_id=approver_id
            )
            return None
        except Exception as exc:  # noqa: BLE001
            return exc

    approver_outcome, _ = await asyncio.gather(
        _approver(), engine.expire_stale_requests()
    )
    fresh = await pg.get_approval_request(request_id, tenant_id=victim.tenant_id)
    assert fresh is not None
    assert fresh.status in ("approved", "expired")

    if fresh.status == "approved":
        assert approver_outcome is None
    else:
        assert isinstance(approver_outcome, ConflictError)

    audit_actions = [e.action for e in await pg.list_approval_audit(request_id, tenant_id=victim.tenant_id)]
    terminals = [a for a in audit_actions if a in ("approved", "expired", "rejected")]
    assert len(terminals) == 1, f"audit must have one terminal; got {audit_actions!r}"
