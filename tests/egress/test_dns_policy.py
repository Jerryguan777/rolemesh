"""Tests for the platform DNS policy (env parsing + match semantics).

The matching function itself (``matches_domain``) belongs to
``egress_domain_rule`` and is covered there; these tests pin the
policy-level contract: env round-trip, default fail-closed mode, and
the loud failure on a mode typo.
"""

from __future__ import annotations

import pytest

from rolemesh.egress.dns_policy import (
    ALLOWLIST_ENV,
    MODE_ENV,
    GlobalDnsPolicy,
)


def test_default_policy_is_enforce_and_empty() -> None:
    policy = GlobalDnsPolicy()
    assert policy.mode == "enforce"
    assert policy.patterns == ()
    assert not policy.is_allowed("example.com")


def test_exact_and_wildcard_patterns() -> None:
    policy = GlobalDnsPolicy(patterns=("api.example.com", "*.github.com"))
    assert policy.is_allowed("api.example.com")
    assert policy.is_allowed("API.Example.COM.")  # case + trailing dot
    assert policy.is_allowed("raw.github.com")
    assert not policy.is_allowed("github.com.evil.com")
    assert not policy.is_allowed("example.com")


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(MODE_ENV, raising=False)
    policy = GlobalDnsPolicy.from_env()
    assert policy.mode == "enforce"
    assert policy.patterns == ()


def test_from_env_parses_list_and_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Trailing comma + stray whitespace are .env-editing facts of life.
    monkeypatch.setenv(ALLOWLIST_ENV, " metrics.corp , *.example.com ,")
    monkeypatch.setenv(MODE_ENV, "Observe")
    policy = GlobalDnsPolicy.from_env()
    assert policy.patterns == ("metrics.corp", "*.example.com")
    assert policy.mode == "observe"


def test_from_env_rejects_bad_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd mode must kill the gateway boot, not silently pick one."""
    monkeypatch.setenv(MODE_ENV, "audit")
    with pytest.raises(ValueError, match="EGRESS_DNS_MODE"):
        GlobalDnsPolicy.from_env()
