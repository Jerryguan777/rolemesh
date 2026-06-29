"""E. Tenant isolation — cross-tenant access attempts.

Attacker legitimately controls their own tenant A and tries to make the
orchestrator act on, or read, victim tenant B.

Historical note: the original E1/E2 drove the approval engine's
``handle_auto_intercept`` (deleted with the human-approval subsystem in
6d79276). The *defense* — "take the trusted tenant from the authoritative
coworker lookup, never from the attacker's payload claim" — did not go
away; it now lives on the safety RPC / event planes. These tests pin that
live guard: ``SafetyRpcServer._handle_request_inner`` drops any request
whose claimed ``tenant_id`` disagrees with the authoritative tenant of the
claimed ``coworker_id`` (the same shape ``safety/subscriber.py`` uses for
events).

  E1. Forge tenantId (keep own coworker)      → drop: tenant_id mismatch.
  E2. Forge coworkerId of another tenant       → drop: the coworker's
                                                 authoritative tenant ≠ the
                                                 claimed tenant.
  E2b. Forge a coworkerId that doesn't exist   → drop: unknown coworker.
  E3. Consistent own identity (control)        → passes the identity gate
                                                 (reaches check lookup).
  E6. NATS subject sidechannel                 → XFAIL: a FULLY consistent
                                                 forge (victim coworker_id +
                                                 victim tenant_id) still
                                                 passes this guard; only
                                                 NATS account-per-tenant
                                                 closes it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rolemesh.core.types import Coworker
from rolemesh.safety.registry import CheckRegistry
from rolemesh.safety.rpc_codec import serialize_context
from rolemesh.safety.rpc_server import SafetyRpcServer
from rolemesh.safety.types import SafetyContext, Stage


class _CapturingMsg:
    """Stand-in for a core-NATS message — the external transport boundary.

    Captures the server's reply so the test can assert on it. Faking the
    NATS msg (not any rolemesh code) is legitimate: it is the network edge,
    exactly the boundary the project's testing guidance allows to mock."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.data = json.dumps(payload).encode("utf-8")
        self.reply: dict[str, Any] | None = None

    async def respond(self, data: bytes) -> None:
        self.reply = json.loads(data)


def _server(coworkers: list[Coworker]) -> SafetyRpcServer:
    """Build a real SafetyRpcServer.

    ``coworker_lookup`` is a ``Callable[[str], TrustedCoworker | None]`` —
    dependency-injected in production. A dict over real ``Coworker`` objects
    IS that contract (not a mock of an internal module). The registry is
    empty so a legitimate request falls through to an "unknown check_id"
    reply, which is the signal that the identity gate let it pass.
    nats_client / thread_pool are unused on the identity-reject and
    unknown-check paths, so they need no broker or pool here.
    """
    by_id = {c.id: c for c in coworkers}
    return SafetyRpcServer(
        nats_client=None,
        registry=CheckRegistry(),
        thread_pool=None,  # type: ignore[arg-type]  # never reached in these paths
        coworker_lookup=by_id.get,
    )


def _request(*, claimed_tenant: str, claimed_coworker: str, check_id: str = "x.check") -> dict[str, Any]:
    ctx = SafetyContext(
        stage=Stage.PRE_TOOL_CALL,
        tenant_id=claimed_tenant,
        coworker_id=claimed_coworker,
        user_id="u",
        job_id="j",
        conversation_id="c",
        payload={"tool_name": "refund", "tool_input": {"amount": 9999}},
    )
    return {
        "request_id": "r1",
        "context": serialize_context(ctx),
        "check_id": check_id,
        "config": {},
    }


_ATTACKER = Coworker(id="cw-attacker", tenant_id="t-attacker", name="A", folder="a")
_VICTIM = Coworker(id="cw-victim", tenant_id="t-victim", name="V", folder="v")


# ---------------------------------------------------------------------------
# E1. Forge tenantId in the request payload
# ---------------------------------------------------------------------------


async def test_E1_forged_tenant_id_dropped() -> None:
    """Attacker: tenant A's container sends a safety RPC for its OWN coworker
    but claims ``tenant_id = victim B`` in the payload, hoping the
    orchestrator runs the check against B's view. Defense: the authoritative
    tenant of ``cw-attacker`` is A; A ≠ B, so the request is dropped before
    any check runs."""
    server = _server([_ATTACKER, _VICTIM])
    msg = _CapturingMsg(
        _request(claimed_tenant=_VICTIM.tenant_id, claimed_coworker=_ATTACKER.id)
    )
    await server._handle_request(msg)

    assert msg.reply is not None
    assert msg.reply["verdict"] is None, "a dropped request must not run a check"
    assert "tenant_id mismatch" in msg.reply["error"], (
        f"forged tenant must be dropped; got {msg.reply['error']!r}"
    )


# ---------------------------------------------------------------------------
# E2. Forge coworkerId belonging to another tenant
# ---------------------------------------------------------------------------


async def test_E2_forged_coworker_id_dropped() -> None:
    """Attacker: tenant A claims victim B's ``coworker_id`` but (not knowing
    B's tenant) keeps its own ``tenant_id = A``. Defense: the authoritative
    tenant of ``cw-victim`` is B; B ≠ A → dropped. The guard anchors on the
    coworker's real tenant, never on the claim."""
    server = _server([_ATTACKER, _VICTIM])
    msg = _CapturingMsg(
        _request(claimed_tenant=_ATTACKER.tenant_id, claimed_coworker=_VICTIM.id)
    )
    await server._handle_request(msg)

    assert msg.reply is not None
    assert msg.reply["verdict"] is None
    assert "tenant_id mismatch" in msg.reply["error"], (
        f"cross-tenant coworker claim must be dropped; got {msg.reply['error']!r}"
    )


async def test_E2b_unknown_coworker_id_dropped() -> None:
    """Attacker forges a ``coworker_id`` that exists in no tenant. Defense:
    the lookup returns None → request refused (no silent tenant adoption)."""
    server = _server([_ATTACKER, _VICTIM])
    msg = _CapturingMsg(
        _request(claimed_tenant=_ATTACKER.tenant_id, claimed_coworker="cw-ghost")
    )
    await server._handle_request(msg)

    assert msg.reply is not None
    assert msg.reply["verdict"] is None
    assert "unknown coworker_id" in msg.reply["error"], (
        f"unknown coworker must be dropped; got {msg.reply['error']!r}"
    )


# ---------------------------------------------------------------------------
# E3. Control — a consistent, legitimate identity passes the tenant gate
# ---------------------------------------------------------------------------


async def test_E3_consistent_identity_passes_tenant_gate() -> None:
    """Anti-false-positive control: a request whose claimed (tenant, coworker)
    pair is internally consistent must NOT be rejected at the identity gate.
    With an empty registry it falls through to an ``unknown check_id`` reply —
    a DIFFERENT error than ``tenant_id mismatch`` — which proves the tenant
    guard let it through rather than blocking a benign caller."""
    server = _server([_ATTACKER, _VICTIM])
    msg = _CapturingMsg(
        _request(
            claimed_tenant=_ATTACKER.tenant_id,
            claimed_coworker=_ATTACKER.id,
            check_id="x.check",
        )
    )
    await server._handle_request(msg)

    assert msg.reply is not None
    assert "tenant_id mismatch" not in (msg.reply["error"] or "")
    assert "unknown coworker_id" not in (msg.reply["error"] or "")
    assert "unknown check_id" in msg.reply["error"], (
        "a legitimate identity must reach check lookup, not be dropped at the "
        f"tenant gate; got {msg.reply['error']!r}"
    )


# ---------------------------------------------------------------------------
# E6. NATS subject sidechannel — XFAIL (no NATS account-per-tenant)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "The authoritative-tenant guard only catches an INCONSISTENT forge "
        "(coworker_id and tenant_id from different tenants). A container that "
        "knows a victim's coworker_id AND its matching tenant_id can present "
        "the consistent pair on core NATS and the guard agrees — there is no "
        "per-tenant transport authentication binding a connection to a tenant. "
        "Closes with NATS account-per-tenant / tenant-scoped credentials so a "
        "connection cannot publish another tenant's coworker identity at all "
        "(see docs/17 §E6, docs/4)."
    ),
    strict=True,
)
async def test_E6_consistent_cross_tenant_forge_is_rejected() -> None:
    """Documenting test for the NATS-ACL gap. The IDEAL: even a fully
    consistent forge of a victim's identity is rejected because the
    transport itself binds the connection to tenant A. Today the safety RPC
    guard accepts the consistent (cw-victim, t-victim) pair, so this asserts
    a property the platform does not yet provide."""
    server = _server([_ATTACKER, _VICTIM])
    msg = _CapturingMsg(
        # Fully consistent victim identity — the guard cannot tell this came
        # from the attacker's connection.
        _request(claimed_tenant=_VICTIM.tenant_id, claimed_coworker=_VICTIM.id)
    )
    await server._handle_request(msg)

    assert msg.reply is not None
    # Ideal (currently FALSE): the forge is refused on identity grounds.
    assert msg.reply["error"] and "tenant" in msg.reply["error"], (
        "consistent cross-tenant identity forge is currently accepted — "
        "NATS account-per-tenant not yet in force"
    )
