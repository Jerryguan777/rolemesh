"""G. Denial of service attempts.

Attacks:
  G3. Oversized tool_input payload — pipeline must not OOM or hang
  G5. Safety pipeline with many rules — check registry is O(N)
  G6. Audit write pressure — writes remain fast under load

Infrastructure-level DoS (G1 fork bomb, G2 infinite loop) are verified
via ``scripts/verify-hardening.sh`` against a live container — not
reproducible in a pure pytest. They appear in the manual runbook.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

import rolemesh.agent  # noqa: F401
from rolemesh.db import list_safety_decisions
from rolemesh.safety.types import SafetyContext, Stage

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
        with contextlib.suppress(UnknownCheckError):
            registry.get("nonexistent")
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
        )
        tasks.append(sink.write(event))
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0

    rows = await list_safety_decisions(victim.tenant_id, limit=1000)
    assert len(rows) >= 500
    # Soft SLO. Allow 10s for 500 writes; real prod is typically <2s.
    assert elapsed < 10.0, f"500 audit writes took {elapsed:.2f}s"
