"""G. Denial of service attempts.

Attacks:
  G3. Oversized tool_input payload — pipeline must not OOM or hang
  G4. Approval flood — many proposals concurrently
  G5. Safety pipeline with many rules — check registry is O(N)
  G6. Audit write pressure — writes remain fast under load

Infrastructure-level DoS (G1 fork bomb, G2 infinite loop) are verified
via ``scripts/verify-hardening.sh`` against a live container — not
reproducible in a pure pytest. They appear in the manual runbook.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import rolemesh.agent  # noqa: F401

from rolemesh.safety.types import SafetyContext, Stage  # noqa: E402


# ---------------------------------------------------------------------------
# G3. Oversized tool_input
# ---------------------------------------------------------------------------


async def test_G3_oversized_tool_input_pipeline_survives() -> None:
    """Attacker: craft a tool_input with 5 MB of garbage strings to
    overload the safety pipeline or the audit write.
    Defense (desired): pipeline should complete within seconds and
    not OOM. No explicit size limit today (documented gap), but
    regex/domain_allowlist checks should be linear-time on payload
    size."""
    from rolemesh.safety.checks.domain_allowlist import DomainAllowlistCheck

    # 1 MB of junk text — not comically huge but reliably beyond
    # typical tool payloads.
    big_blob = "abc " * (250_000)  # ~1 MB
    ctx = SafetyContext(
        stage=Stage.PRE_TOOL_CALL,
        tenant_id="t",
        coworker_id="cw",
        user_id="u",
        job_id="j",
        conversation_id="c",
        payload={"tool_name": "Bash", "tool_input": {"command": big_blob}},
    )

    check = DomainAllowlistCheck()
    t0 = time.monotonic()
    verdict = await check.check(ctx, {"allowed_hosts": ["api.anthropic.com"]})
    elapsed = time.monotonic() - t0
    # Soft SLO: 1 MB payload must not take multi-second work.
    assert elapsed < 2.0, (
        f"domain_allowlist took {elapsed:.2f}s on 1MB input — pipeline "
        "has an O(N^2) path and is a DoS surface"
    )
    # No URL in the junk → allow.
    assert verdict.action == "allow"


# ---------------------------------------------------------------------------
# G4. Approval flood
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("test_db")
async def test_G4_approval_flood_does_not_corrupt_state(
    fake_publisher, fake_channel
) -> None:
    """Attacker: burst 50 submit_proposal calls in rapid succession,
    each unique (no dedup). Goal: confuse CAS / audit trigger / index
    on approval_requests.
    Defense: atomic inserts + audit trigger are per-row, independent.
    Assert all 50 rows are distinct and audit is consistent."""
    from rolemesh.approval.engine import ApprovalEngine
    from rolemesh.approval.notification import NotificationTargetResolver
    from rolemesh.db import pg

    from .conftest import seed_victim

    victim = await seed_victim("flood")
    await pg.create_approval_policy(
        tenant_id=victim.tenant_id,
        coworker_id=victim.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[victim.owner_user_id],
    )

    async def _resolver_get_convs(u: str, c: str) -> list[str]:
        return []

    async def _resolver_get_conv(cid: str) -> object | None:
        return await pg.get_conversation(cid)

    engine = ApprovalEngine(
        publisher=fake_publisher,
        channel_sender=fake_channel,
        resolver=NotificationTargetResolver(
            get_conversations_for_user_and_coworker=_resolver_get_convs,
            get_conversation=_resolver_get_conv,
            webui_base_url=None,
        ),
    )

    N = 50
    tasks = []
    for i in range(N):
        tasks.append(
            engine.handle_proposal(
                {
                    "tenantId": victim.tenant_id,
                    "coworkerId": victim.coworker_id,
                    "conversationId": victim.conversation_id,
                    "jobId": f"flood-{i}",
                    "userId": victim.owner_user_id,
                    "rationale": f"r{i}",
                    "actions": [
                        {
                            "mcp_server": "erp",
                            "tool_name": "refund",
                            "params": {"idx": i},
                        }
                    ],
                },
                tenant_id=victim.tenant_id,
                coworker_id=victim.coworker_id,
            )
        )
    await asyncio.gather(*tasks)

    rows = await pg.list_approval_requests(victim.tenant_id, status="pending")
    assert len(rows) == N, f"expected {N} distinct requests, got {len(rows)}"
    # Each row has one 'created' audit entry — trigger kept up.
    for r in rows:
        audit = await pg.list_approval_audit(r.id)
        assert audit and audit[0].action == "created", (
            f"request {r.id} missing created audit under flood"
        )


# ---------------------------------------------------------------------------
# G5. Registry with many checks
# ---------------------------------------------------------------------------


def test_G5_registry_lookup_is_constant_time() -> None:
    """Attacker path: convince admins to register hundreds of rules to
    slow down the per-turn pipeline. Defense: check registry lookup
    is dict-based O(1). Confirmed here at spec level; real-world
    throughput is dominated by check body (ML model call), not
    registry lookup."""
    from rolemesh.safety.errors import UnknownCheckError
    from rolemesh.safety.registry import CheckRegistry

    registry = CheckRegistry()
    # Registry raises UnknownCheckError on miss (fail-close contract).
    # We just measure the lookup path; the error is expected and cheap.
    t0 = time.monotonic()
    for _ in range(10_000):
        try:
            registry.get("nonexistent")
        except UnknownCheckError:
            pass
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, (
        f"10k registry lookups took {elapsed:.2f}s — expected <0.5s for O(1)"
    )


# ---------------------------------------------------------------------------
# G6. Audit write pressure
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("test_db")
async def test_G6_audit_write_pressure() -> None:
    """Attacker: flood safety_decisions writes via repeated tool calls
    to saturate DB. Goal: degrade orchestrator throughput.
    Defense: audit write is a single insert; Postgres handles
    thousands/sec. We verify 500 writes complete in reasonable time."""
    from rolemesh.db import pg
    from rolemesh.safety.audit import AuditEvent, DbAuditSink

    from .conftest import seed_victim

    victim = await seed_victim("audit-load")
    sink = DbAuditSink()

    t0 = time.monotonic()
    tasks = []
    for i in range(500):
        event = AuditEvent(
            tenant_id=victim.tenant_id,
            coworker_id=victim.coworker_id,
            user_id=victim.owner_user_id,
            job_id=f"j{i}",
            conversation_id=victim.conversation_id,
            stage="pre_tool_call",
            verdict_action="allow",
            triggered_rule_ids=[],
            findings=[],
            context_digest=f"digest-{i}",
            context_summary="tool:x",
            approval_context=None,
        )
        tasks.append(sink.write(event))
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0

    rows = await pg.list_safety_decisions(victim.tenant_id, limit=1000)
    assert len(rows) >= 500
    # Soft SLO. Allow 10s for 500 writes; real prod is typically <2s.
    assert elapsed < 10.0, f"500 audit writes took {elapsed:.2f}s"
