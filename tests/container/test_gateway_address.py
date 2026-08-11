"""Gateway address provider (container.gateway_address).

The provider is the single mutable home for the agent-facing gateway
address: static config value by default (docker mode, K8s static
ClusterIP mode), overridden by the K8s runtime's discovery in dynamic
mode. These tests lock the fallback/override/reset semantics the spawn
path (compute_egress_routing) depends on.
"""

from __future__ import annotations

import pytest

from rolemesh.container import gateway_address
from rolemesh.core.config import EGRESS_GATEWAY_DNS_IP


@pytest.fixture(autouse=True)
def _reset() -> None:
    gateway_address.reset_gateway_dns_ip()
    yield
    gateway_address.reset_gateway_dns_ip()


def test_defaults_to_static_config_value() -> None:
    # docker mode and K8s static mode never call set(): the provider is
    # a pass-through to the config constant.
    assert gateway_address.get_gateway_dns_ip() == EGRESS_GATEWAY_DNS_IP


def test_discovered_override_wins() -> None:
    gateway_address.set_gateway_dns_ip("10.96.7.42")
    assert gateway_address.get_gateway_dns_ip() == "10.96.7.42"


def test_reset_restores_config_fallback() -> None:
    gateway_address.set_gateway_dns_ip("10.96.7.42")
    gateway_address.reset_gateway_dns_ip()
    assert gateway_address.get_gateway_dns_ip() == EGRESS_GATEWAY_DNS_IP


def test_spawn_path_reads_the_provider() -> None:
    # The value agents get as their resolver must follow the provider —
    # runner imports the getter (call-time read), so an override set by
    # the K8s runtime reaches every subsequently built spec.
    from rolemesh.container.runner import compute_egress_routing

    gateway_address.set_gateway_dns_ip("10.96.7.42")
    assert compute_egress_routing(None).dns_servers == ["10.96.7.42"]
    gateway_address.reset_gateway_dns_ip()
    assert compute_egress_routing(None).dns_servers == [EGRESS_GATEWAY_DNS_IP]
