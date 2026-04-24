"""Tests for the egress.domain_rule safety check (EC-3)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from rolemesh.safety.checks.egress_domain_rule import (
    EgressDomainCode,
    EgressDomainRuleCheck,
    EgressDomainRuleConfig,
    _matches,
    make_egress_domain_check,
)
from rolemesh.safety.types import SafetyContext, Stage

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeRequest:
    """Gateway-side request shape — minimal surface the check reads."""

    host: str
    port: int = 443


# ---------------------------------------------------------------------------
# _matches — the shared matching primitive
# ---------------------------------------------------------------------------


class TestMatches:
    def test_exact_match(self) -> None:
        assert _matches("api.anthropic.com", "api.anthropic.com")

    def test_exact_mismatch(self) -> None:
        assert not _matches("api.openai.com", "api.anthropic.com")

    def test_wildcard_matches_subdomain(self) -> None:
        assert _matches("api.github.com", "*.github.com")
        assert _matches("raw.github.com", "*.github.com")

    def test_wildcard_does_not_match_evil_suffix(self) -> None:
        """Regression guard: a pattern ``*.github.com`` must NOT let
        ``github.com.evil.com`` through. The old substring-match bug
        class has burnt plenty of security products; pin it."""
        assert not _matches("github.com.evil.com", "*.github.com")

    def test_wildcard_does_not_match_bare_apex(self) -> None:
        """``*.github.com`` matches subdomains only, not ``github.com``
        itself. Operators who want both must list them explicitly."""
        assert not _matches("github.com", "*.github.com")

    def test_case_insensitive(self) -> None:
        assert _matches("API.Anthropic.COM", "api.anthropic.com")
        assert _matches("API.Anthropic.COM", "*.ANTHROPIC.COM")

    def test_trailing_dot_tolerated(self) -> None:
        """DNS wire-format names sometimes arrive with a trailing dot."""
        assert _matches("api.anthropic.com.", "api.anthropic.com")


# ---------------------------------------------------------------------------
# Config validation (REST-time behaviour)
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_valid_minimal_config(self) -> None:
        cfg = EgressDomainRuleConfig.model_validate(
            {"domain_pattern": "api.anthropic.com"}
        )
        assert cfg.ports is None

    def test_empty_domain_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EgressDomainRuleConfig.model_validate({"domain_pattern": ""})

    def test_overlong_domain_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EgressDomainRuleConfig.model_validate(
                {"domain_pattern": "x" * 254}
            )

    def test_extra_keys_rejected(self) -> None:
        """Typo like ``domains`` (plural) must fail loud at REST time."""
        with pytest.raises(ValidationError):
            EgressDomainRuleConfig.model_validate(
                {"domain_pattern": "x.com", "domains": ["y.com"]}
            )


# ---------------------------------------------------------------------------
# Gateway-side adapter (the factory path)
# ---------------------------------------------------------------------------


class TestGatewayAdapter:
    async def test_match_returns_true_and_finding(self) -> None:
        check = make_egress_domain_check()
        matched, findings = await check(
            _FakeRequest(host="api.anthropic.com", port=443),
            {"domain_pattern": "api.anthropic.com"},
        )
        assert matched is True
        assert len(findings) == 1
        assert findings[0]["code"] == EgressDomainCode.DOMAIN_ALLOWED.value

    async def test_non_match_returns_false_no_findings(self) -> None:
        check = make_egress_domain_check()
        matched, findings = await check(
            _FakeRequest(host="api.openai.com", port=443),
            {"domain_pattern": "api.anthropic.com"},
        )
        assert matched is False
        assert findings == []

    async def test_port_restriction_enforced(self) -> None:
        """domain_pattern matches but port doesn't — must miss."""
        check = make_egress_domain_check()
        matched, _ = await check(
            _FakeRequest(host="api.anthropic.com", port=80),
            {"domain_pattern": "api.anthropic.com", "ports": [443]},
        )
        assert matched is False

    async def test_port_none_matches_any(self) -> None:
        check = make_egress_domain_check()
        matched, _ = await check(
            _FakeRequest(host="api.anthropic.com", port=8080),
            {"domain_pattern": "api.anthropic.com"},
        )
        assert matched is True

    async def test_bad_config_misses_safely(self) -> None:
        """An invalid config should not crash the gateway — miss is the
        safe behaviour because 'no rule matches = block'."""
        check = make_egress_domain_check()
        matched, _ = await check(
            _FakeRequest(host="api.anthropic.com"),
            {},  # missing domain_pattern
        )
        assert matched is False


# ---------------------------------------------------------------------------
# Orchestrator-side SafetyCheck Protocol path
# ---------------------------------------------------------------------------


class TestSafetyCheckProtocol:
    def test_registered_stages_include_egress(self) -> None:
        check = EgressDomainRuleCheck()
        assert Stage.EGRESS_REQUEST in check.stages

    def test_check_class_advertises_stable_codes(self) -> None:
        check = EgressDomainRuleCheck()
        assert EgressDomainCode.DOMAIN_ALLOWED.value in check.supported_codes

    async def test_check_returns_allow_with_finding_on_match(self) -> None:
        check = EgressDomainRuleCheck()
        ctx = SafetyContext(
            stage=Stage.EGRESS_REQUEST,
            tenant_id="t",
            coworker_id="cw",
            user_id="u",
            job_id="j",
            conversation_id="c",
            payload={"host": "api.anthropic.com", "port": 443},
        )
        verdict = await check.check(
            ctx, {"domain_pattern": "api.anthropic.com"}
        )
        assert verdict.action == "allow"
        assert len(verdict.findings) == 1

    async def test_check_returns_allow_without_finding_on_miss(self) -> None:
        """Non-match also returns allow (the aggregator decides the
        final verdict from whether any rule produced a finding)."""
        check = EgressDomainRuleCheck()
        ctx = SafetyContext(
            stage=Stage.EGRESS_REQUEST,
            tenant_id="t",
            coworker_id="cw",
            user_id="u",
            job_id="j",
            conversation_id="c",
            payload={"host": "api.openai.com", "port": 443},
        )
        verdict = await check.check(
            ctx, {"domain_pattern": "api.anthropic.com"}
        )
        assert verdict.action == "allow"
        assert verdict.findings == []


class TestRegistryIntegration:
    def test_egress_check_registered_in_orchestrator_registry(self) -> None:
        """The orchestrator registry must know about egress.domain_rule
        so the REST layer's _validate_safety_rule_body can validate
        rules referencing it."""
        from rolemesh.safety.registry import (
            build_orchestrator_registry,
            reset_orchestrator_registry,
        )

        reset_orchestrator_registry()
        reg = build_orchestrator_registry()
        assert reg.has("egress.domain_rule")
