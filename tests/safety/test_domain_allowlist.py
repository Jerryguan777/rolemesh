"""Tests for the domain_allowlist check.

Focus on the invariants that make or break this check in production:
  - Wildcards: ``*.x.com`` matches subdomains but NOT the apex.
  - Exact host: ``github.com`` does NOT silently match ``fake-github.com``.
  - URL extraction walks every string leaf of tool_input.
  - Case-insensitive host matching (per RFC), case-preserving input
    (debug-friendly).
  - Empty / malformed config at runtime → allow (REST layer rejects
    malformed inputs at create time).
"""

from __future__ import annotations

import pytest

from rolemesh.safety.checks.domain_allowlist import (
    DomainAllowlistCheck,
    DomainAllowlistConfig,
)
from rolemesh.safety.types import SafetyContext, Stage, ToolInfo


def _ctx(payload: dict) -> SafetyContext:
    return SafetyContext(
        stage=Stage.PRE_TOOL_CALL,
        tenant_id="t",
        coworker_id="c",
        user_id="u",
        job_id="j",
        conversation_id="cv",
        payload=payload,
        tool=ToolInfo(name="WebFetch", reversible=True),
    )


class TestWildcardSemantics:
    @pytest.mark.asyncio
    async def test_wildcard_matches_subdomain(self) -> None:
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {"url": "https://api.github.com/repos"},
                }
            ),
            {"allowed_hosts": ["*.github.com"]},
        )
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_wildcard_does_not_match_apex(self) -> None:
        # ``*.example.com`` DOES NOT cover ``example.com`` — deliberate
        # so operators must spell out the apex when they want it.
        # Regression would be a common misconfiguration: admin writes
        # ``*.prod.internal``, accidentally covers the apex too.
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {"url": "https://github.com/robots.txt"},
                }
            ),
            {"allowed_hosts": ["*.github.com"]},
        )
        assert verdict.action == "block"
        assert verdict.findings[0].metadata["host"] == "github.com"

    @pytest.mark.asyncio
    async def test_apex_and_wildcard_together_cover_both(self) -> None:
        chk = DomainAllowlistCheck()
        for url in (
            "https://github.com/",
            "https://api.github.com/",
            "https://a.b.github.com/",
        ):
            verdict = await chk.check(
                _ctx(
                    {"tool_name": "WebFetch", "tool_input": {"url": url}}
                ),
                {"allowed_hosts": ["github.com", "*.github.com"]},
            )
            assert verdict.action == "allow", url


class TestExactHostMatching:
    @pytest.mark.asyncio
    async def test_exact_pattern_does_not_cover_substring_host(
        self,
    ) -> None:
        # Attack: operator allowlists ``github.com``; attacker points
        # agent at ``evil-github.com``. Must block.
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {"url": "https://evil-github.com/x"},
                }
            ),
            {"allowed_hosts": ["github.com"]},
        )
        assert verdict.action == "block"

    @pytest.mark.asyncio
    async def test_host_matching_is_case_insensitive(self) -> None:
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {"url": "https://GITHUB.COM/"},
                }
            ),
            {"allowed_hosts": ["github.com"]},
        )
        assert verdict.action == "allow"


class TestUrlExtraction:
    @pytest.mark.asyncio
    async def test_extracts_url_from_nested_dict(self) -> None:
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {
                        "nested": {
                            "config": {
                                "endpoint": "https://evil.com/api",
                            }
                        }
                    },
                }
            ),
            {"allowed_hosts": ["github.com"]},
        )
        assert verdict.action == "block"
        assert verdict.findings[0].metadata["host"] == "evil.com"

    @pytest.mark.asyncio
    async def test_extracts_url_from_list(self) -> None:
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {
                        "urls": [
                            "https://github.com/ok",
                            "https://evil.com/bad",
                        ]
                    },
                }
            ),
            {"allowed_hosts": ["*.github.com", "github.com"]},
        )
        # Only evil.com triggers; github.com is allowed.
        assert verdict.action == "block"
        hosts = {f.metadata["host"] for f in verdict.findings}
        assert hosts == {"evil.com"}

    @pytest.mark.asyncio
    async def test_extracts_url_from_prose_strings(self) -> None:
        # URLs commonly appear inside prose / rationale fields, not
        # just bare url keys. A refactor that only scans explicit
        # ``url`` keys would miss injection through a prose field.
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {
                        "query": "please fetch https://evil.com/x for me",
                    },
                }
            ),
            {"allowed_hosts": ["github.com"]},
        )
        assert verdict.action == "block"

    @pytest.mark.asyncio
    async def test_deduplicates_identical_hosts(self) -> None:
        # Multiple URLs to the same host → one finding, not N.
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {
                        "a": "https://evil.com/1",
                        "b": "https://evil.com/2",
                        "c": "https://evil.com/3",
                    },
                }
            ),
            {"allowed_hosts": ["github.com"]},
        )
        assert verdict.action == "block"
        assert len(verdict.findings) == 1


class TestEmptyConfigBehaviour:
    @pytest.mark.asyncio
    async def test_runtime_missing_allowed_hosts_allows(self) -> None:
        # REST layer rejects empty lists at create time. At runtime
        # we treat missing/empty as allow to avoid turning a snapshot
        # hot-update bug into a blanket outbound block.
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "WebFetch",
                    "tool_input": {"url": "https://anything.com/"},
                }
            ),
            {},
        )
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_no_urls_in_payload_allows(self) -> None:
        chk = DomainAllowlistCheck()
        verdict = await chk.check(
            _ctx(
                {
                    "tool_name": "Read",
                    "tool_input": {"path": "/etc/passwd"},
                }
            ),
            {"allowed_hosts": ["github.com"]},
        )
        assert verdict.action == "allow"


class TestConfigValidation:
    def test_empty_allowed_hosts_rejected_by_pydantic(self) -> None:
        with pytest.raises(ValueError):
            DomainAllowlistConfig(allowed_hosts=[])

    def test_whitespace_only_entries_stripped_and_rejected(self) -> None:
        with pytest.raises(ValueError):
            DomainAllowlistConfig(allowed_hosts=["", " "])

    def test_extra_key_rejected(self) -> None:
        # Typo catching: ``allowed_host`` (singular) must 422 at create.
        with pytest.raises(ValueError):
            DomainAllowlistConfig(
                allowed_hosts=["x"],  # type: ignore[call-arg]
                allowed_host="y",
            )

    def test_action_override_stored(self) -> None:
        cfg = DomainAllowlistConfig(
            allowed_hosts=["github.com"],
            action_override="require_approval",
        )
        assert cfg.action_override == "require_approval"


class TestRegistryRegistration:
    def test_registered_in_container_registry(self) -> None:
        from rolemesh.safety.registry import build_container_registry

        reg = build_container_registry()
        assert reg.has("domain_allowlist")

    def test_registered_in_orchestrator_registry(self) -> None:
        from rolemesh.safety.registry import build_orchestrator_registry

        reg = build_orchestrator_registry()
        assert reg.has("domain_allowlist")
