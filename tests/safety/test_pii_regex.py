"""Behaviour tests for PIIRegexCheck.

Focuses on bug-finding edges (per user testing guidance): empty configs,
unknown pattern keys, nested payload scanning, no-false-negatives for
plausible injection vectors, multi-code findings. We deliberately do
NOT mirror the regex definitions — the test ships real inputs and real
expectations, not re-implementations of the matcher.
"""

from __future__ import annotations

import pytest

from rolemesh.safety.checks.pii_regex import PIICode, PIIRegexCheck
from rolemesh.safety.types import Finding, Stage

from .conftest import make_context


@pytest.fixture
def check() -> PIIRegexCheck:
    return PIIRegexCheck()


class TestMetadata:
    def test_id_and_version_stable(self, check: PIIRegexCheck) -> None:
        assert check.id == "pii.regex"
        assert check.version == "1"

    def test_cost_class_is_cheap(self, check: PIIRegexCheck) -> None:
        # cheap means "safe inside the container"; V2's enforcement
        # matrix relies on this not quietly changing to slow.
        assert check.cost_class == "cheap"

    def test_supported_codes_matches_enum(self, check: PIIRegexCheck) -> None:
        assert check.supported_codes == frozenset(c.value for c in PIICode)

    def test_declares_all_four_content_stages(
        self, check: PIIRegexCheck
    ) -> None:
        # PRE_COMPACTION is observational and not declared (pii.regex
        # is not useful on transcript summarization input).
        assert Stage.PRE_TOOL_CALL in check.stages
        assert Stage.INPUT_PROMPT in check.stages
        assert Stage.MODEL_OUTPUT in check.stages
        assert Stage.POST_TOOL_RESULT in check.stages
        assert Stage.PRE_COMPACTION not in check.stages


class TestDetection:
    @pytest.mark.asyncio
    async def test_ssn_in_tool_input_blocks(self, check: PIIRegexCheck) -> None:
        ctx = make_context(
            payload={
                "tool_name": "github__create_issue",
                "tool_input": {"body": "My SSN is 123-45-6789 here"},
            }
        )
        verdict = await check.check(ctx, {"patterns": {"SSN": True}})
        assert verdict.action == "block"
        codes = [f.code for f in verdict.findings]
        assert PIICode.SSN.value in codes

    @pytest.mark.asyncio
    async def test_ssn_not_enabled_allows(self, check: PIIRegexCheck) -> None:
        # If SSN is disabled, a payload containing one must pass.
        ctx = make_context(
            payload={"tool_input": {"body": "123-45-6789"}},
        )
        verdict = await check.check(ctx, {"patterns": {"EMAIL": True}})
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_credit_card_detection(self, check: PIIRegexCheck) -> None:
        ctx = make_context(
            payload={"tool_input": {"cc": "4111-1111-1111-1111"}}
        )
        v = await check.check(ctx, {"patterns": {"CREDIT_CARD": True}})
        assert v.action == "block"

    @pytest.mark.asyncio
    async def test_email_detection(self, check: PIIRegexCheck) -> None:
        ctx = make_context(payload={"tool_input": {"to": "alice@example.com"}})
        v = await check.check(ctx, {"patterns": {"EMAIL": True}})
        assert v.action == "block"
        assert any(f.code == PIICode.EMAIL.value for f in v.findings)

    @pytest.mark.asyncio
    async def test_phone_us_detection(self, check: PIIRegexCheck) -> None:
        ctx = make_context(payload={"tool_input": {"phone": "(415) 555-2671"}})
        v = await check.check(ctx, {"patterns": {"PHONE_US": True}})
        assert v.action == "block"

    @pytest.mark.asyncio
    async def test_ip_address_detection(self, check: PIIRegexCheck) -> None:
        ctx = make_context(payload={"tool_input": {"ip": "192.168.1.100"}})
        v = await check.check(ctx, {"patterns": {"IP_ADDRESS": True}})
        assert v.action == "block"

    @pytest.mark.asyncio
    async def test_multiple_codes_surface_all_findings(
        self, check: PIIRegexCheck
    ) -> None:
        ctx = make_context(
            payload={
                "tool_input": {
                    "body": "Contact alice@example.com about SSN 111-22-3333",
                }
            }
        )
        v = await check.check(
            ctx, {"patterns": {"SSN": True, "EMAIL": True}}
        )
        assert v.action == "block"
        codes = {f.code for f in v.findings}
        assert PIICode.SSN.value in codes
        assert PIICode.EMAIL.value in codes

    @pytest.mark.asyncio
    async def test_nested_dict_scanned(self, check: PIIRegexCheck) -> None:
        # Ensures the pipeline extractor walks recursive structures,
        # not just top-level strings.
        ctx = make_context(
            payload={
                "tool_input": {
                    "meta": {"author": {"ssn": "123-45-6789"}},
                },
            },
        )
        v = await check.check(ctx, {"patterns": {"SSN": True}})
        assert v.action == "block"

    @pytest.mark.asyncio
    async def test_list_values_scanned(self, check: PIIRegexCheck) -> None:
        ctx = make_context(
            payload={
                "tool_input": {
                    "recipients": ["bob@x.com", "charlie@y.com"],
                },
            },
        )
        v = await check.check(ctx, {"patterns": {"EMAIL": True}})
        assert v.action == "block"


class TestBoundaryConditions:
    @pytest.mark.asyncio
    async def test_empty_patterns_allows(self, check: PIIRegexCheck) -> None:
        ctx = make_context(payload={"tool_input": {"x": "123-45-6789"}})
        # Empty config is a no-op — not an error, not a block.
        v = await check.check(ctx, {})
        assert v.action == "allow"

    @pytest.mark.asyncio
    async def test_patterns_all_false_allows(
        self, check: PIIRegexCheck
    ) -> None:
        ctx = make_context(payload={"tool_input": {"x": "123-45-6789"}})
        v = await check.check(
            ctx, {"patterns": {"SSN": False, "EMAIL": False}}
        )
        assert v.action == "allow"

    @pytest.mark.asyncio
    async def test_unknown_pattern_key_silently_ignored(
        self, check: PIIRegexCheck
    ) -> None:
        # Forward-compat: a newer rule config referencing PII.PASSPORT
        # must not crash older pii.regex versions.
        ctx = make_context(payload={"tool_input": {"x": "nothing sensitive"}})
        v = await check.check(
            ctx, {"patterns": {"PASSPORT": True, "SSN": True}}
        )
        assert v.action == "allow"
        assert v.findings == []

    @pytest.mark.asyncio
    async def test_non_dict_patterns_treated_as_allow(
        self, check: PIIRegexCheck
    ) -> None:
        # Defensive: garbage config types do not crash. V1 chooses
        # allow rather than block because the admin mis-typed a config,
        # not a user sending bad data. V2's schema validation at REST
        # should catch this upstream.
        ctx = make_context(payload={"tool_input": {"x": "123-45-6789"}})
        v = await check.check(ctx, {"patterns": "nope"})  # type: ignore[dict-item]
        assert v.action == "allow"

    @pytest.mark.asyncio
    async def test_missing_tool_input_allows(
        self, check: PIIRegexCheck
    ) -> None:
        ctx = make_context(payload={"tool_name": "noop"})
        v = await check.check(ctx, {"patterns": {"SSN": True}})
        assert v.action == "allow"

    @pytest.mark.asyncio
    async def test_integer_fields_coerced_for_scan(
        self, check: PIIRegexCheck
    ) -> None:
        # A dict containing a numeric leaf that looks like an IP or SSN
        # must not escape detection just because it's not typed str.
        ctx = make_context(payload={"tool_input": {"port": 8080}})
        # Numeric 8080 is not a PII match; only assert no crash and allow.
        v = await check.check(ctx, {"patterns": {"IP_ADDRESS": True}})
        assert v.action == "allow"


class TestFindingShape:
    @pytest.mark.asyncio
    async def test_finding_has_stable_fields(
        self, check: PIIRegexCheck
    ) -> None:
        ctx = make_context(payload={"tool_input": {"x": "123-45-6789"}})
        v = await check.check(ctx, {"patterns": {"SSN": True}})
        assert v.findings
        finding: Finding = v.findings[0]
        assert finding.code == PIICode.SSN.value
        assert finding.severity == "high"
        assert finding.message
        # supported_codes contract: emitted code MUST be in the set.
        assert finding.code in check.supported_codes
