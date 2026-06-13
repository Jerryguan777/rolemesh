"""T-VER: verify_infrastructure is read-only and fail-closed (docs/21 §4.2).

The deployment layer declares the infrastructure; the application only
verifies the declared invariants at startup and refuses to start when
any of them does not hold — with an error message that tells the
operator how to fix the DEPLOYMENT, never by repairing anything itself.

Each fail-closed case breaks exactly one invariant by pointing the
configuration somewhere wrong (real daemon, zero mocks) and asserts
verify_infrastructure raises with actionable guidance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import rolemesh.core.config as config

if TYPE_CHECKING:
    from rolemesh.container.runtime import ContainerRuntime

pytestmark = pytest.mark.integration


def _assert_actionable(message: str, *expected_fragments: str) -> None:
    """The contract for every verify failure: name the broken invariant
    and point at the deployment layer as the fix — not at app code."""
    for fragment in expected_fragments:
        assert fragment in message, (
            f"verify error must mention {fragment!r} so the operator can "
            f"locate the broken invariant; got: {message}"
        )
    assert "deployment layer" in message, (
        "verify error must direct the operator to the deployment layer "
        f"(declarative-infra contract); got: {message}"
    )


async def test_verify_passes_against_running_deployment(
    runtime: ContainerRuntime,
) -> None:
    """T-VER-1: with the real configuration and the compose stack up,
    verify_infrastructure completes without raising."""
    await runtime.verify_infrastructure()


async def test_verify_fails_closed_when_agent_network_missing(
    runtime: ContainerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-VER-2: a nonexistent agent network is reported immediately
    (static invariant — waiting cannot create a network) with guidance."""
    monkeypatch.setattr(
        config, "CONTAINER_NETWORK_NAME", "rolemesh-contract-no-such-net"
    )
    with pytest.raises(RuntimeError) as exc_info:
        await runtime.verify_infrastructure()
    _assert_actionable(str(exc_info.value), "rolemesh-contract-no-such-net")


async def test_verify_fails_closed_when_agent_network_not_isolated(
    runtime: ContainerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-VER-3: an agent network WITHOUT the no-egress invariant must be
    refused — agents would have a direct route to the internet.

    Constructed without building anything: the egress network exists in
    the deployment but is deliberately NOT internal, so pointing the
    agent-network config at it presents verify with a real network that
    violates exactly this one invariant.
    """
    monkeypatch.setattr(
        config, "CONTAINER_NETWORK_NAME", config.CONTAINER_EGRESS_NETWORK_NAME
    )
    with pytest.raises(RuntimeError) as exc_info:
        await runtime.verify_infrastructure()
    message = str(exc_info.value)
    assert config.CONTAINER_EGRESS_NETWORK_NAME in message
    # The message must say WHY this is fatal (egress isolation), not
    # just that a check failed.
    assert "Internal" in message or "egress" in message.lower()


async def test_verify_fails_closed_when_egress_network_missing(
    runtime: ContainerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-VER-4: a missing egress network (the gateway's route out) is
    reported with deployment guidance."""
    monkeypatch.setattr(
        config, "CONTAINER_EGRESS_NETWORK_NAME", "rolemesh-contract-no-egress-net"
    )
    with pytest.raises(RuntimeError) as exc_info:
        await runtime.verify_infrastructure()
    _assert_actionable(str(exc_info.value), "rolemesh-contract-no-egress-net")


async def test_verify_fails_closed_on_gateway_dns_ip_drift(
    runtime: ContainerRuntime,
    monkeypatch: pytest.MonkeyPatch,
    fast_verify: None,
) -> None:
    """T-VER-5: configured EGRESS_GATEWAY_DNS_IP != the running
    gateway's actual address must refuse startup — every agent spawn
    would silently lose DNS otherwise. The message names both values."""
    monkeypatch.setattr(config, "EGRESS_GATEWAY_DNS_IP", "172.28.100.99")
    with pytest.raises(RuntimeError) as exc_info:
        await runtime.verify_infrastructure()
    _assert_actionable(str(exc_info.value), "172.28.100.99", "EGRESS_GATEWAY_DNS_IP")


async def test_verify_fails_closed_when_gateway_absent(
    runtime: ContainerRuntime,
    monkeypatch: pytest.MonkeyPatch,
    fast_verify: None,
) -> None:
    """T-VER-6: no gateway workload under the configured identity at all
    (not merely unhealthy) is reported with deployment guidance."""
    monkeypatch.setattr(
        config, "EGRESS_GATEWAY_CONTAINER_NAME", "rolemesh-contract-no-gateway"
    )
    with pytest.raises(RuntimeError) as exc_info:
        await runtime.verify_infrastructure()
    _assert_actionable(str(exc_info.value), "rolemesh-contract-no-gateway")


async def test_verify_fails_closed_when_nats_unreachable(
    runtime: ContainerRuntime,
    monkeypatch: pytest.MonkeyPatch,
    fast_verify: None,
) -> None:
    """T-VER-7: a NATS endpoint nothing listens on fails verification
    after the bounded retry budget, naming the endpoint it tried."""
    monkeypatch.setattr(config, "NATS_URL", "nats://127.0.0.1:59993")
    with pytest.raises(RuntimeError) as exc_info:
        await runtime.verify_infrastructure()
    _assert_actionable(str(exc_info.value), "NATS", "59993")
