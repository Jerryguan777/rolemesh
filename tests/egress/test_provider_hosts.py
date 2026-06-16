"""Tests for the platform-managed provider allowlist matcher.

``is_known_provider_host`` is the gateway's platform-allow layer: it must
recognise every LLM-provider upstream the reverse proxy actually dials
(including ``*_BASE_URL`` deployment overrides and Bedrock's region-
templated host) while NOT accidentally whitelisting anything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rolemesh.egress.reverse_proxy import (
    is_known_provider_host,
    known_provider_endpoints,
)

if TYPE_CHECKING:
    import pytest


def test_static_provider_hosts_allowed() -> None:
    assert is_known_provider_host("api.anthropic.com", 443)
    assert is_known_provider_host("api.openai.com", 443)
    assert is_known_provider_host("generativelanguage.googleapis.com", 443)


def test_host_match_is_case_and_trailing_dot_insensitive() -> None:
    assert is_known_provider_host("API.Anthropic.com", 443)
    assert is_known_provider_host("api.anthropic.com.", 443)


def test_wrong_port_blocked() -> None:
    # A matching host on the wrong port must not be allowed — leaving the
    # port open would whitelist non-TLS services on the same host.
    assert not is_known_provider_host("api.anthropic.com", 80)
    assert not is_known_provider_host("api.anthropic.com", 22)


def test_unknown_host_blocked() -> None:
    assert not is_known_provider_host("mcp.example.com", 443)
    assert not is_known_provider_host("attacker.com", 443)
    # Suffix/lookalike must not match the anchored set.
    assert not is_known_provider_host("api.anthropic.com.evil.com", 443)


def test_bedrock_region_templated_host_allowed() -> None:
    assert is_known_provider_host("bedrock-runtime.us-east-1.amazonaws.com", 443)
    assert is_known_provider_host("bedrock-runtime.eu-west-3.amazonaws.com", 443)


def test_bedrock_shape_is_anchored() -> None:
    # Empty region, wrong service, or a lookalike suffix must not match.
    assert not is_known_provider_host("bedrock-runtime..amazonaws.com", 443)
    assert not is_known_provider_host("s3.us-east-1.amazonaws.com", 443)
    assert not is_known_provider_host(
        "bedrock-runtime.us-east-1.amazonaws.com.evil.com", 443
    )
    # Right shape, wrong port.
    assert not is_known_provider_host("bedrock-runtime.us-east-1.amazonaws.com", 80)


def test_base_url_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that points OpenAI at a self-hosted gateway should have
    that host (and its port) allowed automatically — derived, not hardcoded."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm-proxy.internal:8443/v1")
    assert ("llm-proxy.internal", 8443) in known_provider_endpoints()
    assert is_known_provider_host("llm-proxy.internal", 8443)
    # The stock host is no longer the configured upstream, but the default
    # template host stays in the set only if still derived; with an override
    # the override host is what matters for this deployment.
    assert is_known_provider_host("llm-proxy.internal", 8443)


def test_known_endpoints_default_set() -> None:
    eps = known_provider_endpoints()
    assert ("api.anthropic.com", 443) in eps
    assert ("api.openai.com", 443) in eps
    assert ("generativelanguage.googleapis.com", 443) in eps
